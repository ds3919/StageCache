#!/usr/bin/env python3
"""
StageCache - Candidate Stager

Goal:
- Read candidates.json produced by stagecache_analyze.py
- Copy selected candidate files to an internal cache directory
- Replace original external files with symlinks to the internal cached copies
- Write a journal so the operation can be restored safely

This script DOES modify files only when --apply is passed.
Without --apply, it performs a dry run.

Important safety model:
- The analyzer should already exclude writable files.
- This stager still verifies files exist before touching them.
- It does not delete originals. It renames originals to .stagecache-original.
- It writes a journal before mutating each file.
- Restore should be run after testing or if something goes wrong.

Usage dry run:
    python3 stagecache_stage.py \
        --candidates candidates.json \
        --cache-root "$HOME/Library/Caches/StageCache" \
        --journal journals/eldenring_stage_journal.json

Usage apply:
    python3 stagecache_stage.py \
        --candidates candidates.json \
        --cache-root "$HOME/Library/Caches/StageCache" \
        --journal journals/eldenring_stage_journal.json \
        --apply

Restore:
    python3 stagecache_stage.py \
        --restore \
        --journal journals/eldenring_stage_journal.json \
        --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class StageEntry:
    original_path: str
    cache_path: str
    backup_path: str
    size_bytes: int
    status: str = "planned"
    error: Optional[str] = None


@dataclass
class StageJournal:
    tool: str
    journal_version: int
    mode: str
    created_at: float
    updated_at: float
    candidates_file: Optional[str]
    cache_root: Optional[str]
    entries: List[StageEntry] = field(default_factory=list)


def load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as error:
        print(f"Invalid JSON: {error}", file=sys.stderr)
        sys.exit(1)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def write_journal(path: Path, journal: StageJournal) -> None:
    journal.updated_at = time.time()
    data = asdict(journal)
    write_json(path, data)


def load_journal(path: Path) -> StageJournal:
    data = load_json(path)
    entries = [StageEntry(**entry) for entry in data.get("entries", [])]
    return StageJournal(
        tool=data.get("tool", "StageCache"),
        journal_version=int(data.get("journal_version", 1)),
        mode=data.get("mode", "stage"),
        created_at=float(data.get("created_at", time.time())),
        updated_at=float(data.get("updated_at", time.time())),
        candidates_file=data.get("candidates_file"),
        cache_root=data.get("cache_root"),
        entries=entries,
    )


def safe_cache_path(cache_root: Path, original_path: Path) -> Path:
    """
    Preserve enough of the original path to avoid filename collisions.

    Example:
      original: /Volumes/eSSD/Games/ELDEN_RING/Game/Data1.bhd
      cache:    ~/Library/Caches/StageCache/Volumes/eSSD/Games/ELDEN_RING/Game/Data1.bhd
    """
    relative_parts = original_path.parts[1:] if original_path.is_absolute() else original_path.parts
    return cache_root.joinpath(*relative_parts)


def backup_path_for(original_path: Path) -> Path:
    return original_path.with_name(original_path.name + ".stagecache-original")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_candidates(candidates_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = candidates_data.get("candidates")
    if not isinstance(candidates, list):
        print("candidates.json does not contain a valid 'candidates' list.", file=sys.stderr)
        sys.exit(1)
    return candidates


def build_journal(candidates_path: Path, cache_root: Path, candidates: List[Dict[str, Any]]) -> StageJournal:
    entries: List[StageEntry] = []

    for candidate in candidates:
        original = Path(candidate.get("path", ""))
        size_bytes = int(candidate.get("size_bytes") or 0)

        if not str(original):
            continue

        cache_path = safe_cache_path(cache_root, original)
        backup_path = backup_path_for(original)

        entries.append(
            StageEntry(
                original_path=str(original),
                cache_path=str(cache_path),
                backup_path=str(backup_path),
                size_bytes=size_bytes,
            )
        )

    return StageJournal(
        tool="StageCache",
        journal_version=1,
        mode="stage",
        created_at=time.time(),
        updated_at=time.time(),
        candidates_file=str(candidates_path),
        cache_root=str(cache_root),
        entries=entries,
    )


def stage_candidates(candidates_path: Path, cache_root: Path, journal_path: Path, apply: bool) -> None:
    candidates_data = load_json(candidates_path)
    candidates = validate_candidates(candidates_data)
    journal = build_journal(candidates_path, cache_root, candidates)

    total_bytes = sum(entry.size_bytes for entry in journal.entries)

    print("StageCache staging plan")
    print(f"Candidates:   {len(journal.entries)}")
    print(f"Total size:   {round(total_bytes / (1024 ** 3), 3)} GB")
    print(f"Cache root:   {cache_root}")
    print(f"Journal:      {journal_path}")
    print(f"Apply:        {apply}")

    if not apply:
        print("\nDry run only. No files will be changed.")
        for entry in journal.entries[:20]:
            print(f"  {entry.original_path}")
            print(f"    -> {entry.cache_path}")
        if len(journal.entries) > 20:
            print(f"  ... and {len(journal.entries) - 20} more")
        return

    write_journal(journal_path, journal)

    for entry in journal.entries:
        original = Path(entry.original_path)
        cache_path = Path(entry.cache_path)
        backup = Path(entry.backup_path)

        try:
            if not original.exists():
                entry.status = "error"
                entry.error = "original file does not exist"
                write_journal(journal_path, journal)
                continue

            if original.is_symlink():
                entry.status = "error"
                entry.error = "original path is already a symlink"
                write_journal(journal_path, journal)
                continue

            if backup.exists():
                entry.status = "error"
                entry.error = "backup path already exists; possible previous unrestored stage"
                write_journal(journal_path, journal)
                continue

            cache_path.parent.mkdir(parents=True, exist_ok=True)

            print(f"Copying to cache: {original}")
            shutil.copy2(original, cache_path)
            entry.status = "copied_to_cache"
            write_journal(journal_path, journal)

            print(f"Renaming original to backup: {backup}")
            original.rename(backup)
            entry.status = "original_backed_up"
            write_journal(journal_path, journal)

            print(f"Creating symlink: {original} -> {cache_path}")
            os.symlink(cache_path, original)
            entry.status = "staged"
            write_journal(journal_path, journal)

        except Exception as error:  # noqa: BLE001 - broad because this is a safety journal path
            entry.status = "error"
            entry.error = str(error)
            write_journal(journal_path, journal)
            print(f"ERROR staging {entry.original_path}: {error}", file=sys.stderr)

    staged = sum(1 for entry in journal.entries if entry.status == "staged")
    errors = sum(1 for entry in journal.entries if entry.status == "error")

    print("\nStaging complete.")
    print(f"Staged: {staged}")
    print(f"Errors: {errors}")
    print(f"Journal written to: {journal_path}")


def restore_from_journal(journal_path: Path, apply: bool) -> None:
    journal = load_journal(journal_path)

    print("StageCache restore plan")
    print(f"Journal: {journal_path}")
    print(f"Entries: {len(journal.entries)}")
    print(f"Apply:   {apply}")

    if not apply:
        print("\nDry run only. No files will be changed.")
        for entry in journal.entries[:20]:
            print(f"  restore {entry.original_path}")
        if len(journal.entries) > 20:
            print(f"  ... and {len(journal.entries) - 20} more")
        return

    for entry in reversed(journal.entries):
        original = Path(entry.original_path)
        cache_path = Path(entry.cache_path)
        backup = Path(entry.backup_path)

        try:
            if original.is_symlink():
                print(f"Removing symlink: {original}")
                original.unlink()

            if backup.exists():
                print(f"Restoring backup: {backup} -> {original}")
                backup.rename(original)
                entry.status = "restored"
            else:
                if original.exists():
                    entry.status = "already_restored_or_untouched"
                else:
                    entry.status = "restore_warning"
                    entry.error = "backup missing and original missing"

            if cache_path.exists() and not cache_path.is_dir():
                print(f"Removing cached copy: {cache_path}")
                cache_path.unlink()

            write_journal(journal_path, journal)

        except Exception as error:  # noqa: BLE001
            entry.status = "restore_error"
            entry.error = str(error)
            write_journal(journal_path, journal)
            print(f"ERROR restoring {entry.original_path}: {error}", file=sys.stderr)

    restored = sum(1 for entry in journal.entries if entry.status in {"restored", "already_restored_or_untouched"})
    errors = sum(1 for entry in journal.entries if entry.status in {"restore_error", "restore_warning"})

    print("\nRestore complete.")
    print(f"Restored/ok: {restored}")
    print(f"Warnings/errors: {errors}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage or restore StageCache candidates.")

    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually modify files. Without this flag, the script performs a dry run.",
    )

    parser.add_argument(
        "--restore",
        action="store_true",
        help="Restore files from a journal instead of staging candidates.",
    )

    parser.add_argument(
        "--candidates",
        help="Input candidates.json from stagecache_analyze.py. Required for staging.",
    )

    parser.add_argument(
        "--cache-root",
        help="Internal SSD cache root. Required for staging.",
    )

    parser.add_argument(
        "--journal",
        required=True,
        help="Journal path used for staging or restoring.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    journal_path = Path(args.journal)

    if args.restore:
        restore_from_journal(journal_path=journal_path, apply=args.apply)
        return

    if not args.candidates:
        print("--candidates is required when staging.", file=sys.stderr)
        sys.exit(1)

    if not args.cache_root:
        print("--cache-root is required when staging.", file=sys.stderr)
        sys.exit(1)

    stage_candidates(
        candidates_path=Path(args.candidates),
        cache_root=Path(args.cache_root).expanduser(),
        journal_path=journal_path,
        apply=args.apply,
    )


if __name__ == "__main__":
    main()
