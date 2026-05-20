# StageCache

StageCache is a macOS storage-staging tool that helps large programs run more smoothly from external storage. It monitors which files a program repeatedly reads, identifies high-impact read-only files, temporarily stages selected files on the internal SSD, and restores the original external storage layout afterward.

The goal is to reduce runtime stutter caused by CPU/GPU stalls when a program has to fetch data through a bottlenecked external storage path.

StageCache is designed for large programs running from external SSDs, including games, emulators, creative tools, development environments, virtualized workloads, and other applications that repeatedly access read-heavy files during execution.

## Motivation

External SSDs are useful for affordable high-capacity storage, especially on Macs where internal SSD upgrades can be expensive. The tradeoff is that external storage can become a runtime bottleneck when a large program repeatedly needs data that is not already in memory.

The bottleneck occurs because the CPU/GPU can only work with data once it reaches the system. If a program needs a file, asset, module, shader, cache entry, or runtime dependency that is not currently loaded, macOS has to fetch it from storage. When that data lives on an external SSD, the request has to travel through the external connection path before it can reach memory and be used by the processor.

I started StageCache after noticing this issue while running a large game from an external SSD. The machine had enough compute power, but runtime hitches appeared when the game had to repeatedly pull new data from external storage. That made the problem feel less like a raw compute issue and more like a data-path issue.

StageCache explores a simple question:

> Can a program stored externally temporarily borrow a controlled amount of internal SSD speed without needing the entire program installed internally?

---

## Core concept

StageCache acts as a temporary file-level staging layer between an external SSD and the internal SSD.

Instead of moving an entire program internally, StageCache only stages selected files that appear valuable during runtime.

The current flow is:

```text
monitor process → generate profile → analyze hot files → stage candidates → run program → restore originals
```

During staging, selected external files are copied to the internal SSD cache. The original external files are renamed as backups, and symlinks are placed at the original paths. The program continues using the paths it expects, but selected reads are redirected to the faster internal copy.

After runtime/testing, StageCache restores the original files and removes the cached internal copies.

StageCache is not a replacement for RAM, CPU cache, GPU memory, or the operating system’s filesystem cache. It is a profile-guided file staging system that simulates cache-like behavior at the storage level.

---

## Current scripts

```text
stagecache_monitor.py  → captures file access data
stagecache_analyze.py  → selects candidate files
stagecache_stage.py    → stages/restores candidate files
```

Multi-run processing is planned for V2, but it is not part of the stable current pipeline yet.

---

## Pipeline

```text
1. Monitor a process
        ↓
2. Generate profile.json
        ↓
3. Analyze profile.json
        ↓
4. Generate candidates.json
        ↓
5. Dry run staging
        ↓
6. Apply staging
        ↓
7. Run/test the program
        ↓
8. Restore original files
```

---

## Usage

### 1. Capture a profile

```bash
sudo python3 stagecache_monitor.py \
  --process "process_name" \
  --output "profiles/run1.json"
```

The monitor uses macOS `fs_usage` to capture filesystem activity for the target process.

The profile records:

```text
path
file name
file size
access count
read count
write count
metadata count
operation types
process names
raw fs_usage lines
```

The monitor only observes activity. It does not modify files.

---

### 2. Analyze the profile

```bash
python3 stagecache_analyze.py \
  --profile "profiles/run1.json" \
  --output "candidates.json" \
  --budget-gb 5 \
  --external-root "/Volumes/ExternalSSD"
```

The analyzer filters the profile down to safe external-storage candidates.

Candidate files must generally satisfy:

```text
path is clean and absolute
path is under the external storage root
read_count > 0
write_count == 0
file size is known
file is not likely save/config/temp/log data
```

The base score is:

```text
score = read_count / max(size_mb, 1)
```

This measures impact-to-size ratio.

The analyzer also supports dynamic read-time savings estimation:

```text
estimated_saved_ms =
read_count * size_mb * (1 / external_mbps - 1 / internal_mbps) * 1000
```

This helps avoid staging tiny files that are technically high-scoring but not worth the staging/symlink overhead.

Example optimized analyzer run:

```bash
python3 stagecache_analyze.py \
  --profile "profiles/run1.json" \
  --output "candidates_optimized.json" \
  --budget-gb 5 \
  --external-root "/Volumes/ExternalSSD" \
  --external-mbps 500 \
  --internal-mbps 5000 \
  --min-score 1.0 \
  --min-estimated-saved-ms 25
```

---

### 3. Dry run staging

Always dry run first:

```bash
python3 stagecache_stage.py \
  --candidates "candidates.json" \
  --cache-root "$HOME/Library/Caches/StageCache" \
  --journal "journals/stagecache_journal.json"
```

A correct dry run should only show files under the external storage root, for example:

```text
/Volumes/ExternalSSD/Path/To/Program/...
```

If the dry run shows unexpected system paths such as `/System`, `/Library`, `/Users/...`, or `/private/var`, stop and regenerate candidates with the correct `--external-root`.

---

### 4. Apply staging

```bash
python3 stagecache_stage.py \
  --candidates "candidates.json" \
  --cache-root "$HOME/Library/Caches/StageCache" \
  --journal "journals/stagecache_journal.json" \
  --apply
```

For each candidate, the stager:

```text
copies the selected file to the internal SSD cache
renames the original external file to .stagecache-original
creates a symlink at the original path
updates the restore journal
```

Example staged layout:

```text
/Volumes/ExternalSSD/App/file.dat
    -> symlink to internal cached copy

/Volumes/ExternalSSD/App/file.dat.stagecache-original
    -> original external file backup

~/Library/Caches/StageCache/Volumes/ExternalSSD/App/file.dat
    -> internal cached copy
```

---

### 5. Restore after runtime/testing

```bash
python3 stagecache_stage.py \
  --restore \
  --journal "journals/stagecache_journal.json" \
  --apply
```

After restoring, verify cleanup:

```bash
find "/Volumes/ExternalSSD/Path/To/Program" -name "*.stagecache-original"
find "/Volumes/ExternalSSD/Path/To/Program" -type l
```

Both commands should return nothing after a clean restore.

---

## Safety model

StageCache is intentionally conservative.

It avoids files that were written during profiling:

```text
write_count > 0
```

It also skips likely save, config, temp, and log files.

Original files are not deleted during staging. They are renamed with:

```text
.stagecache-original
```

The journal records:

```text
original_path
cache_path
backup_path
size_bytes
status
```

Before applying staging, check for leftover backups or symlinks:

```bash
find "/Volumes/ExternalSSD/Path/To/Program" -name "*.stagecache-original"
find "/Volumes/ExternalSSD/Path/To/Program" -type l
```

If either command returns unexpected output, inspect or restore before staging again.

---

## First real-world test result

The first real-world test used a large game running from an external SSD with a 5 GB staging budget.

Observed result:

```text
Startup time:
No meaningful difference observed.

Runtime smoothness:
Approx. 25–30% reduction in general jitteriness.
Approx. 40–50% reduction in noticeable hitches.
Major stutters were mostly eliminated during the tested route.
Remaining issues were mostly minor hitches when entering new areas or triggering new runtime data loads (since the CPU loads a new asset chunk, one that might not have been part of the profiling run).

Persistent issue:
Tearing/ghosting remained.
```

This suggests StageCache reduced storage-related hitches, while tearing/ghosting was likely related to rendering, frame pacing, display sync, or a translation layer (layer that allows windows apps to run on macOS by creating a wrapper for the windows app to run on) rather than external SSD access.

---

## Tradeoffs and limitations

StageCache currently works at the **file level**.

It can stage:

```text
/Volumes/ExternalSSD/App/archive.dat
```

but it cannot stage only one asset inside that archive.

Many large programs store runtime data inside archive, container, or chunk files. Examples include:

```text
.pak
.bundle
.dat
.assets
.arc
.bdt
.ucas
.utoc
```

This creates an important tradeoff: if a program uses smaller or medium-sized chunks, StageCache has more flexibility. It can stage the files that matter most while staying within the user’s internal SSD budget.

If a program uses very large average asset chunks, StageCache becomes more budget-dependent. A file may contain useful hot data, but if the whole archive is several gigabytes, staging it can consume most of the user’s budget. If the file is larger than the budget, StageCache cannot stage it at all.

Example cases:

```text
Best case:
Many useful 10 MB–500 MB chunks
→ StageCache can selectively stage high-impact files

Mixed case:
Several 2 GB–5 GB chunks
→ StageCache can help, but needs a larger budget

Worst case:
One or two massive 40 GB–80 GB archives
→ StageCache becomes less effective because staging them is similar to installing the whole program internally
```

This does not make StageCache ineffective, but it changes how useful it can be for each program. Its effectiveness depends on how the program’s developers organized their internal file layout.

---

## Candidate selection approach

A larger staging budget can help, especially when a program relies on multi-GB archive containers. However, StageCache should not simply fill the budget for the sake of filling it.

The analyzer is being designed around:

```text
impact-to-size scoring
dynamic estimated read-time savings
runtime-vs-startup awareness
small-file usefulness filtering
large-archive selection improvements
```

The goal is to stage files that are likely to reduce runtime stalls, not simply the files that happen to fit.

---

## Dynamic small-file filtering

A small file is not automatically worth staging.

Some small files are so cheap to read from the external SSD that adding StageCache overhead may not be worth it. That overhead includes:

```text
copying the file during staging
renaming the original
creating a symlink
resolving the symlink at runtime
tracking it in the journal
restoring it afterward
```

To address this, StageCache estimates whether staging a file is expected to save enough read time to justify touching it.

This avoids unnecessary small-file staging while still allowing small files to be selected if they are read frequently enough.

---

## Planned V2: Multi-run profiling

The current version analyzes one profile at a time. This works for basic testing, but a single run can overfit to one workload, area, route, project, or startup sequence.

A planned V2 improvement is multi-run profiling:

```text
profile run 1
profile run 2
profile run 3
        ↓
merged profile
        ↓
candidate analysis
        ↓
better candidates.json
```

The goal is to track:

```text
total read count across runs
total write count across runs
seen_in_runs
run_presence_ratio
per-run access stats
confidence across sessions
```

This would help StageCache identify files that are consistently useful instead of selecting files that only appeared during one specific run.

---

## Planned V2: App fit analysis

V2 is also planned to include an app-fit analyzer that estimates how effective StageCache is likely to be for a selected program before staging anything.

The idea is to walk the program’s external storage directory and inspect the file layout:

```text
total file count
total program size
read-only file count
average read-only file size
median read-only file size
largest files
percentage of files that fit within the selected budget
percentage of total program size that can fit within the selected budget
distribution of small, medium, and large files
```

This would produce a rough **StageCache effectiveness score** for the selected app.

StageCache is likely to work better when a program has many read-only files or medium to small-sized chunks that fit within the user’s staging budget.

```text
High-fit example:
Many 10 MB–500 MB read-only chunks
Large percentage of hot candidates can fit inside budget
StageCache has many flexible staging choices
```

```text
Low-fit example:
One or two massive 40 GB–80 GB archives
Small percentage of useful data can fit inside budget
StageCache has limited room to optimize without staging most of the program
```

A possible V2 command could look like:

```bash
python3 stagecache_fit.py \
  --app-root "/Volumes/ExternalSSD/Path/To/Program" \
  --budget-gb 5
```

Example output:

```text
StageCache fit score: 72/100

Total program size: 86.4 GB
Read-only files: 1,248
Average read-only file size: 42 MB
Median read-only file size: 8 MB
Files fitting within budget individually: 96%
Total read-only data fitting within budget: 5.8%
Layout assessment: medium-to-high fit

Reason:
This program has many small and mid-sized read-only files, giving StageCache enough flexibility to build a useful staging set under the selected budget.
```

For programs with large average asset chunks, the score would be lower because StageCache becomes more budget-dependent. This does not mean the program cannot benefit, but it tells the user that a larger budget or more targeted profiling may be needed.

---

## Planned V2: Large archive limitations

V2 is planned to reduce budget-dependence by improving candidate identification.

Instead of relying only on large archive files, StageCache should look for every smaller or mid-sized file that can still provide a measurable improvement. Even if the largest hot archives are too expensive to stage, there may still be support files, headers, indexes, metadata files, runtime libraries, shader files, smaller asset packs, or other read-heavy files that are worth staging.

The goal is to find:

```text
small files that are read often enough to matter
medium files with strong impact-to-size ratios
support/index files that reduce repeated external reads
large files only when their benefit justifies the budget
```

This helps StageCache produce a useful boost even when the biggest asset containers are too large to fit comfortably inside the staging budget.

The long-term goal is not to stage the largest files by default. It is to build the best possible staging set under the user’s budget.

---

## Planned V2 features

```text
multi-run profile merging
confidence scoring across runs
runtime-vs-startup classification
smarter large archive selection
dynamic small-file filtering
subpar-sized file discovery
app-fit effectiveness scoring
budget coverage analysis
automatic stage → launch → restore wrapper
checksum verification
stronger restore safety checks
better benchmark logging
```

---

## Example workflow

```bash
# 1. Capture a profile
sudo python3 stagecache_monitor.py \
  --process "process_name" \
  --output "profiles/run1.json"

# 2. Analyze the profile
python3 stagecache_analyze.py \
  --profile "profiles/run1.json" \
  --output "candidates.json" \
  --budget-gb 5 \
  --external-root "/Volumes/ExternalSSD"

# 3. Dry run staging
python3 stagecache_stage.py \
  --candidates "candidates.json" \
  --cache-root "$HOME/Library/Caches/StageCache" \
  --journal "journals/stagecache_journal.json"

# 4. Apply staging
python3 stagecache_stage.py \
  --candidates "candidates.json" \
  --cache-root "$HOME/Library/Caches/StageCache" \
  --journal "journals/stagecache_journal.json" \
  --apply

# 5. Restore after runtime/testing
python3 stagecache_stage.py \
  --restore \
  --journal "journals/stagecache_journal.json" \
  --apply
```

---

## ⚖️ Licensing & Commercial Use

This repository is dual-licensed to accommodate both community and commercial users:

1. **Open Source & Personal Track**: Free for personal use, education, and open-source projects under the terms of the **Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)** license.
2. **Commercial & Proprietary Track**: Any commercial use, including internal business use, SaaS deployment, or embedding this code into commercial software, requires a separate **Paid Commercial License Agreement**.

### 💼 Get a Commercial License

If you want to bypass the non-commercial restrictions, avoid open-sourcing your proprietary modifications, or require custom enterprise terms, please contact me:

📩 **Contact:** devshah1066@gmail.com  
💬 **Subject Line:** Commercial License Request - [Your Company Name]
