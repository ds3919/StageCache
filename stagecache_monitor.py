#!/usr/bin/env python3
"""
StageCache - Process File Access Monitor

Goal:
- Run macOS fs_usage
- Capture filesystem events for a target process name or PID
- Store both raw events and summarized per-file access records in JSON

This first version does NOT:
- score hot files
- move files
- create symlinks
- modify the target app/game
- decide what is safe to stage

Usage by process name:
    sudo python3 stagecache_monitor.py \
        --process "eldenring" \
        --output profile.json

Usage by PID:
    sudo python3 stagecache_monitor.py \
        --pid 12345 \
        --output profile.json

For Sikarugir/Wine games, useful process filters may include:
    --process "eldenring,wineserver,conhost"

Stop with Ctrl+C when you are done profiling.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class RawFSEvent:
    line_number: int
    raw_line: str
    matched_process_name: Optional[str]
    matched_pid: Optional[int]
    parsed_operation: Optional[str]
    parsed_path: Optional[str]
    parsed_access_type: str
    captured_at: float


@dataclass
class FileAccessRecord:
    path: str
    file_name: str
    size_bytes: Optional[int] = None
    access_count: int = 0
    read_count: int = 0
    write_count: int = 0
    metadata_count: int = 0
    other_count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    operations: Dict[str, int] = field(default_factory=dict)
    processes: Dict[str, int] = field(default_factory=dict)
    raw_line_numbers: List[int] = field(default_factory=list)

    def record_access(
        self,
        operation: str,
        access_type: str,
        process_name: Optional[str],
        line_number: int,
    ) -> None:
        now = time.time()
        self.last_seen = now
        self.access_count += 1
        self.operations[operation] = self.operations.get(operation, 0) + 1
        self.raw_line_numbers.append(line_number)

        if process_name:
            self.processes[process_name] = self.processes.get(process_name, 0) + 1

        if access_type == "read":
            self.read_count += 1
        elif access_type == "write":
            self.write_count += 1
        elif access_type == "metadata":
            self.metadata_count += 1
        else:
            self.other_count += 1


def classify_access_type(operation: Optional[str]) -> str:
    if not operation:
        return "other"

    op = operation.lower()

    write_keywords = (
        "wrdata",
        "write",
        "pwrite",
        "unlink",
        "rename",
        "mkdir",
        "rmdir",
        "truncate",
        "create",
        "clonefile",
        "fsync",
        "setattr",
    )

    read_keywords = (
        "rddata",
        "read",
        "pread",
    )

    metadata_keywords = (
        "rdmeta",
        "stat",
        "lstat",
        "getattr",
        "access",
        "open",
        "lookup",
    )

    if any(keyword in op for keyword in write_keywords):
        return "write"

    if any(keyword in op for keyword in read_keywords):
        return "read"

    if any(keyword in op for keyword in metadata_keywords):
        return "metadata"

    return "other"


def parse_process_from_line(line: str) -> Tuple[Optional[str], Optional[int]]:
    """
    fs_usage lines commonly end like:
        0.005085 W eldenring.exe.867088
        0.000202 W Google Chrome.32753

    The process name may contain spaces and can be truncated by fs_usage.
    """
    match = re.search(r"\s[RW]\s+(.+?)\.(\d+)\s*$", line)
    if not match:
        return None, None

    process_name = match.group(1).strip()
    pid = int(match.group(2))
    return process_name, pid


def process_matches(
    actual_process_name: Optional[str],
    actual_pid: Optional[int],
    process_filters: List[str],
    pid_filter: Optional[int],
) -> bool:
    if pid_filter is not None:
        return actual_pid == pid_filter

    if not process_filters:
        return True

    if not actual_process_name:
        return False

    actual_lower = actual_process_name.lower()
    return any(process_filter.lower() in actual_lower for process_filter in process_filters)


def normalize_path(path: str) -> str:
    path = path.strip().rstrip(",")

    # Remove APFS resource fork suffix for the logical file path.
    path = path.replace("/..namedfork/rsrc", "")

    return path


def looks_like_latency_token(token: str) -> bool:
    return bool(re.fullmatch(r"\d+\.\d+", token))


def extract_operation_and_path(line: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Best-effort parser for fs_usage output.

    Important observed format:
        TIME OPERATION D=... B=... /dev/diskXsY ACTUAL PATH WITH SPACES LATENCY R/W PROCESS.PID

    The actual path may contain spaces, for example:
        /Users/devshah/Applications/Sikarugir/Elden Ring.app/...

    So we cannot just split on spaces and take one token after /dev/disk.
    Instead, we:
      1. read the operation token
      2. find the /dev/disk token
      3. capture everything after it until the ending "latency R/W process.pid" block
    """

    if not line:
        return None, None

    tokens = line.split()
    if len(tokens) < 2:
        return None, None

    operation = re.sub(r"\[.*?\]", "", tokens[1])

    disk_index = None
    for index, token in enumerate(tokens):
        if token.startswith("/dev/disk"):
            disk_index = index
            break

    if disk_index is None:
        # Fallback for events that have an absolute path but no disk token.
        fallback_match = re.search(r"(/(?:Volumes|Users|System|Library|private|Applications)/.+?)\s+\d+\.\d+\s+[RW]\s+.+?\.\d+\s*$", line)
        if fallback_match:
            return operation, normalize_path(fallback_match.group(1))
        return operation, None

    path_start = disk_index + 1

    if path_start >= len(tokens):
        return operation, None

    # Find the trailing latency token that is followed by R/W and process.pid.
    path_end = None
    for index in range(path_start, len(tokens) - 2):
        if looks_like_latency_token(tokens[index]) and tokens[index + 1] in {"R", "W"}:
            path_end = index
            break

    if path_end is None:
        # Fallback: capture the next token only.
        candidate = tokens[path_start]
    else:
        candidate = " ".join(tokens[path_start:path_end])

    candidate = normalize_path(candidate)

    # Device-only metadata events are not useful file candidates.
    if candidate.startswith("/dev/disk"):
        return operation, candidate

    return operation, candidate or None


def get_file_size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def build_fs_usage_command() -> List[str]:
    # -w forces wide output.
    # -f filesys limits output to filesystem events.
    # Process filtering is done in Python because Sikarugir/Wine process names can vary.
    return ["fs_usage", "-w", "-f", "filesys"]


def write_profile(
    output_path: Path,
    process_filters: List[str],
    pid_filter: Optional[int],
    started_at: float,
    records: Dict[str, FileAccessRecord],
    raw_events: List[RawFSEvent],
) -> None:
    ended_at = time.time()

    files = sorted(
        records.values(),
        key=lambda record: (-record.access_count, record.path),
    )

    profile = {
        "tool": "StageCache",
        "profile_version": 3,
        "capture_mode": "process_filtered_fs_usage",
        "process_filters": process_filters,
        "pid_filter": pid_filter,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(ended_at - started_at, 3),
        "total_raw_events": len(raw_events),
        "total_unique_files": len(records),
        "files": [asdict(record) for record in files],
        "raw_events": [asdict(event) for event in raw_events],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)


def monitor(process_filters: List[str], pid_filter: Optional[int], output_path: Path) -> None:
    started_at = time.time()
    records: Dict[str, FileAccessRecord] = {}
    raw_events: List[RawFSEvent] = []
    has_written_profile = False

    command = build_fs_usage_command()

    print("StageCache monitor started.")
    print("Mode:        process-filtered fs_usage capture")
    print(f"Processes:   {process_filters if process_filters else '(all)'}")
    print(f"PID:         {pid_filter if pid_filter is not None else '(none)'}")
    print(f"Output:      {output_path}")
    print(f"Command:     {' '.join(command)}")
    print("\nLaunch/load into the workload, use it lightly, then press Ctrl+C here.\n")

    fs_process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def write_once_and_exit(exit_code: int = 0) -> None:
        nonlocal has_written_profile
        if not has_written_profile:
            has_written_profile = True
            write_profile(output_path, process_filters, pid_filter, started_at, records, raw_events)
            print(f"Profile written to: {output_path}")
            print(f"Raw matching events recorded: {len(raw_events)}")
            print(f"Unique files recorded: {len(records)}")
        sys.exit(exit_code)

    def shutdown_handler(signum, frame):  # type: ignore[no-untyped-def]
        print("\nStopping monitor and writing profile...")
        if fs_process.poll() is None:
            fs_process.terminate()
        write_once_and_exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        assert fs_process.stdout is not None

        source_line_number = 0
        for line in fs_process.stdout:
            source_line_number += 1
            line = line.rstrip("\n")

            actual_process_name, actual_pid = parse_process_from_line(line)
            if not process_matches(actual_process_name, actual_pid, process_filters, pid_filter):
                continue

            operation, path = extract_operation_and_path(line)
            access_type = classify_access_type(operation)

            raw_event = RawFSEvent(
                line_number=source_line_number,
                raw_line=line,
                matched_process_name=actual_process_name,
                matched_pid=actual_pid,
                parsed_operation=operation,
                parsed_path=path,
                parsed_access_type=access_type,
                captured_at=time.time(),
            )
            raw_events.append(raw_event)

            if not path:
                continue

            if path.startswith("/dev/disk"):
                continue

            file_name = os.path.basename(path)
            record = records.get(path)

            if record is None:
                record = FileAccessRecord(
                    path=path,
                    file_name=file_name,
                    size_bytes=get_file_size(path),
                )
                records[path] = record
                print(f"[new] {path}")

            record.record_access(
                operation=operation or "unknown",
                access_type=access_type,
                process_name=actual_process_name,
                line_number=source_line_number,
            )

    finally:
        if fs_process.poll() is None:
            fs_process.terminate()
        if not has_written_profile:
            write_profile(output_path, process_filters, pid_filter, started_at, records, raw_events)
            print(f"Profile written to: {output_path}")
            print(f"Raw matching events recorded: {len(raw_events)}")
            print(f"Unique files recorded: {len(records)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor filesystem accesses for a target process and save results to JSON."
    )

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--process",
        help="Comma-separated process-name substrings to match, for example 'eldenring,wineserver,conhost'.",
    )
    target.add_argument(
        "--pid",
        type=int,
        help="PID to match if the process is already running.",
    )

    parser.add_argument(
        "--output",
        required=True,
        help="JSON file to write the access profile to.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if os.geteuid() != 0:
        print("This script should be run with sudo because fs_usage usually requires elevated privileges.")
        print("Examples:")
        print(f"  sudo python3 {sys.argv[0]} --process 'eldenring,wineserver,conhost' --output profile.json")
        print(f"  sudo python3 {sys.argv[0]} --pid 12345 --output profile.json")
        sys.exit(1)

    process_filters = []
    if args.process:
        process_filters = [item.strip() for item in args.process.split(",") if item.strip()]

    monitor(
        process_filters=process_filters,
        pid_filter=args.pid,
        output_path=Path(args.output),
    )


if __name__ == "__main__":
    main()
