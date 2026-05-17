#!/usr/bin/env python3
"""
StageCache - Profile Analyzer

Goal:
- Read a StageCache profile.json produced by stagecache_monitor.py
- Keep only safe candidate files under a target external SSD root
- Score candidates using impact-to-size ratio
- Select the best candidates under a user-provided storage budget
- Write a clean candidates.json file

This script does NOT:
- move files
- create symlinks
- modify the target app/game

Formula:
    size_mb = size_bytes / 1,048,576
    score = read_count / max(size_mb, 1)

Why max(size_mb, 1)?
- Prevents tiny files from getting absurdly inflated scores just because their
  size is close to zero.

Usage:
    python3 stagecache_analyze.py \
        --profile profile.json \
        --output candidates.json \
        --budget-gb 5 \
        --external-root "/Volumes/eSSD"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List

BYTES_PER_MB = 1024 * 1024
BYTES_PER_GB = 1024 * 1024 * 1024


@dataclass
class CandidateFile:
    path: str
    file_name: str
    size_bytes: int
    size_mb: float
    access_count: int
    read_count: int
    write_count: int
    metadata_count: int
    other_count: int
    score: float
    operations: Dict[str, int]
    processes: Dict[str, int]


def load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Profile not found: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as error:
        print(f"Invalid JSON in profile: {error}", file=sys.stderr)
        sys.exit(1)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def normalize_root(path: str) -> str:
    """Normalize the external root so prefix matching is consistent."""
    normalized = os.path.abspath(path.rstrip("/"))
    return normalized


def is_under_root(path: str, root: str) -> bool:
    """
    True when path is equal to root or lives under root.

    This prevents /Volumes/eSSD2 from matching /Volumes/eSSD.
    """
    return path == root or path.startswith(root + "/")


def is_clean_absolute_path(path: str) -> bool:
    """
    Stage 1 monitor can capture some truncated fs_usage paths.
    For the analyzer MVP, only trust clean absolute paths.
    """
    return path.startswith("/")


def is_resource_fork_path(path: str) -> bool:
    return "/..namedfork/rsrc" in path


def is_probably_save_or_user_data(path: str) -> bool:
    """
    Conservative safety filter.
    Writable files should already be removed by write_count > 0, but some save
    files may only be read during a short profile. We still do not want them as
    cache candidates.
    """
    lowered = path.lower()

    risky_parts = (
        "/appdata/roaming/",
        "/saved games/",
        "/save/",
        "/saves/",
        "/userdata/",
        "/documents/steam/",
    )

    risky_extensions = (
        ".sl2",  # Elden Ring save file
        ".sav",
        ".save",
        ".ini",  # often config/stateful; skip for MVP safety
        ".log",
        ".tmp",
    )

    return any(part in lowered for part in risky_parts) or lowered.endswith(risky_extensions)


def should_consider_file(file_record: Dict[str, Any], external_root: str) -> bool:
    path = file_record.get("path")
    size_bytes = file_record.get("size_bytes")
    read_count = int(file_record.get("read_count") or 0)
    write_count = int(file_record.get("write_count") or 0)

    if not isinstance(path, str) or not path:
        return False

    if not is_clean_absolute_path(path):
        return False

    # Main rule: only consider files that live on the target external SSD/root.
    if not is_under_root(path, external_root):
        return False

    if is_resource_fork_path(path):
        return False

    if is_probably_save_or_user_data(path):
        return False

    if not isinstance(size_bytes, int):
        return False

    if size_bytes <= 0:
        return False

    if read_count <= 0:
        return False

    # Hard safety rule: avoid anything that was written during profiling.
    if write_count > 0:
        return False

    return True


def build_candidate(file_record: Dict[str, Any]) -> CandidateFile:
    path = file_record["path"]
    file_name = file_record.get("file_name") or os.path.basename(path)
    size_bytes = int(file_record["size_bytes"])
    size_mb = size_bytes / BYTES_PER_MB
    read_count = int(file_record.get("read_count") or 0)

    score = read_count / max(size_mb, 1.0)

    return CandidateFile(
        path=path,
        file_name=file_name,
        size_bytes=size_bytes,
        size_mb=round(size_mb, 3),
        access_count=int(file_record.get("access_count") or 0),
        read_count=read_count,
        write_count=int(file_record.get("write_count") or 0),
        metadata_count=int(file_record.get("metadata_count") or 0),
        other_count=int(file_record.get("other_count") or 0),
        score=round(score, 6),
        operations=dict(file_record.get("operations") or {}),
        processes=dict(file_record.get("processes") or {}),
    )


def select_candidates(candidates: List[CandidateFile], budget_bytes: int) -> List[CandidateFile]:
    """
    Greedy MVP selection.

    Sort by score descending, then add files while the total selected size stays
    under the user budget.

    Later, this could become a real knapsack optimizer, but greedy is good
    enough to test the idea.
    """
    sorted_candidates = sorted(
        candidates,
        key=lambda item: (item.score, item.read_count),
        reverse=True,
    )

    selected: List[CandidateFile] = []
    total_bytes = 0

    for candidate in sorted_candidates:
        if candidate.size_bytes > budget_bytes:
            continue

        if total_bytes + candidate.size_bytes <= budget_bytes:
            selected.append(candidate)
            total_bytes += candidate.size_bytes

    return selected


def analyze(profile_path: Path, output_path: Path, budget_gb: float, external_root_arg: str) -> None:
    if budget_gb <= 0:
        print("Budget must be greater than 0 GB.", file=sys.stderr)
        sys.exit(1)

    external_root = normalize_root(external_root_arg)

    profile = load_json(profile_path)
    files = profile.get("files")

    if not isinstance(files, list):
        print("Profile does not contain a valid 'files' list.", file=sys.stderr)
        sys.exit(1)

    budget_bytes = int(budget_gb * BYTES_PER_GB)

    considered_records = [record for record in files if should_consider_file(record, external_root)]
    all_candidates = [build_candidate(record) for record in considered_records]
    selected = select_candidates(all_candidates, budget_bytes)

    selected_total_bytes = sum(candidate.size_bytes for candidate in selected)

    output = {
        "tool": "StageCache",
        "analysis_version": 2,
        "source_profile": str(profile_path),
        "settings": {
            "budget_gb": budget_gb,
            "budget_bytes": budget_bytes,
            "external_root": external_root,
            "require_read_only": True,
            "require_clean_absolute_path": True,
            "require_under_external_root": True,
            "score_formula": "read_count / max(size_mb, 1)",
            "selection_strategy": "greedy_by_score_desc",
        },
        "summary": {
            "profile_total_files": len(files),
            "eligible_candidate_files": len(all_candidates),
            "selected_files": len(selected),
            "selected_total_bytes": selected_total_bytes,
            "selected_total_mb": round(selected_total_bytes / BYTES_PER_MB, 3),
            "selected_total_gb": round(selected_total_bytes / BYTES_PER_GB, 3),
            "budget_used_percent": round((selected_total_bytes / budget_bytes) * 100, 2),
        },
        "candidates": [asdict(candidate) for candidate in selected],
    }

    write_json(output_path, output)

    print("StageCache analysis complete.")
    print(f"Profile:              {profile_path}")
    print(f"Output:               {output_path}")
    print(f"External root:        {external_root}")
    print(f"Budget:               {budget_gb} GB")
    print(f"Profile total files:  {len(files)}")
    print(f"Eligible candidates:  {len(all_candidates)}")
    print(f"Selected files:       {len(selected)}")
    print(f"Selected size:        {round(selected_total_bytes / BYTES_PER_GB, 3)} GB")

    if selected:
        print("\nTop selected candidates:")
        for candidate in selected[:10]:
            print(
                f"  score={candidate.score:<12} "
                f"reads={candidate.read_count:<8} "
                f"size={candidate.size_mb:<10}MB "
                f"{candidate.path}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze a StageCache profile and produce cache candidates under a storage budget."
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="Input profile.json from stagecache_monitor.py.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output candidates.json file.",
    )
    parser.add_argument(
        "--budget-gb",
        type=float,
        required=True,
        help="Maximum total size of selected candidate files in GB.",
    )
    parser.add_argument(
        "--external-root",
        required=True,
        help="External SSD root to consider for candidates, for example '/Volumes/eSSD'.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    analyze(
        profile_path=Path(args.profile),
        output_path=Path(args.output),
        budget_gb=args.budget_gb,
        external_root_arg=args.external_root,
    )


if __name__ == "__main__":
    main()
