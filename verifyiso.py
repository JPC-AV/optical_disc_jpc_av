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

It never changes an existing file. --csv creates one NEW report file: the
path is reserved before anything is hashed, and an existing path (file,
directory, symlink, /dev/stdout) or a name ending in .iso or _manifest.json
is refused. No sudo needed.

Usage:
  python3 verifyiso.py /path/to/collection [more/paths ...]
  python3 verifyiso.py /Volumes/Archive --csv fixity_20261024.csv

Exit codes:
  0    every checked ISO matched its recorded hashes
  1    at least one mismatch, missing ISO, unverifiable manifest or error;
       or the --csv report was refused or could not be written
  130  interrupted with Ctrl-C (an unfinished report is removed)

Manifests are classified by content, not by file name: a makeiso.py
preservation record is checked; a makeiso-video.py access-copy record is
SKIPPED, as is a macOS AppleDouble (._*) metadata file. A preservation
record is UNVERIFIABLE (and fails the audit) when it cannot be trusted to
verify: missing or unrecognized run status, missing integrity record,
missing MD5 source hash, a blank, non-text or malformed hash (including a
declared SHA-256 with no SHA-256 hash), wrong field types, an ISO filename
that tries to escape the manifest's own directory, a dry-run record lying
next to an existing ISO (a dry run replaced that master's own manifest), or
markers of both record families at once. A *_manifest.json that is neither
family is UNVERIFIABLE too. These never silently pass. Explicitly failed
runs and dry runs with no ISO are SKIPPED.
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
from typing import List, Optional, Tuple

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
    status: str                      # PASS | FAIL | MISSING_ISO | UNVERIFIABLE | SKIPPED | ERROR
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


# Top-level keys only makeiso.py preservation manifests carry (any one marks
# the record as preservation evidence; these win over a conversion marker)
PRESERVATION_KEYS = frozenset({
    "backup_metadata", "backup_status", "integrity_verification", "source_disc",
    "volume_information", "operations_performed", "timing_performance",
    "structural_analysis", "quality_assurance", "technical_details"})
# Top-level keys of makeiso-video.py access-copy records (never masters)
CONVERSION_KEYS = frozenset({"conversion_metadata", "conversion_status"})
# Every overall_status makeiso.py has written: verified, or explicitly no master
VERIFY_STATUSES = ("success", "success_with_warnings")
NO_MASTER_STATUSES = ("failed", "verification_failed")
KNOWN_STATUSES = VERIFY_STATUSES + NO_MASTER_STATUSES + ("dry_run",)
# macOS AppleDouble metadata files (._name) start with this magic number
APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"
HASH_LENGTHS = {"MD5": 32, "SHA-256": 64}


def find_preservation_manifests(roots: List[Path]) -> List[Path]:
    """Find every *_manifest.json under the roots. Which family a manifest
    belongs to is decided by its content in check_manifest, never by file
    name, since a master's name can be anything."""
    manifests = []
    for root in roots:
        if root.is_file() and root.name.endswith("_manifest.json"):
            manifests.append(root)
            continue
        manifests.extend(sorted(root.rglob("*_manifest.json")))
    return manifests


def _hash_shape_ok(value: str, algorithm: str) -> bool:
    """A normalized hash of the right length, in hex"""
    return len(value) == HASH_LENGTHS[algorithm] and all(c in "0123456789abcdef" for c in value)


def _normalize_hash(value) -> Optional[str]:
    """Normalize a manifest hash for comparison: hashlib emits lowercase hex,
    so strip surrounding whitespace and lowercase the recorded value too. A
    non-string or blank value normalizes to None (treated as 'no usable hash')."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


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
        raw = manifest_path.read_bytes()
    except OSError as e:
        result.detail = f"unreadable manifest: {e}"
        return result
    if raw.startswith(APPLEDOUBLE_MAGIC):
        result.status = "SKIPPED"
        result.detail = "macOS AppleDouble metadata file, not a manifest"
        return result
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        result.detail = f"manifest is not valid UTF-8 text: {e}"
        return result
    except json.JSONDecodeError as e:
        result.detail = f"manifest is not valid JSON: {e}"
        return result
    if not isinstance(manifest, dict):
        result.detail = "manifest is not a JSON object"
        return result

    # Family by content. Any makeiso.py marker makes this preservation
    # evidence; a record carrying both families' markers is damaged or
    # conflicting and must never take any benign skip.
    if PRESERVATION_KEYS & manifest.keys() and CONVERSION_KEYS & manifest.keys():
        result.status = "UNVERIFIABLE"
        result.detail = "manifest carries both makeiso.py and makeiso-video.py markers"
        return result
    if not PRESERVATION_KEYS & manifest.keys():
        if CONVERSION_KEYS & manifest.keys():
            result.status = "SKIPPED"
            result.detail = "makeiso-video access-copy record, not a preservation master"
        else:
            result.status = "UNVERIFIABLE"
            result.detail = "unrecognized manifest: neither a makeiso.py nor a makeiso-video.py record"
        return result

    def unverifiable(detail: str) -> CheckResult:
        result.status = "UNVERIFIABLE"
        result.detail = detail
        return result

    metadata = manifest.get("backup_metadata", {})
    if not isinstance(metadata, dict):
        return unverifiable("manifest backup_metadata is not an object")
    run_id = metadata.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        return unverifiable("manifest run_id is not text")
    result.run_id = run_id
    dry_run_flag = metadata.get("dry_run", False)
    if not isinstance(dry_run_flag, bool):
        return unverifiable("manifest dry_run flag is not true/false")

    # Fail closed: a preservation manifest that has lost its status block is
    # corrupt/truncated, not implicitly a good run. Only an explicitly recorded
    # non-success run is a legitimate skip; an unrecognized status is not.
    backup_status = manifest.get("backup_status")
    overall = backup_status.get("overall_status") if isinstance(backup_status, dict) else None
    if not isinstance(overall, str):
        return unverifiable("manifest missing backup_status/overall_status — cannot trust this master")

    # The status must be one makeiso.py writes before any skip is considered,
    # dry-run flag or not (main-era dry runs recorded "success" + dry_run)
    if overall not in KNOWN_STATUSES:
        return unverifiable(f"unrecognized run status {overall!r} — cannot trust this master")
    dry_run = overall == "dry_run" or dry_run_flag
    if not dry_run and overall in NO_MASTER_STATUSES:
        result.status = "SKIPPED"
        result.detail = f"run status was '{overall}' — no verified master to check"
        return result

    # iso_filename is attacker-controllable manifest data; it must name a sibling
    # file, never traverse out of the manifest's own directory.
    output_files = manifest.get("output_files")
    if not isinstance(output_files, dict):
        return unverifiable("manifest output_files is missing or not an object")
    iso_name = output_files.get("iso_filename")
    if iso_name is None or iso_name == "":
        return unverifiable("manifest records no ISO filename")
    if not isinstance(iso_name, str):
        return unverifiable("manifest ISO filename is not text")
    if iso_name != Path(iso_name).name or os.path.isabs(iso_name) or iso_name == "..":
        return unverifiable(f"manifest ISO filename is not a plain sibling name: {iso_name!r}")
    result.iso = manifest_path.parent / iso_name

    if dry_run:
        # A dry run makes no master, so its record is a legitimate skip — but
        # only when no ISO sits under the name it records. Next to an existing
        # ISO it means a dry run replaced that master's own manifest.
        if os.path.lexists(result.iso):
            return unverifiable(f"dry-run record next to an existing ISO ({iso_name}) — "
                                f"this master's own manifest may have been replaced")
        result.status = "SKIPPED"
        result.detail = "dry-run record — no master was made"
        return result

    integrity = manifest.get("integrity_verification")
    if not isinstance(integrity, dict):
        return unverifiable("manifest has no integrity_verification record")
    # Distinguish "key absent" (legitimately hash-free) from "key present but
    # blank/invalid" (corrupt or tampered manifest — must not pass silently).
    # Every makeiso.py version records the MD5 source hash (older ones only
    # that), so a missing one means the record is damaged. A SHA-256 key is
    # optional unless the record declares the algorithm.
    md5_present = "source_hash" in integrity
    sha_present = "sha256_source_hash" in integrity
    result.md5_expected = _normalize_hash(integrity.get("source_hash"))
    result.sha256_expected = _normalize_hash(integrity.get("sha256_source_hash"))
    if not md5_present:
        # A preservation manifest with no usable hash cannot be verified; this
        # is a finding, not a benign skip — it must affect the exit code.
        return unverifiable("manifest records no MD5 source hash")
    if not result.md5_expected or (sha_present and not result.sha256_expected):
        return unverifiable("manifest records a blank or non-text hash")
    if not _hash_shape_ok(result.md5_expected, "MD5") or (
            result.sha256_expected and not _hash_shape_ok(result.sha256_expected, "SHA-256")):
        return unverifiable("manifest records a malformed hash (wrong length or not hex)")
    algorithms = integrity.get("hash_algorithms", [])
    if not isinstance(algorithms, list):
        return unverifiable("manifest hash_algorithms is not a list")
    if "SHA-256" in algorithms and not sha_present:
        return unverifiable("manifest declares SHA-256 but records no SHA-256 source hash")

    # exists/stat/hash handled as one unit: a file vanishing mid-check maps
    # to MISSING_ISO, any other filesystem trouble (stale network handle,
    # permissions) to ERROR — one bad item must never stop the batch
    started = time.time()
    try:
        if not result.iso.exists():
            result.status = "MISSING_ISO"
            result.detail = f"ISO not found next to manifest: {iso_name}"
            return result
        result.size_bytes = result.iso.stat().st_size
        result.md5_actual, result.sha256_actual = hash_file(result.iso, result.iso.name)
    except FileNotFoundError:
        result.status = "MISSING_ISO"
        result.detail = f"ISO disappeared during check: {iso_name}"
        return result
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


class ReportError(Exception):
    """The --csv report path cannot be used; nothing has been written"""


def reserve_report(path: Path) -> Tuple[int, Tuple[int, int]]:
    """Claim the report path before anything is hashed, so a bad destination
    fails fast and no existing file can ever be replaced. The path is used
    exactly as given, never resolved: an existing entry of any kind — file,
    directory, symlink (even a dangling one), /dev/stdout — is refused, as is
    a name that looks like an ISO or a manifest. Returns (fd, identity) of
    the new, empty file this run now owns."""
    if path.name.lower().endswith((".iso", "_manifest.json")):
        raise ReportError(f"refusing --csv {path}: the name looks like an ISO or a manifest")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise ReportError(f"refusing --csv {path}: it already exists "
                          f"(the report must be a new file)") from None
    except OSError as e:
        raise ReportError(f"cannot create --csv report {path}: {e.strerror or e}") from None
    try:
        st = os.fstat(fd)
    except BaseException:
        os.close(fd)
        os.unlink(path)
        raise
    return fd, (st.st_dev, st.st_ino)


def _report_still_ours(path: Path, identity: Tuple[int, int]) -> bool:
    """True while the report path still names the file this run created"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == identity


def write_csv(results: List[CheckResult], out):
    writer = csv.writer(out)
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
                "UNVERIFIABLE": "red", "ERROR": "red", "SKIPPED": "yellow"}


def main():
    parser = argparse.ArgumentParser(
        description="Verify ISO preservation masters against their manifest hashes "
                    "(never changes an existing file)")
    parser.add_argument("paths", nargs="+", type=Path,
                        help="Collection directories (searched recursively) or manifest files")
    parser.add_argument("--csv", type=Path, metavar="PATH",
                        help="Write a CSV report of every check to a NEW file; refused before "
                             "anything is hashed if PATH already exists or is named like an "
                             ".iso or _manifest.json file")
    args = parser.parse_args()

    roots = [p.expanduser() for p in args.paths]
    for root in roots:
        if not root.exists():
            sys.exit(f"Path not found: {root}")

    report_path = args.csv.expanduser() if args.csv else None
    report_fd = None
    report_identity = None
    report_written = False
    if report_path:
        try:
            report_fd, report_identity = reserve_report(report_path)
        except ReportError as e:
            print(colorize("red", str(e)))
            sys.exit(1)

    try:
        manifests = find_preservation_manifests(roots)
        if not manifests:
            sys.exit("No manifests (*_manifest.json) found under the given paths.")

        print(colorize("cyan", f"Found {len(manifests)} manifest(s) to examine\n"))

        results: List[CheckResult] = []
        started = time.time()
        for i, manifest_path in enumerate(manifests, 1):
            print(colorize("cyan", f"[{i}/{len(manifests)}] {manifest_path.parent.name}/{manifest_path.name}"))
            # One bad item must never stop the batch: anything unexpected
            # becomes an ERROR for this manifest and the next one is checked
            try:
                r = check_manifest(manifest_path)
            except Exception as e:
                r = CheckResult(manifest=manifest_path, status="ERROR",
                                detail=f"could not check this manifest: {type(e).__name__}: {e}")
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
        for status in ("PASS", "FAIL", "MISSING_ISO", "UNVERIFIABLE", "ERROR", "SKIPPED"):
            if counts.get(status):
                print(colorize(STATUS_COLOR[status], f"  {status}: {counts[status]}"))
        # UNVERIFIABLE is a finding, not a benign skip: a master that should carry a
        # checkable hash but doesn't must not let the audit exit 0.
        bad = [r for r in results if r.status in ("FAIL", "MISSING_ISO", "UNVERIFIABLE", "ERROR")]
        if bad:
            print()
            print(colorize("red", "Items needing attention:"))
            for r in bad:
                print(colorize("red", f"  {r.iso or r.manifest}: {r.status} — {r.detail}"))

        if report_path:
            # Write only into the file this run reserved; if something else
            # has taken its place, leave that alone and report the failure
            if not _report_still_ours(report_path, report_identity):
                print(colorize("red", f"\nCould not write CSV report: {report_path} was replaced "
                                      f"by another file during the run (left untouched)"))
                sys.exit(1)
            try:
                # The descriptor stays ours (closefd=False) until the report is
                # complete, so cleanup can still prove which file is ours
                with os.fdopen(report_fd, "w", encoding="utf-8", newline="", closefd=False) as out:
                    write_csv(results, out)
                fd, report_fd = report_fd, None
                os.close(fd)
            except Exception as e:
                # The report is part of the deliverable for a scheduled audit
                print(colorize("red", f"\nCould not write CSV report: {e}"))
                sys.exit(1)
            report_written = True
            print(colorize("cyan", f"\nCSV report written to: {report_path}"))

        sys.exit(1 if bad else 0)
    except KeyboardInterrupt:
        print(colorize("yellow", "\nInterrupted by operator (Ctrl-C)"
                                 + ("" if report_written else " — verification incomplete"
                                    + (", no report written" if report_path else ""))))
        sys.exit(130)
    finally:
        # An unfinished report never survives the run: remove it, but only
        # while its path still names the file this run created (checked while
        # the descriptor is still open, so its identity cannot be reused)
        if report_path and not report_written:
            if _report_still_ours(report_path, report_identity):
                try:
                    os.unlink(report_path)
                except OSError:
                    pass
            if report_fd is not None:
                try:
                    os.close(report_fd)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
