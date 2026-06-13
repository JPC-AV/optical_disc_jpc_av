#!/usr/bin/env python3

"""
verifyiso.py - Collection Fixity Verification for ISO Preservation Masters

Re-hashes existing ISO files and compares them against the source hashes
recorded in their makeiso.py manifests — auditing the whole collection
without reloading a single disc.

Why this works even for ISOs made by older makeiso.py versions: the
manifest's `source_hash` was always computed by streaming the optical disc
itself during imaging, so it is a valid fixity baseline regardless of tool
version. Newer manifests also record `sha256_source_hash`, which is compared
too when present.

Read-only: this script never modifies the collection. No sudo needed.

Usage:
  python3 verifyiso.py /path/to/collection [more/paths ...]
  python3 verifyiso.py /Volumes/Archive --csv report.csv

Exit codes:
  0  every checked ISO matched its recorded hashes
  1  at least one mismatch, missing ISO, or read error
"""

import argparse
import csv
import datetime
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# ANSI color only when stdout is a terminal and NO_COLOR is unset
ANSI_ENABLED = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
COLOR = {"cyan": "\033[96m", "green": "\033[92m", "yellow": "\033[93m",
         "red": "\033[91m", "off": "\033[0m"}


def colorize(color: str, text: str) -> str:
    if not ANSI_ENABLED:
        return text
    return f"{COLOR[color]}{text}{COLOR['off']}"


@dataclass
class CheckResult:
    manifest: Path
    status: str                      # PASS | FAIL | MISSING_ISO | SKIPPED | ERROR
    iso: Optional[Path] = None
    size_bytes: int = 0
    run_id: Optional[str] = None
    md5_expected: Optional[str] = None
    md5_actual: Optional[str] = None
    sha256_expected: Optional[str] = None
    sha256_actual: Optional[str] = None
    detail: Optional[str] = None
    seconds: float = 0.0
    failed_algorithms: List[str] = field(default_factory=list)


def find_preservation_manifests(roots: List[Path]) -> List[Path]:
    """Find makeiso.py preservation manifests (not access-copy or skip
    manifests, which belong to makeiso-video.py)"""
    manifests = []
    for root in roots:
        if root.is_file() and root.name.endswith("_manifest.json"):
            manifests.append(root)
            continue
        manifests.extend(sorted(root.rglob("*_manifest.json")))
    # Identity check happens at load time (backup_metadata key); here we just
    # exclude the obviously-other families by name to keep the scan readable
    return [m for m in manifests
            if not m.name.endswith(("_access_manifest.json", "_skip_manifest.json",
                                    "_aborted_manifest.json", "_error_manifest.json"))]


def hash_file(path: Path, progress_label: str = "") -> tuple:
    """One read, both digests. Returns (md5, sha256)."""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    last_tick = time.time()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            md5.update(chunk)
            sha256.update(chunk)
            done += len(chunk)
            if ANSI_ENABLED and total and time.time() - last_tick >= 0.2:
                last_tick = time.time()
                pct = done * 100 // total
                sys.stdout.write(f"\r  hashing {progress_label}: {pct}%   ")
                sys.stdout.flush()
    if ANSI_ENABLED:
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()
    return md5.hexdigest(), sha256.hexdigest()


def check_manifest(manifest_path: Path) -> CheckResult:
    """Verify one ISO against its manifest"""
    result = CheckResult(manifest=manifest_path, status="ERROR")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        result.detail = f"unreadable manifest: {e}"
        return result

    # Only makeiso.py preservation manifests carry backup_metadata
    if "backup_metadata" not in manifest or "integrity_verification" not in manifest:
        result.status = "SKIPPED"
        result.detail = "not a preservation manifest"
        return result

    result.run_id = manifest.get("backup_metadata", {}).get("run_id")
    overall = manifest.get("backup_status", {}).get("overall_status", "success")
    if overall not in ("success", "success_with_warnings"):
        result.status = "SKIPPED"
        result.detail = f"run status was '{overall}' — no verified master to check"
        return result

    integrity = manifest.get("integrity_verification", {})
    result.md5_expected = integrity.get("source_hash")
    result.sha256_expected = integrity.get("sha256_source_hash")
    if not result.md5_expected and not result.sha256_expected:
        result.status = "SKIPPED"
        result.detail = "manifest records no source hash"
        return result

    iso_name = manifest.get("output_files", {}).get("iso_filename")
    if not iso_name:
        result.detail = "manifest records no ISO filename"
        return result
    result.iso = manifest_path.parent / iso_name
    if not result.iso.exists():
        result.status = "MISSING_ISO"
        result.detail = f"ISO not found next to manifest: {iso_name}"
        return result

    result.size_bytes = result.iso.stat().st_size
    started = time.time()
    try:
        result.md5_actual, result.sha256_actual = hash_file(result.iso, result.iso.name)
    except OSError as e:
        result.detail = f"could not read ISO: {e}"
        return result
    result.seconds = time.time() - started

    if result.md5_expected and result.md5_actual != result.md5_expected:
        result.failed_algorithms.append("MD5")
    if result.sha256_expected and result.sha256_actual != result.sha256_expected:
        result.failed_algorithms.append("SHA-256")

    if result.failed_algorithms:
        result.status = "FAIL"
        result.detail = f"hash mismatch ({', '.join(result.failed_algorithms)})"
    else:
        result.status = "PASS"
        algorithms = "MD5+SHA-256" if result.sha256_expected else "MD5"
        result.detail = f"verified ({algorithms})"
    return result


def write_csv(results: List[CheckResult], csv_path: Path):
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["status", "iso", "manifest", "size_bytes", "run_id",
                         "md5_expected", "md5_actual",
                         "sha256_expected", "sha256_actual",
                         "detail", "hash_seconds", "checked_at"])
        checked_at = datetime.datetime.now().isoformat(timespec="seconds")
        for r in results:
            writer.writerow([r.status, str(r.iso) if r.iso else "", str(r.manifest),
                             r.size_bytes, r.run_id or "",
                             r.md5_expected or "", r.md5_actual or "",
                             r.sha256_expected or "", r.sha256_actual or "",
                             r.detail or "", f"{r.seconds:.1f}", checked_at])


STATUS_COLOR = {"PASS": "green", "FAIL": "red", "MISSING_ISO": "red",
                "ERROR": "red", "SKIPPED": "yellow"}


def main():
    parser = argparse.ArgumentParser(
        description="Verify ISO preservation masters against their manifest hashes (read-only)")
    parser.add_argument("paths", nargs="+", type=Path,
                        help="Collection directories (searched recursively) or manifest files")
    parser.add_argument("--csv", type=Path, metavar="PATH",
                        help="Write a CSV report of every check")
    args = parser.parse_args()

    roots = [p.expanduser() for p in args.paths]
    for root in roots:
        if not root.exists():
            sys.exit(f"Path not found: {root}")

    manifests = find_preservation_manifests(roots)
    if not manifests:
        sys.exit("No preservation manifests (*_manifest.json) found under the given paths.")

    print(colorize("cyan", f"Found {len(manifests)} manifest(s) to examine\n"))

    results: List[CheckResult] = []
    started = time.time()
    for i, manifest_path in enumerate(manifests, 1):
        print(colorize("cyan", f"[{i}/{len(manifests)}] {manifest_path.parent.name}/{manifest_path.name}"))
        r = check_manifest(manifest_path)
        results.append(r)
        line = f"  {r.status}: {r.detail or ''}"
        if r.status == "PASS" and r.seconds:
            speed = (r.size_bytes / (1024 * 1024)) / r.seconds if r.seconds else 0
            line += f" — {r.size_bytes / (1024*1024):.0f} MB in {r.seconds:.1f}s ({speed:.0f} MB/s)"
        print(colorize(STATUS_COLOR.get(r.status, "yellow"), line))

    elapsed = time.time() - started
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    print()
    print(colorize("cyan", "=" * 60))
    print(colorize("cyan", f"Checked {len(results)} manifest(s) in {elapsed:.0f}s"))
    for status in ("PASS", "FAIL", "MISSING_ISO", "ERROR", "SKIPPED"):
        if counts.get(status):
            print(colorize(STATUS_COLOR[status], f"  {status}: {counts[status]}"))
    bad = [r for r in results if r.status in ("FAIL", "MISSING_ISO", "ERROR")]
    if bad:
        print()
        print(colorize("red", "Items needing attention:"))
        for r in bad:
            print(colorize("red", f"  {r.iso or r.manifest}: {r.status} — {r.detail}"))

    if args.csv:
        write_csv(results, args.csv)
        print(colorize("cyan", f"\nCSV report written to: {args.csv}"))

    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
