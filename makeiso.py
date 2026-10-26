#!/usr/bin/env python3

"""
Optical Disc to ISO Backup Utility - Streamlined Refactored Version
Creates bit-perfect ISO backups with verification, logging, and comprehensive metadata.
"""

import subprocess
import os
import sys
import datetime
import time
import plistlib
import hashlib
import re
import shlex
import platform
import argparse
import json
import logging
import fcntl
import signal
import stat
import pwd
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape

### === DATA STRUCTURES ===

# The artifact roles, in ArtifactSet.publish_pairs order
ARTIFACT_ROLES = ("iso", "tree", "isolyzer", "log", "manifest")

def inspect_path(path: Path) -> Tuple[Optional[os.stat_result], Optional[str]]:
    """(lstat result, None) when path exists, (None, None) when it does not
    (FileNotFoundError), or (None, reason) when the check itself failed - an
    unknown location, never taken as absence."""
    try:
        return os.lstat(path), None
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, str(e)

def regular_file_size(path: Path) -> Tuple[Optional[int], Optional[str]]:
    """(size, None) for a regular file at path, (None, None) when nothing is
    there (FileNotFoundError), or (None, reason) when it could not be checked
    or is not a regular file: unavailable, never absent."""
    here, problem = inspect_path(path)
    if problem:
        return None, problem
    if here is None:
        return None, None
    if not stat.S_ISREG(here.st_mode):
        return None, "not a regular file"
    return here.st_size, None

@dataclass(frozen=True)
class ArtifactSet:
    """The five artifacts a run can produce."""
    iso: Path
    tree: Path
    isolyzer: Path
    log: Path
    manifest: Path

    @property
    def archive_order(self) -> List[Path]:
        return [self.iso, self.log, self.manifest, self.tree, self.isolyzer]

    def publish_pairs(self, canonical: "ArtifactSet") -> List[Tuple[Path, Path]]:
        return [
            (self.iso, canonical.iso),
            (self.tree, canonical.tree),
            (self.isolyzer, canonical.isolyzer),
            (self.log, canonical.log),
            (self.manifest, canonical.manifest),
        ]


@dataclass
class BackupConfig:
    """Configuration for backup operation"""
    disk_id: str
    volume_name: str
    filename: str
    operator: str
    output_dir: Path
    dry_run: bool = False
    force: bool = False
    media_type: str = "Unknown"
    mount_point: Optional[str] = None
    # Set when a run fails; sidecar paths then resolve to failure-marked names
    # so a retry can never overwrite the failed attempt's records.
    failure_token: Optional[str] = None
    # Set while a run is in progress; ISO/tree/isolyzer writes go to private
    # attempt paths and only replace the canonical artifacts after verification.
    attempt_token: Optional[str] = None

    def _sidecar_token(self) -> str:
        token = self.failure_token or self.attempt_token
        return f"_{token}" if token else ""

    def _named(self, suffix: str) -> Path:
        token = self._sidecar_token()
        return self.output_dir / f"{self.filename}{token}{suffix}"

    @property
    def output_path(self) -> Path:
        token = f"_{self.attempt_token}" if self.attempt_token else ""
        return self.output_dir / f"{self.filename}{token}.iso"

    @property
    def canonical_output_path(self) -> Path:
        return self.output_dir / f"{self.filename}.iso"
    
    @property
    def log_path(self) -> Path:
        return self._named(".iso.log.txt")

    @property
    def canonical_log_path(self) -> Path:
        return self.output_dir / f"{self.filename}.iso.log.txt"
    
    @property
    def manifest_path(self) -> Path:
        return self._named("_manifest.json")

    @property
    def canonical_manifest_path(self) -> Path:
        return self.output_dir / f"{self.filename}_manifest.json"
    
    @property
    def tree_path(self) -> Path:
        return self._named("_tree.txt")

    @property
    def canonical_tree_path(self) -> Path:
        return self.output_dir / f"{self.filename}_tree.txt"
    
    @property
    def isolyzer_path(self) -> Path:
        return self._named("_isolyzer.xml")

    @property
    def canonical_isolyzer_path(self) -> Path:
        return self.output_dir / f"{self.filename}_isolyzer.xml"

    @property
    def artifacts(self) -> ArtifactSet:
        return ArtifactSet(
            iso=self.output_path,
            tree=self.tree_path,
            isolyzer=self.isolyzer_path,
            log=self.log_path,
            manifest=self.manifest_path,
        )

    @property
    def canonical_artifacts(self) -> ArtifactSet:
        return ArtifactSet(
            iso=self.canonical_output_path,
            tree=self.canonical_tree_path,
            isolyzer=self.canonical_isolyzer_path,
            log=self.canonical_log_path,
            manifest=self.canonical_manifest_path,
        )

@dataclass
class BackupResult:
    """Result of backup operation"""
    success: bool
    iso_path: Path
    disk_size: int
    creation_path: Optional[Path] = None
    md5_iso: Optional[str] = None
    md5_raw: Optional[str] = None
    sha256_iso: Optional[str] = None
    sha256_raw: Optional[str] = None
    creation_time: float = 0.0
    verification_time: float = 0.0
    error_message: Optional[str] = None
    unmount_ok: Optional[bool] = None  # None = unmount not attempted (dry run)
    finalized: Optional[bool] = None   # disc ejected; None = not attempted (dry run)
    eject_interrupted: bool = False    # Ctrl-C arrived during the eject
    unmount_interrupted: bool = False  # Ctrl-C arrived during the unmount
    warnings: List[str] = field(default_factory=list)  # non-fatal problems (run still valid)
    flush_method: Optional[str] = None   # as IsoCreationResult: how the ISO was flushed
    cache_bypass: Optional[bool] = None  # as IsoCreationResult: F_NOCACHE set for the re-read
    # Where this run's own tree/isolyzer/log/manifest actually are, by role,
    # once a move (publication, move-back, failure marking) - successful or
    # not - has decided it; otherwise the config-derived name applies.
    # iso_path plays the same part for the ISO.
    artifact_paths: Dict[str, Path] = field(default_factory=dict)

    @property
    def checksum_match(self) -> Optional[bool]:
        """Whether checksums match.

        False if any recorded pair mismatches; True only when both the MD5
        and SHA-256 pairs are present and match; None when verification
        never produced a complete set of hashes."""
        pairs = [(self.md5_iso, self.md5_raw), (self.sha256_iso, self.sha256_raw)]
        if any(a and b and a != b for a, b in pairs):
            return False
        if all(a and b for a, b in pairs):
            return True
        return None
    
    @property
    def total_time(self) -> float:
        return self.creation_time + self.verification_time
    
    def speed_mb_s(self, duration: float) -> float:
        """Calculate speed in MB/s"""
        if duration > 0:
            return round((self.disk_size / duration) / (1024 * 1024), 2)
        return 0.0

@dataclass
class RunContext:
    """Identity and step timing for a single backup run.

    Generated once per run and shared by the log and manifest writers so
    both records carry the same run_id/uuid and real step timestamps.
    """
    run_id: str
    run_uuid: str
    start_time: datetime.datetime
    step_times: Dict[str, datetime.datetime] = field(default_factory=dict)

    def mark(self, step: str):
        """Record the moment a step actually happened"""
        self.step_times[step] = datetime.datetime.now()

@dataclass
class IsoCreationResult:
    """Hashes and timing produced by the copy and verification passes"""
    creation_time: float
    verification_time: float
    md5_raw: str
    md5_iso: Optional[str]
    sha256_raw: str
    sha256_iso: Optional[str]
    output_path: Path
    warnings: List[str] = field(default_factory=list)  # degraded-but-not-fatal conditions
    flush_method: Optional[str] = None   # "F_FULLFSYNC", or "fsync" when that was unavailable
    cache_bypass: Optional[bool] = None  # whether F_NOCACHE was set for the verification read

@dataclass(frozen=True)
class RecordMetadata:
    """What one pass of record writing found about this run's ISO and
    isolyzer output, checked once and shared by the log and the manifest:
    the ISO's size (None when absent or unknown) and why either file could
    not be checked (None when it could)."""
    iso_size: Optional[int]
    iso_problem: Optional[str]
    isolyzer_present: bool
    isolyzer_problem: Optional[str]

    @classmethod
    def check(cls, current: "ArtifactSet") -> "RecordMetadata":
        iso_size, iso_problem = regular_file_size(current.iso)
        isolyzer, isolyzer_problem = inspect_path(current.isolyzer)
        return cls(iso_size, iso_problem, isolyzer is not None, isolyzer_problem)

@dataclass(frozen=True)
class RecordView:
    """Artifact names to write inside records (files), and where this run's
    own files actually are now (current), to stat and read."""
    files: ArtifactSet
    current: ArtifactSet

    @classmethod
    def from_run(cls, config: BackupConfig, result: BackupResult,
                 log_path: Optional[Path] = None) -> "RecordView":
        staged_success = bool(
            result.success and config.attempt_token
            and result.creation_path
            and result.creation_path != config.canonical_output_path
        )
        current = ArtifactSet(
            iso=result.iso_path,
            tree=result.artifact_paths.get("tree", config.tree_path),
            isolyzer=result.artifact_paths.get("isolyzer", config.isolyzer_path),
            log=log_path or config.log_path,
            manifest=config.manifest_path,
        )
        files = config.canonical_artifacts if staged_success else current
        return cls(files, current)

@dataclass
class BackupPreparation:
    """State gathered before any copy work starts."""
    disk_info: Dict[str, Any]
    disk_size: int
    created_output_dir: bool
    attempt: ArtifactSet

@dataclass
class CopyPhaseResult:
    """Outputs from tree listing, unmount, copy, analysis, and eject."""
    tree_ok: bool
    iso_result: Optional[IsoCreationResult]
    iso_analysis: Dict[str, Any]
    backup_error: Optional[str]
    unmount_ok: Optional[bool]
    finalized: Optional[bool]

### === COLOR UTILITIES ===

COLOR = {
    "blue": "\033[94m",
    "cyan": "\033[96m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "off": "\033[0m"
}

# ANSI output (color + progress cursor codes) only when stdout is a terminal
# and NO_COLOR is unset (https://no-color.org). Decided once at import time.
ANSI_ENABLED = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

def colorize(color: str, text: str) -> str:
    """Apply ANSI color to text (no-op when output is not a terminal)"""
    if not ANSI_ENABLED:
        return text
    return f"{COLOR[color]}{text}{COLOR['off']}"

### === LOGGING SETUP ===

def setup_logging() -> logging.Logger:
    """Setup console logging"""
    logger = logging.getLogger("iso_backup")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(stream_handler)
    return logger

# Global logger instance
logger = setup_logging()

def log(msg: str):
    """Log a message to console"""
    logger.info(msg)

def log_divider(title: Optional[str] = None):
    """Log a divider with optional title"""
    bar = "=" * 60
    log("")
    if title:
        log(colorize("blue", bar))
        log(colorize("blue", f"{title:^60}"))
        log(colorize("blue", bar))
    else:
        log(colorize("blue", bar))
    log("")

### === UTILITY FUNCTIONS ===

def get_input(prompt: str) -> str:
    """Get user input with keyboard interrupt handling"""
    try:
        return input(prompt).strip()
    except KeyboardInterrupt:
        log(colorize("red", "\nCancelled by user."))
        sys.exit(1)

def run_cmd(cmd: List[str], capture_output: bool = True, plist: bool = False) -> Any:
    """Run command and return output"""
    if plist:
        result = subprocess.run(cmd, capture_output=capture_output, check=True)
        return plistlib.loads(result.stdout)
    else:
        result = subprocess.run(cmd, capture_output=capture_output, text=True, check=True)
        return result.stdout

# Commands as run, and as shown and recorded: shown with shlex.join or
# shlex.quote, so a name with spaces, quotes, $, ` or ; stays one argument

def tree_command(config: "BackupConfig") -> List[str]:
    """The tree listing's argv"""
    return ["tree", "-RapugD", "--si", "--du",
            config.mount_point or f"/Volumes/{config.volume_name}"]

def dd_command(disk_id: str) -> List[str]:
    """dd's argv. dd only reads the raw device: makeiso.py reads dd's
    output, writes the ISO from it and hashes it as it goes"""
    return ["dd", f"if=/dev/r{disk_id}", "bs=4m"]

def spot_check_command(iso: str, disk_id: str) -> str:
    """An optional operator check that makeiso.py never runs: hash a fresh
    raw-device read and compare it with the ISO at iso"""
    return (f"iso=$(md5 -q {shlex.quote(iso)}); "
            f"disc=$({shlex.join(dd_command(disk_id))} 2>/dev/null | md5 -q); "
            f'[ "$iso" = "$disc" ] && echo MATCH || echo MISMATCH')

def flush_wording(flush_method: Optional[str]) -> str:
    """What was done to get the written ISO onto its media before verification"""
    if flush_method == "F_FULLFSYNC":
        return ("F_FULLFSYNC (asks the drive to write its cache to the media; a drive "
                "or network volume may not honour it)")
    if flush_method == "fsync":
        return ("fsync only (F_FULLFSYNC unavailable: the drive's own write cache may "
                "not have been flushed)")
    return "not recorded"

def reread_wording(cache_bypass: Optional[bool]) -> str:
    """How the written ISO was re-read for verification"""
    if cache_bypass:
        return ("written ISO re-read with F_NOCACHE set (macOS is asked not to cache "
                "the read; data the write left cached may still be served)")
    if cache_bypass is False:
        return ("written ISO re-read without F_NOCACHE (unavailable: the read may have "
                "been served from the page cache)")
    return "written ISO re-read"

def calculate_disc_speed_multiplier(speed_mb_s: float, media_type: str) -> str:
    """Calculate disc speed as Xx multiplier based on media type.

    Baselines (1x):
      CD:        150 KB/s  (0.150 MB/s)
      DVD:     1,385 KB/s  (1.385 MB/s)
      Blu-ray: 4,500 KB/s  (4.500 MB/s)
    """
    BASELINES = {
        "cd":  0.150,
        "dvd": 1.385,
        "bd":  4.500,
    }
    media_lower = media_type.lower()
    if "blu" in media_lower or "bd" in media_lower:
        baseline = BASELINES["bd"]
    elif "dvd" in media_lower:
        baseline = BASELINES["dvd"]
    else:
        baseline = BASELINES["cd"]

    if speed_mb_s <= 0:
        return "N/A"
    multiplier = speed_mb_s / baseline
    return f"{multiplier:.1f}x"

### === FORMATTED LOG WITH ISOLYZER ===

def format_duration(seconds: float) -> str:
    """Format duration as human-readable string"""
    if seconds == 0:
        return "0 seconds"
    minutes = int(seconds) // 60
    remaining_seconds = round(seconds % 60, 2)
    if minutes > 0:
        return f"{minutes}m {remaining_seconds:.2f}s"
    return f"{remaining_seconds:.2f}s"

def format_bytes(bytes_val: int) -> str:
    """Format bytes as human-readable size (1024-based, e.g. 1.5 GB)"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} TB"

def fmt_diskutil_size(bytes_val: int, total_bytes: int = None) -> str:
    """Format bytes in diskutil verbose style (1000-based GB, with byte count and optional %)"""
    if not bytes_val:
        return None
    gb = bytes_val / (1000 ** 3)
    units_512 = bytes_val // 512
    result = f"{gb:.1f} GB ({bytes_val} Bytes) (exactly {units_512} 512-Byte-Units)"
    if total_bytes:
        pct = (bytes_val / total_bytes) * 100
        result += f" ({pct:.1f}%)"
    return result

def create_formatted_log(log_path: Path, config: BackupConfig, result: BackupResult,
                        start_time: datetime.datetime, disk_info: Dict[str, Any],
                        iso_analysis: Dict[str, Any] = None, run: RunContext = None,
                        opener=None, metadata: Optional[RecordMetadata] = None):
    """Create a properly formatted log file (opener, when given, creates it:
    a dry run's exclusive creation). metadata: the run's ISO/isolyzer check
    shared with the manifest (checked here when not given)."""

    end_time = datetime.datetime.now()
    total_duration = (end_time - start_time).total_seconds()
    record = RecordView.from_run(config, result, log_path)
    if metadata is None:
        metadata = RecordMetadata.check(record.current)
    # An ISO that could not be checked: its size is unavailable, not absent
    iso_unavailable = (f"unavailable (could not check {record.current.iso.name}: "
                       f"{metadata.iso_problem})" if metadata.iso_problem else None)

    def step_time(step: str, fallback: datetime.datetime) -> str:
        """Real captured timestamp for a step, or the fallback if it never ran"""
        t = run.step_times.get(step, fallback) if run else fallback
        return t.strftime('%H:%M:%S')
    
    with (opener(log_path) if opener else open(log_path, "w")) as f:
        # === HEADER SUMMARY ===
        f.write("=" * 70 + "\n")
        f.write("OPTICAL DISC TO ISO BACKUP LOG\n")
        f.write("=" * 70 + "\n\n")
        
        f.write("BACKUP SUMMARY\n")
        f.write("-" * 20 + "\n")
        run_id = run.run_id if run else f"{start_time.strftime('%Y%m%dT%H%M%S')}_{config.operator}_{config.disk_id}"
        f.write(f"Run ID:           {run_id}\n")
        f.write(f"Operator:         {config.operator}\n")
        f.write(f"Start Time:       {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"End Time:         {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total Duration:   {format_duration(total_duration)}\n")
        if config.dry_run:
            status = 'DRY RUN'
        elif result.success:
            status = 'SUCCESS (WITH WARNINGS)' if result.warnings else 'SUCCESS'
        else:
            status = 'VERIFICATION FAILED' if result.checksum_match is False else 'FAILED'
        f.write(f"Status:           {status}\n")
        if result.error_message:
            f.write(f"Error:            {result.error_message}\n")
        for warning in result.warnings:
            f.write(f"Warning:          {warning}\n")
        if result.checksum_match is not None:
            f.write(f"Verification:     {'PASS' if result.checksum_match else 'FAIL'}\n")
        f.write("\n")
        
        # === DISC INFORMATION ===
        f.write("DISC INFORMATION\n")
        f.write("-" * 20 + "\n")
        f.write(f"Disk ID:          {config.disk_id}\n")
        f.write(f"Volume Name:      {config.volume_name}\n")
        f.write(f"Volume Size:      {result.disk_size / (1024 * 1024):.0f} MB ({result.disk_size / (1024 * 1024 * 1024):.1f} GB)\n")
        f.write(f"Media Type:       {disk_info.get('OpticalMediaType', 'Unknown')}\n")
        f.write(f"File System:      {disk_info.get('FilesystemName', disk_info.get('FilesystemType', 'Unknown'))}\n")
        f.write(f"Device:           {disk_info.get('MediaName', disk_info.get('IORegistryEntryName', 'Unknown'))}\n")
        f.write("\n")
        
        # === OUTPUT FILES ===
        f.write("OUTPUT FILES\n")
        f.write("-" * 20 + "\n")
        f.write(f"ISO File:         {record.files.iso.name}\n")
        f.write(f"Output Directory: {config.output_dir}\n")
        f.write(f"Log File:         {record.files.log.name}\n")
        f.write(f"Tree File:        {record.files.tree.name}\n")
        f.write(f"Isolyzer File:    {record.files.isolyzer.name}\n")
        f.write(f"Manifest File:    {record.files.manifest.name}\n")
        if metadata.iso_size is not None:
            f.write(f"ISO File Size:    {metadata.iso_size / (1024 * 1024):.0f} MB\n")
        elif iso_unavailable:
            f.write(f"ISO File Size:    {iso_unavailable}\n")
        f.write("\n")
        
        # Rest of the log formatting remains the same...
        # === OPERATION DETAILS ===
        f.write("OPERATION DETAILS\n")
        f.write("-" * 20 + "\n")
        
        # Tree generation
        f.write(f"[{step_time('tree', start_time)}] Tree Listing Generation\n")
        f.write(f"  Command: {shlex.join(tree_command(config))}\n")
        f.write(f"  Output:  {record.files.tree.name}\n\n")
        
        # Disk unmount
        if result.unmount_interrupted:
            unmount_status = "INTERRUPTED (Ctrl-C; the disc may or may not be unmounted)"
        elif result.unmount_ok is None:
            unmount_status = "Skipped (dry run)" if config.dry_run else "Not attempted"
        else:
            unmount_status = "Success" if result.unmount_ok else "FAILED"
        f.write(f"[{step_time('unmount', start_time)}] Disk Unmount\n")
        f.write(f"  Command: {shlex.join(['diskutil', 'unmountDisk', f'/dev/{config.disk_id}'])}\n")
        f.write(f"  Status:  {unmount_status}\n\n")
        
        # ISO Creation and Verification (real captured times; approximation
        # only as fallback for steps that never ran)
        create_start = (run.step_times.get("copy_start") if run else None) or (start_time + datetime.timedelta(seconds=1))
        create_end = (run.step_times.get("copy_end") if run else None) or (create_start + datetime.timedelta(seconds=result.creation_time))
        
        f.write(f"[{create_start.strftime('%H:%M:%S')}] ISO Creation\n")
        if config.dry_run:
            f.write(f"  Status:  Skipped (dry run)\n\n")
        elif result.unmount_ok is not True or result.creation_path is None:
            f.write(f"  Status:  Not started ({result.error_message or 'aborted before copy'})\n\n")
        else:
            creation_path = result.creation_path or config.output_path
            f.write(f"  Command: {shlex.join(dd_command(config.disk_id))}\n")
            f.write(f"  Output:  {creation_path} (written by makeiso.py from dd's output)\n")
            f.write(f"  Method:  Streamed copy with in-stream source (raw disk) hashing\n")
            if creation_path != config.canonical_output_path:
                f.write(f"  Attempt: Run-private path; promoted to {config.canonical_output_path.name} only after successful verification\n")
            if result.md5_raw is None and result.error_message:
                f.write(f"  Status:  FAILED ({result.error_message})\n\n")
            else:
                f.write(f"  Duration: {format_duration(result.creation_time)}\n")
                speed = result.speed_mb_s(result.creation_time)
                multiplier = calculate_disc_speed_multiplier(speed, config.media_type)
                f.write(f"  Speed:    {speed:.2f} MB/s ({multiplier})\n")
                f.write(f"  Completed: {create_end.strftime('%H:%M:%S')}\n\n")
        
        # Hash verification results
        if result.md5_iso and result.md5_raw:
            f.write("VERIFICATION RESULTS\n")
            f.write("-" * 20 + "\n")
            f.write(f"Method:         {reread_wording(result.cache_bypass)}, "
                    f"compared to streamed raw disk hashes\n")
            f.write(f"Flush:          {flush_wording(result.flush_method)}\n")
            f.write(f"MD5 (ISO):          {result.md5_iso}\n")
            f.write(f"MD5 (Raw Disk):     {result.md5_raw}\n")
            if result.sha256_iso and result.sha256_raw:
                f.write(f"SHA-256 (ISO):      {result.sha256_iso}\n")
                f.write(f"SHA-256 (Raw Disk): {result.sha256_raw}\n")
            algorithms = "MD5 (128-bit), SHA-256 (256-bit)" if result.sha256_iso else "MD5 (128-bit)"
            f.write(f"Match:          {'YES' if result.checksum_match else 'NO'}\n")
            f.write(f"Algorithms:     {algorithms}\n")
            f.write(f"Duration:       {format_duration(result.verification_time)}\n\n")
        
        # ISO Structure Analysis: omitted only when its output is confirmed absent
        if metadata.isolyzer_present or metadata.isolyzer_problem:
            f.write(f"[{step_time('isolyzer', create_end)}] ISO Structure Analysis\n")
            f.write(f"  Tool:    isolyzer\n")
            f.write(f"  Output:  {record.files.isolyzer.name}"
                    + (f" (could not check it: {metadata.isolyzer_problem})"
                       if metadata.isolyzer_problem else "") + "\n")
            
            if iso_analysis and 'error' not in iso_analysis:
                f.write("  Results:\n")
                f.write(f"    Contains Known File System: {iso_analysis.get('contains_known_filesystem', 'Unknown')}\n")
                f.write(f"    Valid ISO 9660:             {iso_analysis.get('valid_iso9660', 'Unknown')}\n")
                f.write(f"    Contains UDF:               {iso_analysis.get('has_udf', 'Unknown')}\n")
                f.write(f"    Expected Size:              {iso_analysis.get('size_expected', 0)} bytes\n")
                f.write(f"    Actual Size:                {iso_analysis.get('size_actual', 0)} bytes\n")
                f.write(f"    Size Difference:            {iso_analysis.get('size_difference_bytes', 0)} bytes ({iso_analysis.get('size_difference_sectors', 0)} sectors)\n")
                f.write(f"    Size as Expected:           {iso_analysis.get('size_as_expected', 'Unknown')}\n")
                f.write(f"    Smaller Than Expected:      {iso_analysis.get('smaller_than_expected', 'Unknown')}\n")
                
                # Contextual size difference note
                size_diff = iso_analysis.get('size_difference_bytes', 0)
                smaller = iso_analysis.get('smaller_than_expected', False)
                size_as_expected = iso_analysis.get('size_as_expected', True)
                
                if size_as_expected:
                    f.write(f"  Size check: ISO size matches expected size exactly.\n")
                elif smaller:
                    f.write(f"  Size check: WARNING — ISO is smaller than expected by {size_diff} bytes.\n")
                    f.write(f"  This may indicate a truncated read. Verify the ISO carefully before use.\n")
                else:
                    f.write(f"  Size check: ISO is {size_diff} bytes ({iso_analysis.get('size_difference_sectors', 0)} sectors) larger than the filesystem declares.\n")
                    f.write(f"  This is normal for optical media — extra sectors are lead-out/padding and do not indicate data loss.\n")
            
            if iso_analysis and 'error' in iso_analysis:
                f.write(f"  Status:  FAILED ({iso_analysis['error']})\n\n")
            elif iso_analysis and iso_analysis.get('skipped'):
                f.write(f"  Status:  Skipped ({iso_analysis.get('reason', 'unknown')})\n\n")
            else:
                f.write("  Status:  Complete\n\n")
        
        # Finalization
        f.write(f"[{step_time('finalize', create_end + datetime.timedelta(seconds=1))}] Finalization\n")
        f.write(f"  Eject:   {shlex.join(['diskutil', 'eject', f'/dev/{config.disk_id}'])}\n")
        if config.dry_run:
            final_status = "Skipped (dry run)"
        elif result.finalized:
            final_status = "Complete"
        elif result.eject_interrupted:
            final_status = "INTERRUPTED (Ctrl-C during the eject)"
        elif result.unmount_ok is not True:
            final_status = "Skipped (disc was never unmounted)"
        else:
            final_status = "FAILED (eject error — see console output)"
        f.write(f"  Status:  {final_status}\n\n")
        
        # === SYSTEM INFORMATION ===
        f.write("SYSTEM INFORMATION\n")
        f.write("-" * 20 + "\n")
        f.write(f"Operating System: {platform.system()} {platform.release()}\n")
        f.write(f"Machine:          {platform.machine()}\n")
        f.write(f"Python Version:   {platform.python_version()}\n")
        f.write(f"Tool Version:     v14-streamlined\n\n")
        
        # === DETAILED DISK METADATA ===
        if disk_info:
            f.write("DETAILED DISK METADATA\n")
            f.write("-" * 25 + "\n")
            
            def fmt_field(label: str, value) -> None:
                if value not in (None, '', 'Unknown'):
                    f.write(f"{label:20} {value}\n")
            
            fmt_field("Device Identifier", disk_info.get('DeviceIdentifier'))
            fmt_field("Drive Model",        disk_info.get('MediaName', disk_info.get('IORegistryEntryName')))
            fmt_field("File System",        disk_info.get('FilesystemName', disk_info.get('FilesystemType')))
            fmt_field("Connection",         disk_info.get('BusProtocol'))
            # Physical Size = actual ISO/disc size; TotalSize in plist returns volume size on optical media.
            # An ISO that could not be checked has no size here: never TotalSize, no percentage
            if iso_unavailable:
                physical_size = None
                fmt_field("Physical Size",  iso_unavailable)
            else:
                physical_size = (metadata.iso_size if metadata.iso_size is not None
                                 else disk_info.get('TotalSize'))
                fmt_field("Physical Size",  fmt_diskutil_size(physical_size))
            volume_size = disk_info.get('TotalSize')
            fmt_field("Volume Capacity",    fmt_diskutil_size(volume_size))
            fmt_field("Used Space",         fmt_diskutil_size(volume_size, physical_size))
            fmt_field("Read-Only Status",   'Yes' if disk_info.get('Writable') is False else 'No')
            fmt_field("Drive Capabilities", disk_info.get('OpticalDeviceType'))
            fmt_field("Erasable",           'Yes' if disk_info.get('OpticalMediaErasable') else 'No')
            f.write("\n")
        
        # === FOOTER ===
        f.write("=" * 70 + "\n")
        f.write(f"Log generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 70 + "\n")

class DeferredInterrupt:
    """Hold Ctrl-C (SIGINT) for one short critical section: the bundle's
    archive/publication renames and any rollback of them. A Ctrl-C that
    arrives meanwhile only sets .received, so the section can never stop
    halfway; the caller acts on it afterwards. The previous handler is always
    restored. Off the main thread (where handlers cannot be set) the section
    simply runs unprotected."""

    def __init__(self):
        self.received = False
        self._previous = None
        self._installed = False

    def _hold(self, signum, frame):
        self.received = True

    def __enter__(self) -> "DeferredInterrupt":
        try:
            self._previous = signal.signal(signal.SIGINT, self._hold)
            self._installed = True
        except ValueError:
            pass
        return self

    def __exit__(self, *exc) -> bool:
        if self._installed:
            signal.signal(signal.SIGINT, self._previous)
        return False

class DryRunStopped(Exception):
    """A dry run that cannot finish truthfully (its tree listing failed, or
    one of its file names was taken): it stops before writing anything more"""

### === MAIN BACKUP CLASS ===

class OpticalDiscBackup:
    """Main class for optical disc backup operations"""
    
    def __init__(self, config: BackupConfig):
        self.config = config
        self.run: Optional[RunContext] = None
        self._progress_marks = set()  # deciles already reported in non-TTY mode
        # Ctrl-C was received but the run's real outcome stands (eject cut
        # short after a verified copy, or Ctrl-C held while publishing); a
        # successful run then exits 130 (see main)
        self.interrupted = False
        self._eject_interrupted = False
        self._unmount_interrupted = False
        self._held_interrupt: Optional[DeferredInterrupt] = None
        self._published = False
        self._archive_dir: Optional[Path] = None  # this run's overwritten-* folder
        self._rollback_problems: List[str] = []    # files rollback could not put back
        self._dry_run_files: List[Path] = []       # files a dry run created, in order
        self._operator_uid_value: Optional[int] = None
        self._operator_uid_known = False

    def _standard_artifacts(self) -> List[Path]:
        """Canonical artifacts that a successful run publishes."""
        return self.config.canonical_artifacts.archive_order

    ### --- dry runs: exclusive creation and owned-object ownership ---

    def _operator_uid(self) -> Optional[int]:
        """The uid of SUDO_USER, the account _fix_ownership hands a normal
        run's files to; None when there is none (or it is unknown)"""
        if not self._operator_uid_known:
            self._operator_uid_known = True
            sudo_user = os.environ.get("SUDO_USER")
            if sudo_user:
                try:
                    self._operator_uid_value = pwd.getpwnam(sudo_user).pw_uid
                except KeyError:
                    log(colorize("yellow", f"Warning: unknown SUDO_USER {sudo_user!r}; "
                                           f"this dry run's files stay owned by root"))
        return self._operator_uid_value

    def _give_to_operator(self, fd: int, path: Path):
        """Change the owner of an object this run holds open (never by name)"""
        uid = self._operator_uid()
        if uid is None:
            return
        try:
            os.fchown(fd, uid, -1)
        except OSError as e:
            log(colorize("yellow", f"Warning: could not change the owner of {path}: {e}"))

    def _keep_new_dry_run_folder_owner(self, folder: Path):
        """A dry run never changes the owner of the output folder it created.
        mkdir gives no descriptor, and reopening the path cannot prove the
        folder now there is the one it made (another folder, even one with the
        same owner, could have been moved in), so the folder keeps its owner,
        with a warning. Only the files this run creates exclusively change
        owner, through their own descriptors."""
        if self._operator_uid() is None:
            return
        log(colorize("yellow", f"Warning: the output folder {folder} was created by this dry "
                               f"run and keeps its owner (root); only the files it creates are "
                               f"handed to {os.environ.get('SUDO_USER')}. Create the folder "
                               f"yourself first to manage it without sudo."))

    def _create_dry_run_file(self, path: Path, encoding: Optional[str] = None):
        """Create one of a dry run's files (tree listing, log, manifest). A dry
        run writes at the canonical names, so it only ever creates a new file
        there: mode "x" (O_CREAT|O_EXCL) never truncates an existing file and
        never follows a symlink; a taken name raises FileExistsError. The file
        counts as this run's own at once, and changes owner through its open
        descriptor."""
        f = open(path, "x", encoding=encoding)
        self._dry_run_files.append(path)
        try:
            self._give_to_operator(f.fileno(), path)
        except BaseException:
            f.close()
            raise
        return f

    def _dry_run_problem(self, error: Exception, what: str) -> str:
        if isinstance(error, FileExistsError):
            name = Path(error.filename).name if error.filename else what
            return (f"{name} appeared in {self.config.output_dir} during the dry run and was "
                    f"left unchanged; choose a fresh --dir or --filename")
        return f"{what} failed: {error}"

    def _dry_run_files_note(self) -> str:
        """Exact paths of a stopped dry run's own files (possibly incomplete)"""
        if not self._dry_run_files:
            return "It created no files."
        return ("Files this dry run created, possibly incomplete (remove only these if "
                f"they are not wanted): {', '.join(str(p) for p in self._dry_run_files)}")

    def _dry_run_refusal(self, occupied: List[Path], disk_size: int) -> BackupResult:
        names = ", ".join(p.name for p in occupied)
        message = (f"Dry run refused: {self.config.output_dir} already holds {names}. A dry run "
                   f"writes its own tree listing, log and manifest under {self.config.filename}'s "
                   f"canonical names, so it runs only where none of them is taken. Nothing was "
                   f"changed; choose a fresh --dir or --filename.")
        return BackupResult(False, self.config.output_path, disk_size, error_message=message)

    def _dry_run_stopped(self, reason: str, disk_size: int) -> BackupResult:
        message = f"Dry run stopped: {reason}. {self._dry_run_files_note()}"
        return BackupResult(False, self.config.output_path, disk_size, error_message=message)

    def _relative(self, path: Path) -> str:
        """A path as shown in messages: relative to the output directory"""
        try:
            return str(path.relative_to(self.config.output_dir))
        except ValueError:
            return str(path)

    def _rollback_problem(self, message: str):
        """Record a file rollback could not put back (it reaches the records)"""
        self._rollback_problems.append(message)
        log(colorize("yellow", f"Warning: {message}"))

    def _restore_archived_artifacts(self, archived: List[Tuple[Path, Path]],
                                    clear: bool = True):
        """Put archived canonical artifacts back after a failed promotion.

        Best effort, and never replacing a file: if any canonical slot is
        occupied (or the caller could not clear them), nothing is restored and
        the prior files stay in the archive folder. If a restore fails partway,
        the files already restored are moved back into the folder. If renames
        keep failing, files can be left split between canonical names and the
        folder: the recorded problem then names where each one actually is.
        The archive folder is removed only when it is empty again."""
        pending = [(o, a) for o, a in archived if os.path.lexists(a)]
        occupied = [o.name for o, _ in pending if os.path.lexists(o)]
        if pending and (occupied or not clear):
            where = self._relative(self._archive_dir) if self._archive_dir else "its archive folder"
            reason = (f"{', '.join(occupied)} still occupied" if occupied
                      else "this run's files could not all be moved out of the canonical names")
            self._rollback_problem(f"prior bundle left intact in {where} ({reason}); "
                                   f"nothing was restored over this run's files")
            return
        restored: List[Tuple[Path, Path]] = []
        for original, archive in reversed(archived):
            if not os.path.lexists(archive):
                continue
            problem = None
            if os.path.lexists(original):
                problem = f"{original.name} is occupied"
            else:
                try:
                    archive.replace(original)
                    restored.append((original, archive))
                    log(colorize("yellow", f"Restored prior artifact: {original.name}"))
                    continue
                except OSError as e:
                    problem = f"could not restore {original.name}: {e}"
            # Keep the prior bundle together: move back what was restored
            for done_original, done_archive in reversed(restored):
                try:
                    if os.path.lexists(done_archive):
                        raise OSError(f"{self._relative(done_archive)} is occupied")
                    done_original.rename(done_archive)
                except OSError as e:
                    self._rollback_problem(f"could not return {done_original.name} to "
                                           f"{self._relative(done_archive)}: {e}")
            # Say where every file this rollback handled actually is now
            where = self._relative(self._archive_dir) if self._archive_dir else "the archive folder"
            in_folder = [o.name for o, a in archived if os.path.lexists(a)]
            at_canonical = [o.name for o, a in archived
                            if not os.path.lexists(a) and os.path.lexists(o)]
            missing = [o.name for o, a in archived
                       if not os.path.lexists(a) and not os.path.lexists(o)]
            if not at_canonical and not missing:
                outcome = (f"the files it had restored were moved back, so they are all in "
                           f"{where} ({', '.join(in_folder)})")
            else:
                outcome = ("continuing rename errors left the prior files split: "
                           + "; ".join(part for part in (
                               f"at canonical names: {', '.join(at_canonical)}" if at_canonical else "",
                               f"in {where}: {', '.join(in_folder)}" if in_folder else "",
                               f"not found: {', '.join(missing)}" if missing else "") if part))
            self._rollback_problem(f"rollback stopped ({problem}); {outcome}; nothing was "
                                   f"restored over this run's files")
            return
        if self._archive_dir is not None:
            try:
                os.rmdir(self._archive_dir)  # only ever succeeds when empty
            except OSError:
                pass
            else:
                log(colorize("yellow", f"Removed empty archive folder: {self._archive_dir.name}"))
                self._archive_dir = None

    def _archive_existing_artifacts(self) -> List[Tuple[Path, Path]]:
        """Move the existing canonical bundle into a new per-replacement
        folder, <filename>_overwritten-<stamp>-<rand>/, keeping each file's
        canonical name. Every archived manifest then still names its own
        sibling ISO, so verifyiso checks each retained master against its own
        hashes; no record is rewritten. Called only after the new ISO has
        verified, so a failed retry leaves the existing master untouched.

        The folder is created exclusively (an existing one is never reused),
        and only when there is something to archive."""
        existing = [p for p in self._standard_artifacts() if os.path.lexists(p)]
        archived: List[Tuple[Path, Path]] = []
        if not existing:
            return archived
        token = f"overwritten-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"
        folder = self.config.output_dir / f"{self.config.filename}_{token}"
        os.mkdir(folder)  # FileExistsError is an OSError: nothing has moved yet
        self._archive_dir = folder
        try:
            for path in existing:
                if path.is_symlink():
                    raise OSError(f"Symlink appeared at canonical artifact path during publish: {path}")
                archive_path = folder / path.name
                if os.path.lexists(archive_path):
                    raise OSError(f"{self._relative(archive_path)} already exists; not replacing it")
                path.rename(archive_path)
                archived.append((path, archive_path))
                log(colorize("yellow", f"Existing artifact archived to: {self._relative(archive_path)}"))
        except OSError:
            self._restore_archived_artifacts(archived)
            raise
        return archived

    def _set_location(self, result: BackupResult, role: str, path: Path):
        """Record where this run's own artifact for role actually is now"""
        if role == "iso":
            result.iso_path = path
        else:
            result.artifact_paths[role] = path

    def _move_published_attempts_back(self, result: BackupResult,
                                      published: List[Tuple[str, Path, Path]]) -> bool:
        """Move any already-published attempt artifacts back out of canonical
        slots so prior canonical artifacts can be restored. Returns False if
        any of this run's files is (or may be) still at a canonical name;
        result keeps naming each file where it actually is."""
        cleared = True
        for role, attempt, canonical in reversed(published):
            here, problem = inspect_path(canonical)
            if problem:
                # Unknown, not absent: it may still be this run's file
                self._rollback_problem(f"could not check {canonical.name} ({problem}); this "
                                       f"run's copy may still be at the canonical name")
                cleared = False
                continue
            if here is None:
                self._set_location(result, role, attempt)
                continue
            occupant, problem = inspect_path(attempt)
            if occupant is not None or problem:
                reason = "is occupied" if occupant is not None else f"could not be checked ({problem})"
                self._rollback_problem(f"could not move {canonical.name} back: "
                                       f"{attempt.name} {reason}")
                cleared = False
                continue
            try:
                canonical.rename(attempt)
                self._set_location(result, role, attempt)
                log(colorize("yellow", f"Moved failed publish back to attempt path: {attempt.name}"))
            except OSError as e:
                self._rollback_problem(f"could not move {canonical.name} back to "
                                       f"{attempt.name}: {e}; this run's copy stays at "
                                       f"the canonical name")
                cleared = False
        return cleared

    def _check_publish_symlinks(self, paths: List[Path]):
        linked = [p.name for p in paths if p.is_symlink()]
        if linked:
            raise OSError(f"Symlink appeared at publish path(s): {', '.join(linked)}")

    @staticmethod
    def _require_verified_iso(iso: Path):
        """The verified ISO is what publication is for: when it is missing,
        cannot be inspected or is no longer a regular file, publication fails"""
        here, problem = inspect_path(iso)
        if problem:
            raise OSError(f"could not inspect the verified ISO {iso.name} ({problem})")
        if here is None:
            raise OSError(f"the verified ISO {iso.name} is missing")
        if not stat.S_ISREG(here.st_mode):
            raise OSError(f"the verified ISO {iso.name} is no longer a regular file")

    def _promote_successful_attempt(self, result: BackupResult, attempt: ArtifactSet):
        """Publish verified attempt artifacts at the canonical names.

        The old canonical artifacts are archived only after the new copy has
        passed completeness and checksum verification. Archiving, publishing
        and any rollback run as one short section with Ctrl-C held
        (DeferredInterrupt), so a Ctrl-C can never leave a half-moved
        bundle; the caller acts on a held Ctrl-C afterwards. If promotion
        fails (OSError), the prior bundle is restored as far as the
        filesystem allows (best effort, see _restore_archived_artifacts) and
        the attempt falls through the normal failed-run record path."""
        archived: List[Tuple[Path, Path]] = []
        published: List[Tuple[str, Path, Path]] = []
        publish_order = attempt.publish_pairs(self.config.canonical_artifacts)
        self._held_interrupt = DeferredInterrupt()
        with self._held_interrupt:
            try:
                self._check_publish_symlinks(self._standard_artifacts())
                self._check_publish_symlinks([src for src, _ in publish_order if src.exists()])
                # Checked before the prior bundle is archived, and again as the
                # ISO is published (if it disappears in between, the rollback
                # below restores the prior bundle as far as it can)
                self._require_verified_iso(attempt.iso)
                archived = self._archive_existing_artifacts()
                for role, (src, dst) in zip(ARTIFACT_ROLES, publish_order):
                    if role == "iso":
                        self._require_verified_iso(src)
                    elif not src.exists():
                        continue
                    self._check_publish_symlinks([src, dst])
                    if os.path.lexists(dst):
                        raise OSError(f"{dst.name} appeared during publish; not replacing it")
                    src.replace(dst)
                    published.append((role, src, dst))
                    self._set_location(result, role, dst)
                    log(colorize("green", f"Published verified artifact: {dst.name}"))
            except OSError:
                cleared = self._move_published_attempts_back(result, published)
                self._restore_archived_artifacts(archived, clear=cleared)
                raise
            self.config.attempt_token = None
            result.iso_path = self.config.canonical_output_path
            self._published = True

    def _mark_failed_attempt(self, result: BackupResult, attempt: ArtifactSet):
        """Move this run's attempt artifacts to failure-marked names.

        Only this run's files at their attempt paths are touched: one that a
        failed rollback left at a canonical name stays there (and is recorded
        there), and canonical artifacts from a prior successful run are never
        relabelled as failed evidence for this run. Safe to call again: the
        failure token is chosen once, and a repeat call resumes an
        interrupted marking from what is on disk. A file that cannot be
        renamed, or whose location cannot be checked, is recorded where it
        last was, with a warning."""
        if self.config.failure_token is None:
            self.config.failure_token = (f"failed-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}"
                                         f"-{uuid4().hex[:6]}")
        self.config.attempt_token = None
        marker = ".iso.mismatch" if result.checksum_match is False else ".iso.partial"
        failed = self.config.artifacts  # tree, isolyzer, log and manifest now carry the failure token
        targets = {
            "iso": self.config.output_dir / f"{self.config.filename}_{self.config.failure_token}{marker}",
            "tree": failed.tree, "isolyzer": failed.isolyzer,
            "log": failed.log, "manifest": failed.manifest,
        }
        for role in ARTIFACT_ROLES:
            src, dst = getattr(attempt, role), targets[role]
            here = result.iso_path if role == "iso" else result.artifact_paths.get(role, src)
            if here != src:
                continue  # recorded elsewhere (a canonical name, or already marked)
            current, problem = inspect_path(src)
            if current is None and not problem:
                current, problem = inspect_path(dst)
                if current is not None:
                    self._set_location(result, role, dst)  # renamed by an earlier, interrupted call
                if current is not None or not problem:
                    continue  # or never created: it keeps its planned name in the records
            if problem:
                self._set_location(result, role, src)
                note = (f"could not verify where this run's {role} is ({problem}); "
                        f"it was last at {src.name} and may now be at {dst.name}")
                result.warnings.append(note)
                log(colorize("yellow", f"Warning: {note}"))
                continue
            try:
                src.rename(dst)
            except OSError as e:
                self._set_location(result, role, src)
                note = f"could not rename {src.name} to {dst.name} ({e}); it is still at {src.name}"
                if role in ("log", "manifest"):
                    note += " (this run's staged success record: it does not describe a successful run)"
                result.warnings.append(note)
                log(colorize("yellow", f"Warning: {note}"))
                continue
            self._set_location(result, role, dst)
            log(colorize("yellow", f"ISO renamed to: {dst.name}" if role == "iso"
                         else f"{src.name} renamed to: {dst.name}"))

    def _write_run_records(self, result: BackupResult, start_time: datetime.datetime,
                           disk_info: Dict[str, Any], iso_analysis: Dict[str, Any]):
        current = RecordView.from_run(self.config, result).current
        metadata = RecordMetadata.check(current)
        unknown = [(path, problem) for path, problem in ((current.iso, metadata.iso_problem),
                                                         (current.isolyzer, metadata.isolyzer_problem))
                   if problem]
        if unknown and result.success and not self.config.dry_run:
            # Staged success records never describe a file they could not
            # check: the run takes the failure path before publication
            raise OSError("could not check " + "; ".join(f"{path.name} ({problem})"
                                                         for path, problem in unknown))
        for path, problem in unknown:
            # Failure records still get written, saying what is unknown and why
            if not any(problem in w and path.name in w for w in result.warnings):
                result.warnings.append(f"could not check {path.name} while writing the records "
                                       f"({problem}): its presence and size are unknown")
        opener = self._create_dry_run_file if self.config.dry_run else None
        create_formatted_log(self.config.log_path, self.config, result,
                             start_time, disk_info, iso_analysis, self.run, opener=opener,
                             metadata=metadata)
        self._create_manifest(result, disk_info, iso_analysis, metadata=metadata)

    def _write_failure_records_best_effort(self, result: BackupResult,
                                           start_time: datetime.datetime,
                                           disk_info: Dict[str, Any],
                                           iso_analysis: Dict[str, Any]):
        try:
            self._write_run_records(result, start_time, disk_info, iso_analysis)
        except Exception as record_error:
            log(colorize("red", f"Could not write failure records: {record_error}"))
        except KeyboardInterrupt:
            self.interrupted = True
            log(colorize("red", "Interrupted by operator (Ctrl-C) while writing failure records; "
                                "they may be incomplete"))

    def _discard_staged_records(self, result: BackupResult, attempt: ArtifactSet):
        """Delete this run's staged log/manifest. They were written for a
        success that then did not happen, so they must never survive
        (renamed as failure records) claiming success. One that a failed
        rollback left at a canonical name is this run's own too (publication
        only ever moves into an empty slot) and is removed there. If one
        cannot be removed, or checked, the failure records say so."""
        canonical = self.config.canonical_artifacts
        for role in ("log", "manifest"):
            # Only its attempt name or the canonical name a failed rollback
            # left it at; a failed-* name holds failure records
            recorded = result.artifact_paths.get(role)
            at_canonical = recorded == getattr(canonical, role)
            path = recorded if at_canonical else getattr(attempt, role)
            where = (" (left at the canonical name by the failed rollback)"
                     if at_canonical else "")
            current, problem = inspect_path(path)
            if problem:
                note = (f"could not check {path.name}{where} ({problem}); if present, it is "
                        f"this run's staged success {role} and does not describe a successful run")
                result.warnings.append(note)
                log(colorize("yellow", f"Warning: {note}"))
                continue
            if current is None:
                if recorded == path:
                    del result.artifact_paths[role]
                continue
            if not stat.S_ISREG(current.st_mode):
                continue
            try:
                path.unlink()
            except OSError as e:
                note = (f"could not remove {path.name}{where} ({e}): it is this run's staged "
                        f"success {role} and does not describe a successful run")
                result.warnings.append(note)
                log(colorize("yellow", f"Warning: {note}"))
                continue
            if recorded == path:
                del result.artifact_paths[role]
            if at_canonical:
                note = f"removed {path.name}: this run's staged success {role}{where}"
                result.warnings.append(note)
                log(colorize("yellow", f"Warning: {note}"))

    def _fail_staged_success(self, result: BackupResult, attempt: ArtifactSet,
                             start_time: datetime.datetime, disk_info: Dict[str, Any],
                             iso_analysis: Dict[str, Any]):
        """A verified run that could not then be recorded or published:
        drop its staged success records first, then mark the attempt failed
        and write failure records (best effort). A staged record that cannot
        be removed is contradicted in the failure records; if those cannot
        be written either, only the console says so."""
        # Removing the staged records and renaming the attempt are a few
        # local unlinks/renames: held like publication, so Ctrl-C cannot stop them halfway
        held = DeferredInterrupt()
        with held:
            self._discard_staged_records(result, attempt)
            self._mark_failed_attempt(result, attempt)
        if held.received:
            self.interrupted = True
        self._write_failure_records_best_effort(result, start_time, disk_info, iso_analysis)

    def _prepare_backup(self) -> Tuple[Optional[BackupPreparation], Optional[BackupResult]]:
        if os.geteuid() != 0:
            log(colorize("red", "This script must be run with sudo."))
            return None, BackupResult(False, self.config.output_path, 0,
                                      error_message="Not running as root")

        disk_info = self._get_disk_info()
        if not disk_info:
            return None, BackupResult(False, self.config.output_path, 0,
                                      error_message=f"Could not get disk info for {self.config.disk_id}")

        disk_size = disk_info.get("TotalSize", 0)
        self.config.media_type = disk_info.get("OpticalMediaType", "Unknown")
        self.config.mount_point = disk_info.get("MountPoint") or f"/Volumes/{self.config.volume_name}"

        if not disk_info.get("OpticalMediaType") and not self.config.force:
            device_desc = disk_info.get('MediaName', disk_info.get('IORegistryEntryName', 'unknown device'))
            log(colorize("red", f"/dev/{self.config.disk_id} does not appear to be an optical disc "
                                f"(device: {device_desc}). Refusing to continue. Use --force to override."))
            return None, BackupResult(False, self.config.output_path, disk_size,
                                      error_message=f"/dev/{self.config.disk_id} is not optical media")

        try:
            self.config.output_dir.mkdir(parents=True)
            created_output_dir = True
        except FileExistsError:
            created_output_dir = False
        if created_output_dir and self.config.dry_run:
            self._keep_new_dry_run_folder_owner(self.config.output_dir)

        if self.config.output_dir.is_symlink() or not self.config.output_dir.is_dir():
            log(colorize("red", f"Output path {self.config.output_dir} exists but is not a real directory "
                                f"(file or symlink). Refusing to continue."))
            return None, BackupResult(False, self.config.output_path, disk_size,
                                      error_message=f"Output path {self.config.output_dir} is not a directory")

        planned = self._standard_artifacts()
        if self.config.dry_run:
            # A dry run writes its tree listing, log and manifest at the
            # canonical names, so it runs only where none of the five is taken
            # by anything (a file, an empty file, a folder, a live or broken
            # symlink). There is no overwrite prompt.
            occupied = [p for p in planned if os.path.lexists(p)]
            if occupied:
                return None, self._dry_run_refusal(occupied, disk_size)

        linked = [p.name for p in planned if p.is_symlink()]
        if linked:
            log(colorize("red", f"Refusing to continue: symlink(s) at planned output path(s): {', '.join(linked)}"))
            return None, BackupResult(False, self.config.output_path, disk_size,
                                      error_message=f"Symlink at planned output path(s): {', '.join(linked)}")

        if not self.config.dry_run:
            existing_artifacts = [p for p in planned if p.exists()]
            if existing_artifacts and not self._confirm_overwrite(existing_artifacts):
                names = ", ".join(p.name for p in existing_artifacts)
                return None, BackupResult(False, self.config.output_path, disk_size,
                                          error_message=f"Aborted to avoid replacing existing artifact(s): {names}")

            self.config.attempt_token = f"attempt-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"
        prepared = BackupPreparation(disk_info, disk_size, created_output_dir,
                                     self.config.artifacts)
        return prepared, None

    def _run_copy_phase(self, disk_size: int) -> CopyPhaseResult:
        tree_ok = False
        backup_error = None
        unmount_ok = None
        finalized = None

        try:
            self.run.mark("tree")
            tree_ok = self._generate_tree_listing()
            if not self.config.dry_run:
                self.run.mark("unmount")
                unmount_ok = self._unmount_disk()
                if not unmount_ok:
                    backup_error = f"Failed to unmount /dev/{self.config.disk_id}"
        except KeyboardInterrupt:
            if self.config.dry_run:
                raise  # a dry run makes no master: Ctrl-C simply stops it, as before
            # Recorded like any failed run: attempt files get failed-* names
            self._unmount_interrupted = "unmount" in self.run.step_times
            backup_error = "Interrupted by operator (Ctrl-C) before the copy started"
            log(colorize("red", f"Backup failed: {backup_error}"))

        iso_result = None
        iso_analysis = {"skipped": True, "reason": backup_error or "not started"}
        if backup_error is None:
            try:
                if self.config.dry_run:
                    iso_analysis = {"skipped": True, "reason": "dry run"}
                    log(colorize("cyan", "Dry run:") + " " + colorize("yellow", "Skipping .iso creation and verification."))
                else:
                    iso_result = self._create_and_verify_iso(disk_size)
                    iso_analysis = self._analyze_iso_structure(self.config.output_path)
            except (Exception, KeyboardInterrupt) as e:
                if isinstance(e, KeyboardInterrupt) and self.config.dry_run:
                    raise  # a dry run makes no master: Ctrl-C simply stops it
                backup_error = "Interrupted by operator (Ctrl-C)" if isinstance(e, KeyboardInterrupt) else str(e)
                iso_analysis = {"skipped": True, "reason": f"backup failed: {backup_error}"}
                log(colorize("red", f"Backup failed: {backup_error}"))
            finally:
                # Eject the disc even if the backup failed
                self.run.mark("finalize")
                try:
                    finalized = self._finalize_disk()
                except KeyboardInterrupt:
                    if self.config.dry_run:
                        raise  # a dry run makes no master: Ctrl-C simply stops it
                    # Only the eject was cut short: a copy that verified stays
                    # verified, with a warning, and the run exits 130 (main);
                    # any failure keeps its own outcome
                    finalized = False
                    self._eject_interrupted = True
                    self.interrupted = True
                    log(colorize("yellow", "Disc eject interrupted by operator (Ctrl-C)"))

        if (backup_error is None and not self.config.dry_run
                and iso_analysis.get('smaller_than_expected')):
            backup_error = (f"ISO is structurally smaller than the filesystem declares by "
                            f"{iso_analysis.get('size_difference_bytes', 0)} bytes — "
                            f"truncated read, master not certified")
            log(colorize("red", f"Backup failed: {backup_error}"))

        return CopyPhaseResult(tree_ok, iso_result, iso_analysis,
                               backup_error, unmount_ok, finalized)

    def _build_backup_result(self, phase: CopyPhaseResult, disk_size: int,
                             attempt: ArtifactSet) -> BackupResult:
        iso_result = phase.iso_result
        result = BackupResult(
            success=phase.backup_error is None,
            iso_path=self.config.output_path,
            disk_size=disk_size,
            creation_path=(iso_result.output_path if iso_result else
                           (attempt.iso if attempt.iso.exists() else None)),
            md5_iso=iso_result.md5_iso if iso_result else None,
            md5_raw=iso_result.md5_raw if iso_result else None,
            sha256_iso=iso_result.sha256_iso if iso_result else None,
            sha256_raw=iso_result.sha256_raw if iso_result else None,
            creation_time=iso_result.creation_time if iso_result else 0.0,
            verification_time=iso_result.verification_time if iso_result else 0.0,
            error_message=phase.backup_error,
            unmount_ok=phase.unmount_ok,
            finalized=phase.finalized,
            eject_interrupted=self._eject_interrupted,
            unmount_interrupted=self._unmount_interrupted,
            flush_method=iso_result.flush_method if iso_result else None,
            cache_bypass=iso_result.cache_bypass if iso_result else None
        )
        self._classify_result(result, phase.tree_ok, iso_result, phase.iso_analysis)
        return result

    def _classify_result(self, result: BackupResult, tree_ok: bool,
                         iso_result: Optional[IsoCreationResult],
                         iso_analysis: Dict[str, Any]):
        if result.checksum_match is False:
            result.success = False
            result.error_message = "Checksum mismatch: written ISO does not match source stream"

        # Non-fatal problems on otherwise-valid runs: the master is good, but
        # the record should make these findable (success_with_warnings).
        if result.eject_interrupted:
            # Recorded on every outcome; a verified copy stays verified
            result.warnings.append("disc eject interrupted by operator (Ctrl-C)")
        elif result.success and not self.config.dry_run and not result.finalized:
            result.warnings.append("disc finalization (eject) failed")
        if result.success and not tree_ok:
            result.warnings.append("tree listing failed or incomplete")
        if result.success and not self.config.dry_run and iso_analysis.get('error'):
            result.warnings.append(f"isolyzer structural analysis not performed ({iso_analysis['error']})")
        # Degraded-independence notes from the copy/verify pass (fsync or page-cache
        # fallbacks) are recorded regardless of overall success.
        if iso_result and iso_result.warnings:
            result.warnings.extend(iso_result.warnings)

    def _record_and_publish(self, result: BackupResult, start_time: datetime.datetime,
                            disk_info: Dict[str, Any], iso_analysis: Dict[str, Any],
                            attempt: ArtifactSet):
        # Failed or aborted runs keep all their records under failure-marked
        # names, so they are obvious in the output directory and a retry can
        # never overwrite the evidence of a previous attempt.
        if not result.success and not self.config.dry_run:
            # A few local renames: held like _fail_staged_success, so Ctrl-C
            # cannot leave the failed attempt's files half-renamed
            held = DeferredInterrupt()
            with held:
                self._mark_failed_attempt(result, attempt)
            if held.received:
                self.interrupted = True
                log(colorize("yellow", "Ctrl-C was held until this failed attempt's files had "
                                       "their failed-* names; the failure is recorded as usual"))

        # Write the log and manifest before publishing a successful ISO. This
        # makes the record bundle part of the same promotion step as the master,
        # so a sidecar write failure cannot leave a canonical ISO with missing
        # evidence.
        try:
            self._write_run_records(result, start_time, disk_info, iso_analysis)
        except (Exception, KeyboardInterrupt) as e:
            if isinstance(e, KeyboardInterrupt):
                if self.config.dry_run:
                    raise  # a dry run makes no master: Ctrl-C simply stops it
                self.interrupted = True
                problem = "Interrupted by operator (Ctrl-C) while writing run records"
            elif self.config.dry_run:
                problem = (f"Dry run stopped: {self._dry_run_problem(e, 'writing its log and manifest')}. "
                           f"{self._dry_run_files_note()}")
            else:
                problem = f"Could not write run records before publish: {e}"
            if result.success:
                result.success = False
                result.error_message = problem
                if not self.config.dry_run:  # main reports a dry run's message
                    log(colorize("red", f"Backup failed: {result.error_message}"))
                    self._fail_staged_success(result, attempt, start_time,
                                              disk_info, iso_analysis)
            else:
                log(colorize("red", f"Could not write failure records: {problem}"))

        if result.success and not self.config.dry_run:
            try:
                self._promote_successful_attempt(result, attempt)
            except (OSError, KeyboardInterrupt) as e:
                if self._published:
                    # Ctrl-C landed just after the held section ended: every
                    # file is already published and its records are final
                    self.interrupted = True
                else:
                    if isinstance(e, KeyboardInterrupt):
                        self.interrupted = True
                        result.error_message = ("Interrupted by operator (Ctrl-C) before the "
                                                "verified bundle was published")
                        if isinstance(e.__context__, OSError):
                            result.error_message += (f" (after publication had failed: "
                                                     f"{e.__context__})")
                    else:
                        result.error_message = f"Could not publish verified attempt artifacts: {e}"
                    result.success = False
                    result.warnings.extend(self._rollback_problems)
                    log(colorize("red", f"Backup failed: {result.error_message}"))
                    self._fail_staged_success(result, attempt, start_time,
                                              disk_info, iso_analysis)
            if self._held_interrupt is not None and self._held_interrupt.received:
                # A Ctrl-C held during publication is acknowledged, never
                # half-acted on: a published bundle stays published (exit 130),
                # a failed one's best-effort rollback has already run
                self.interrupted = True
                log(colorize("yellow",
                             "Ctrl-C was held until publication finished: the verified bundle "
                             "is complete and its records are accurate" if result.success else
                             "Ctrl-C was held until the failed publication's rollback had run "
                             "(any file it could not put back is listed in the warnings)"))

    def _fix_ownership(self, result: BackupResult, created_output_dir: bool):
        """Hand this run's files to SUDO_USER. Runs after the records are
        final, so it is best effort throughout: a file that cannot be
        checked is skipped with a warning, the others are still handed
        over, and no error here changes the run's result or exit status."""
        if self.config.dry_run:
            return  # a dry run's files changed owner as it created them; its new folder keeps its owner
        sudo_user = os.environ.get("SUDO_USER")
        if not sudo_user:
            return
        try:
            # This run's files where they actually are (see RecordView.current),
            # plus any staged record a failed rollback left elsewhere
            current = RecordView.from_run(self.config, result).current
            artifacts = list(dict.fromkeys([*current.archive_order, *result.artifact_paths.values()]))
            if created_output_dir:
                artifacts.insert(0, self.config.output_dir)
            # The archive folder this run created, but not the archived files:
            # they keep whatever ownership their own run gave them
            if self._archive_dir is not None:
                folder, problem = inspect_path(self._archive_dir)
                if problem:
                    log(colorize("yellow", f"Warning: could not check {self._archive_dir} ({problem}); "
                                           f"its owner was not changed"))
                elif folder is not None and stat.S_ISDIR(folder.st_mode):
                    artifacts.append(self._archive_dir)
            existing = []
            for artifact in artifacts:
                try:
                    os.stat(artifact)  # follows a link, as chown does
                except FileNotFoundError:
                    continue
                except OSError as e:
                    log(colorize("yellow", f"Warning: could not check {artifact} ({e}); "
                                           f"its owner was not changed"))
                    continue
                existing.append(str(artifact))
            subprocess.run(["chown", sudo_user] + existing, check=True)
        except Exception as e:
            log(colorize("yellow", f"Warning: Could not set ownership of output files: {e}"))

    def create_backup(self) -> BackupResult:
        """Main entry point for creating backup"""
        start_time = datetime.datetime.now()
        self.run = RunContext(
            run_id=f"{start_time.strftime('%Y%m%dT%H%M%S')}_{self.config.operator}_{self.config.disk_id}",
            run_uuid=str(uuid4()),
            start_time=start_time,
        )

        prepared, early_result = self._prepare_backup()
        if early_result:
            return early_result

        try:
            phase = self._run_copy_phase(prepared.disk_size)
        except DryRunStopped as e:
            # A failed dry-run tree listing: no record is written after it
            return self._dry_run_stopped(str(e), prepared.disk_size)
        result = self._build_backup_result(phase, prepared.disk_size, prepared.attempt)
        try:
            self._record_and_publish(result, start_time, prepared.disk_info,
                                     phase.iso_analysis, prepared.attempt)
        except KeyboardInterrupt:
            if self.config.dry_run or self._published:
                raise
            # A further Ctrl-C landed inside the failure handling itself.
            # Finish its essential part (held): no staged record may survive
            # claiming success, and the attempt gets failed-* names.
            self.interrupted = True
            result.success = False
            result.error_message = result.error_message or "Interrupted by operator (Ctrl-C)"
            held = DeferredInterrupt()
            with held:
                self._discard_staged_records(result, prepared.attempt)
                self._mark_failed_attempt(result, prepared.attempt)  # resumes a cut-short marking
            self._write_failure_records_best_effort(result, start_time, prepared.disk_info,
                                                    phase.iso_analysis)
        try:
            self._generate_summary(result)
            self._fix_ownership(result, prepared.created_output_dir)
        except KeyboardInterrupt:
            if self.config.dry_run:
                raise  # a dry run makes no master: Ctrl-C simply stops it
            # The bundle and its records are already final; only the summary
            # or the ownership fix was cut short
            self.interrupted = True
            log(colorize("yellow", "Interrupted by operator (Ctrl-C) after the run was recorded: "
                                   "the summary or the file-ownership fix may be incomplete"))

        return result
    
    def _get_disk_info(self) -> Dict[str, Any]:
        """Get disk information via diskutil"""
        try:
            return run_cmd(["diskutil", "info", "-plist", f"/dev/{self.config.disk_id}"], plist=True)
        except subprocess.CalledProcessError as e:
            log(colorize("red", f"Failed to get disk info: {e}"))
            return {}
    
    def _confirm_overwrite(self, existing_paths: List[Path]) -> bool:
        """Confirm replacement of existing canonical artifacts.

        A confirmed replacement does not truncate anything up front. Existing
        artifacts remain in place until the new attempt verifies, then they are
        moved, unchanged, into a <filename>_overwritten-<stamp>-<rand>/ folder
        before the new artifacts publish.
        """
        names = ", ".join(p.name for p in existing_paths)
        question = (f"Existing artifact(s) may be replaced by this run ({names}); if the new "
                    f"copy verifies, the current ones are kept in a "
                    f"{self.config.filename}_overwritten-* folder. Continue? (y/n): ")
        response = get_input(colorize("yellow", question))
        return response.lower() == 'y'
    
    def _generate_tree_listing(self) -> bool:
        """Generate tree listing of volume contents; returns True on success.

        A normal run treats a failed listing as a warning. A dry run's listing
        is one of its own outputs, so there any failure (its name taken, the
        file not creatable, writable or closable, tree missing or failing)
        raises DryRunStopped instead."""
        log_divider("Generating Tree Listing")
        tree_cmd = tree_command(self.config)
        try:
            log(colorize("cyan", "Generating tree listing..."))
            log(colorize("cyan", "Running command:") + " " + colorize("yellow", shlex.join(tree_cmd)))
            with (self._create_dry_run_file(self.config.tree_path, encoding="utf-8")
                  if self.config.dry_run else
                  open(self.config.tree_path, "w", encoding="utf-8")) as f:
                proc = subprocess.run(tree_cmd, stdout=f, stderr=subprocess.STDOUT,
                             env={**os.environ, "LANG": "en_US.UTF-8"})
            if proc.returncode != 0:
                if self.config.dry_run:
                    raise DryRunStopped(f"tree exited with status {proc.returncode}, so the "
                                        f"listing is missing or incomplete")
                log(colorize("yellow", f"Warning: tree exited with status {proc.returncode}; listing may be missing or incomplete."))
                return False
            log(colorize("cyan", "Tree saved to:") + " " + colorize("yellow", str(self.config.tree_path)))
            return True
        except DryRunStopped:
            raise
        except Exception as e:
            if self.config.dry_run:
                raise DryRunStopped(self._dry_run_problem(e, "the tree listing")) from e
            log(colorize("red", f"Tree generation failed: {e}"))
            return False
    
    def _create_and_verify_iso(self, disk_size: int) -> IsoCreationResult:
        """Create ISO with in-stream source hashing, then verify by re-reading the written file"""
        log_divider("Creating ISO")
        log(colorize("cyan", "Creating ISO from:") + " " + colorize("yellow", self.config.disk_id))

        dd_cmd = dd_command(self.config.disk_id)
        log(colorize("cyan", "Running command:") + " " + colorize("yellow", shlex.join(dd_cmd)))
        log(colorize("cyan", f"dd's output is written to {self.config.output_path} and hashed by makeiso.py"))

        raw_hasher = hashlib.md5()
        raw_sha_hasher = hashlib.sha256()
        bytes_written = 0
        warnings: List[str] = []
        start_time = time.time()
        if self.run:
            self.run.mark("copy_start")

        # Initialize progress display
        if ANSI_ENABLED:
            print("\n" * 4, end="")

        # A KeyboardInterrupt here propagates to create_backup(), which records
        # the interruption as a failed run and renames the partial ISO.
        with open(self.config.output_path, 'wb') as iso_file:
            with subprocess.Popen(dd_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                while True:
                    chunk = proc.stdout.read(4 * 1024 * 1024)  # 4MB chunks
                    if not chunk:
                        break

                    # Write to file and update the streamed source hashes
                    iso_file.write(chunk)
                    raw_hasher.update(chunk)
                    raw_sha_hasher.update(chunk)
                    bytes_written += len(chunk)

                    # Update progress (with "ISO Creation:" label to match expected output)
                    self._display_progress("ISO Creation", bytes_written, disk_size, start_time)

                # Wait for dd to complete and get stderr (which contains the transfer stats)
                _, stderr = proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(f"dd failed: {stderr.decode()}")

                # Print dd output (records in/out, transfer stats) - decode and strip
                if stderr:
                    # Decode bytes to string and clean up the output
                    dd_output = stderr.strip() if isinstance(stderr, str) else stderr.decode().strip()
                    log(dd_output)

            # Ask for the data to reach the media before the verification pass
            # below: plain fsync on macOS may stop at the drive's cache, and
            # F_FULLFSYNC asks the drive to flush it too (a drive or network
            # volume may still not honour it, so neither is a guarantee)
            iso_file.flush()
            try:
                fcntl.fcntl(iso_file.fileno(), fcntl.F_FULLFSYNC)
                flush_method = "F_FULLFSYNC"
            except (OSError, AttributeError):
                os.fsync(iso_file.fileno())
                flush_method = "fsync"
                warnings.append("F_FULLFSYNC unavailable; fell back to fsync, which on "
                                "macOS may not flush the drive's own write cache before "
                                "verification")
                log(colorize("yellow", f"Warning: {warnings[-1]}"))

        # Completeness check: a bit-perfect *equal* copy is not enough — it must
        # also be *complete*. dd exits 0 on a short read (bad sector, flaky USB
        # optical drive), and the streamed and re-read hashes would then match
        # over the same truncated bytes. Compare what we wrote against the disc's
        # reported size and fail hard on any shortfall. (disk_size == 0 means the
        # size was unknown up front, so there is nothing to compare against.)
        if disk_size and bytes_written < disk_size:
            raise RuntimeError(
                f"Short read: copied {bytes_written} of {disk_size} bytes "
                f"({disk_size - bytes_written} bytes missing). The disc may have a "
                f"read error; the ISO is incomplete and will not be certified.")

        creation_time = time.time() - start_time
        raw_hash = raw_hasher.hexdigest()
        raw_sha = raw_sha_hasher.hexdigest()
        if self.run:
            self.run.mark("copy_end")

        log(colorize("green", "ISO creation complete."))
        log(colorize("cyan", "MD5 (Raw Disk):") + " " + colorize("yellow", raw_hash))
        log(colorize("cyan", "SHA-256 (Raw Disk):") + " " + colorize("yellow", raw_sha))

        # Verification: re-read the written ISO and hash it, independently of
        # the hashes taken from the stream (the optional manual spot-check,
        # a fresh raw-device read, is shown with the summary, naming the ISO
        # where the run left it)
        log_divider("Verifying ISO")
        log(colorize("cyan", "Verifying using:") + " " + colorize("yellow", f"a re-read of {self.config.output_path.name}, hashed and compared to the streamed raw disk hashes"))

        iso_hasher = hashlib.md5()
        iso_sha_hasher = hashlib.sha256()
        verify_start = time.time()
        bytes_read = 0
        if self.run:
            self.run.mark("verify_start")

        # Initialize progress display
        if ANSI_ENABLED:
            print("\n" * 4, end="")

        with open(self.config.output_path, 'rb') as iso_file:
            # Ask macOS not to cache this read, so it is not simply served
            # from memory (data the write left cached may still be used)
            try:
                fcntl.fcntl(iso_file.fileno(), fcntl.F_NOCACHE, 1)
                cache_bypass = True
            except (OSError, AttributeError):
                cache_bypass = False
                warnings.append("F_NOCACHE unavailable; the verification read may have "
                                "been served from the page cache rather than re-read from "
                                "disk, weakening the independence of the verification pass")
                log(colorize("yellow", f"Warning: {warnings[-1]}"))
            while True:
                chunk = iso_file.read(4 * 1024 * 1024)  # 4MB chunks
                if not chunk:
                    break
                iso_hasher.update(chunk)
                iso_sha_hasher.update(chunk)
                bytes_read += len(chunk)
                self._display_progress("Verification", bytes_read, bytes_written, verify_start)

        verification_time = time.time() - verify_start
        iso_hash = iso_hasher.hexdigest()
        iso_sha = iso_sha_hasher.hexdigest()
        if self.run:
            self.run.mark("verify_end")

        log(colorize("cyan", "MD5 (ISO):") + " " + colorize("yellow", iso_hash))
        log(colorize("cyan", "MD5 (Raw Disk):") + " " + colorize("yellow", raw_hash))
        log(colorize("cyan", "SHA-256 (ISO):") + " " + colorize("yellow", iso_sha))
        log(colorize("cyan", "SHA-256 (Raw Disk):") + " " + colorize("yellow", raw_sha))

        if iso_hash == raw_hash and iso_sha == raw_sha:
            log(colorize("green", "Checksum match: the re-read ISO matches the stream read from the disc"
                                  + ("." if cache_bypass and flush_method == "F_FULLFSYNC"
                                     else " (see the warning above about how it was flushed or re-read).")))
        else:
            log(colorize("red", "Checksum mismatch! The re-read ISO does not match the source stream."))

        return IsoCreationResult(
            creation_time=creation_time,
            verification_time=verification_time,
            md5_raw=raw_hash,
            md5_iso=iso_hash,
            sha256_raw=raw_sha,
            sha256_iso=iso_sha,
            output_path=self.config.output_path,
            warnings=warnings,
            flush_method=flush_method,
            cache_bypass=cache_bypass,
        )
    
    def _display_progress(self, title: str, current: int, total: int, start_time: float):
        """Display progress information"""
        if not ANSI_ENABLED:
            # Redirected output: one plain line per ~10% instead of cursor codes
            if total > 0:
                decile = min(10, current * 10 // total)
                key = (title, decile)
                if decile > 0 and key not in self._progress_marks:
                    self._progress_marks.add(key)
                    log(f"{title}: {decile * 10}% ({current / (1024*1024):.0f}MB / {total / (1024*1024):.0f}MB)")
            return
        elapsed = time.time() - start_time
        speed = current / elapsed if elapsed > 0 else 0
        remaining = max(0, total - current)
        eta = remaining / speed if speed > 0 else 0
        
        mb_current = current / (1024 * 1024)
        mb_total = total / (1024 * 1024)
        speed_mb = speed / (1024 * 1024)
        
        sys.stdout.write("\033[4A")
        sys.stdout.write(f"\033[2K\r{colorize('cyan', title)}: {mb_current:.0f}MB / {mb_total:.0f}MB\n")
        sys.stdout.write(f"\033[2K\r{colorize('cyan', 'Elapsed')}: {str(datetime.timedelta(seconds=int(elapsed)))}\n")
        sys.stdout.write(f"\033[2K\r{colorize('cyan', 'Remaining')}: {str(datetime.timedelta(seconds=int(eta)))}\n")
        sys.stdout.write(f"\033[2K\r{colorize('cyan', 'Avg Speed')}: {speed_mb:.2f}MB/s\n")
        sys.stdout.flush()
    
    def _unmount_disk(self) -> bool:
        """Unmount disk safely"""
        log_divider("Unmounting Disk")
        log(colorize("cyan", f"Unmounting /dev/{self.config.disk_id}..."))
        log(colorize("cyan", "Running command:") + " " + colorize("yellow", shlex.join(["diskutil", "unmountDisk", f"/dev/{self.config.disk_id}"])))
        
        try:
            subprocess.run(["diskutil", "unmountDisk", f"/dev/{self.config.disk_id}"], 
                         capture_output=True, text=True, check=True)
            log(colorize("green", "Unmounted successfully."))
            return True
        except subprocess.CalledProcessError as e:
            log(colorize("red", f"Failed to unmount:\n{e.stderr}"))
            return False
        except OSError as e:
            log(colorize("red", f"Failed to unmount: could not run diskutil: {e}"))
            return False
    
    def _finalize_disk(self) -> Optional[bool]:
        """Eject the disc; True if ejected, False if the eject failed,
        None when nothing was attempted (dry run)"""
        log_divider("Finalizing")
        if self.config.dry_run:
            # Inspect only — a dry run must not change system state
            try:
                mounted = bool(run_cmd(["diskutil", "info", "-plist", f"/dev/{self.config.disk_id}"],
                                       plist=True).get("MountPoint"))
                state = "mounted" if mounted else "not mounted"
                log(colorize("cyan", "Dry run:") + " " + colorize("yellow", f"Disk is {state}; leaving as-is (no mount, no eject)."))
            except Exception as e:
                log(colorize("yellow", f"Dry run: could not read mount state: {e}"))
            return None
        else:
            # Eject directly — diskutil eject works on an unmounted disk, and
            # the goal of finalization is physical release of the disc.
            log(colorize("cyan", "Running command:") + " " + colorize("yellow", shlex.join(["diskutil", "eject", f"/dev/{self.config.disk_id}"])))
            try:
                eject_proc = subprocess.run(["diskutil", "eject", f"/dev/{self.config.disk_id}"],
                                            capture_output=True, text=True)
            except OSError as e:
                log(colorize("yellow", f"Warning: eject failed: could not run diskutil: {e}"))
                return False
            if eject_proc.returncode != 0:
                log(colorize("yellow", f"Warning: eject failed: {eject_proc.stderr.strip() or eject_proc.stdout.strip()}"))
                return False
            log(colorize("green", "Disk ejected. All done."))
            return True
    
    def _generate_summary(self, result: BackupResult):
        """Generate timing summary"""
        log_divider("Summary")
        if not self.config.dry_run:
            speed = result.speed_mb_s(result.creation_time)
            multiplier = calculate_disc_speed_multiplier(speed, self.config.media_type)
            log(colorize("cyan", "Time to create:") + " " + colorize("yellow", f"{result.creation_time:.2f} seconds") +
                f" ({format_duration(result.creation_time)})")
            if result.verification_time:
                log(colorize("cyan", "Time to verify:") + " " + colorize("yellow", f"{result.verification_time:.2f} seconds") +
                    f" ({format_duration(result.verification_time)})")
            log(colorize("cyan", "Average creation speed:") + " " + colorize("yellow", f"{speed:.2f} MB/s ({multiplier})"))
            # Named where the run left its ISO (published, or kept under a
            # failed-* name); nothing is shown when no ISO is there
            if result.creation_path and regular_file_size(result.iso_path)[0] is not None:
                log(colorize("cyan", f"Optional manual spot-check (not run by makeiso.py; it re-reads "
                                     f"the whole disc, which must be back in the drive as "
                                     f"{self.config.disk_id} - check with diskutil list):"))
                log(colorize("yellow", spot_check_command(str(result.iso_path), self.config.disk_id)))
        else:
            log(colorize("cyan", "Dry run:") + " " + colorize("yellow", "No timing summary available."))
    
    def _parse_isolyzer_xml(self, xml_content: str) -> Dict[str, Any]:
        """Parse isolyzer XML output into structured data"""
        import xml.etree.ElementTree as ET
        
        try:
            root = ET.fromstring(xml_content)
            ns = {'iso': 'http://kb.nl/ns/isolyzer/v1/'}
            
            # Extract tool info
            tool_info = {}
            tool_element = root.find('iso:toolInfo', ns)
            if tool_element is not None:
                tool_info = {
                    'name': self._get_xml_text(tool_element, 'iso:toolName', ns),
                    'version': self._get_xml_text(tool_element, 'iso:toolVersion', ns)
                }
            
            # Extract image analysis
            image = root.find('iso:image', ns)
            if image is None:
                return {"error": "No image element found in XML"}
            
            # File info
            file_info = {}
            file_element = image.find('iso:fileInfo', ns)
            if file_element is not None:
                file_info = {
                    'name': self._get_xml_text(file_element, 'iso:fileName', ns),
                    'size_bytes': int(self._get_xml_text(file_element, 'iso:fileSizeInBytes', ns) or 0),
                    'last_modified': self._get_xml_text(file_element, 'iso:fileLastModified', ns)
                }
            
            # Status info
            status_info = {}
            status_element = image.find('iso:statusInfo', ns)
            if status_element is not None:
                status_info = {
                    'success': self._get_xml_text(status_element, 'iso:success', ns) == 'True'
                }
            
            # Tests
            tests = {}
            tests_element = image.find('iso:tests', ns)
            if tests_element is not None:
                contains_known_fs_text = self._get_xml_text(tests_element, 'iso:containsKnownFileSystem', ns)
                size_expected_text = self._get_xml_text(tests_element, 'iso:sizeExpected', ns)
                size_actual_text = self._get_xml_text(tests_element, 'iso:sizeActual', ns)
                size_difference_text = self._get_xml_text(tests_element, 'iso:sizeDifference', ns)
                size_diff_sectors_text = self._get_xml_text(tests_element, 'iso:sizeDifferenceSectors', ns)
                size_as_expected_text = self._get_xml_text(tests_element, 'iso:sizeAsExpected', ns)
                smaller_than_expected_text = self._get_xml_text(tests_element, 'iso:smallerThanExpected', ns)
                
                # Convert values to appropriate types
                try:
                    size_expected = int(size_expected_text) if size_expected_text else 0
                except ValueError:
                    size_expected = 0
                    
                try:
                    size_actual = int(size_actual_text) if size_actual_text else 0
                except ValueError:
                    size_actual = 0
                    
                try:
                    size_difference = int(size_difference_text) if size_difference_text else 0
                except ValueError:
                    size_difference = 0
                    
                try:
                    size_difference_sectors = float(size_diff_sectors_text) if size_diff_sectors_text else 0
                except ValueError:
                    size_difference_sectors = 0
                
                # Build the tests dictionary
                tests = {
                    'contains_known_filesystem': contains_known_fs_text == 'True',
                    'size_expected': size_expected,
                    'size_actual': size_actual,
                    'size_difference': size_difference,
                    'size_difference_sectors': size_difference_sectors,
                    'size_as_expected': size_as_expected_text == 'True',
                    'smaller_than_expected': smaller_than_expected_text == 'True'
                }
                
            # File systems
            filesystems = []
            fs_elements = image.findall('iso:fileSystems/iso:fileSystem', ns)
            
            for fs in fs_elements:
                fs_type = fs.get('TYPE')
                fs_data = {'type': fs_type}
                
                if fs_type == 'ISO 9660':
                    pvd = fs.find('iso:primaryVolumeDescriptor', ns)
                    if pvd is not None:
                        fs_data.update({
                            'volume_identifier': self._get_xml_text(pvd, 'iso:volumeIdentifier', ns),
                            'volume_creation_date': self._get_xml_text(pvd, 'iso:volumeCreationDateAndTime', ns),
                            'publisher': self._get_xml_text(pvd, 'iso:publisherIdentifier', ns),
                            'data_preparer': self._get_xml_text(pvd, 'iso:dataPreparerIdentifier', ns),
                            'logical_block_size': int(self._get_xml_text(pvd, 'iso:logicalBlockSize', ns) or 0),
                            'volume_space_size': int(self._get_xml_text(pvd, 'iso:volumeSpaceSize', ns) or 0)
                        })
                
                elif fs_type == 'UDF':
                    lvd = fs.find('iso:logicalVolumeDescriptor', ns)
                    if lvd is not None:
                        fs_data.update({
                            'logical_volume_identifier': self._get_xml_text(lvd, 'iso:logicalVolumeIdentifier', ns),
                            'logical_block_size': int(self._get_xml_text(lvd, 'iso:logicalBlockSize', ns) or 0),
                            'implementation': self._get_xml_text(lvd, 'iso:implementationIdentifier', ns)
                        })
                
                filesystems.append(fs_data)
            
            # Compile warnings
            warnings = []
            if not tests.get('size_as_expected', True):
                size_diff = tests.get('size_difference', 0)
                sectors = tests.get('size_difference_sectors', 0)
                if tests.get('smaller_than_expected', False):
                    warnings.append(
                        f"File size is smaller than expected by {size_diff} bytes ({sectors} sectors). "
                        f"This may indicate a truncated read — verify the ISO carefully before use."
                    )
                else:
                    warnings.append(
                        f"File size is {size_diff} bytes ({sectors} sectors) larger than the filesystem declares. "
                        f"This is normal for optical media — extra sectors are lead-out/padding and do not indicate data loss."
                    )
            
            # Build result
            result = {
                'tool_info': tool_info,
                'file_info': file_info,
                'valid_iso9660': any(fs['type'] == 'ISO 9660' for fs in filesystems),
                'has_udf': any(fs['type'] == 'UDF' for fs in filesystems),
                'status_success': status_info.get('success', False),
                'size_as_expected': tests.get('size_as_expected', False),
                'size_difference_bytes': tests.get('size_difference', 0),
                'size_difference_sectors': tests.get('size_difference_sectors', 0),
                'size_expected': tests.get('size_expected', 0),
                'size_actual': tests.get('size_actual', 0),
                'contains_known_filesystem': tests.get('contains_known_filesystem', True),
                'smaller_than_expected': tests.get('smaller_than_expected', False),
                'filesystems': filesystems,
                'tests': tests,
                'warnings': warnings,
            }
            
            # Remove 'raw_xml' since we're saving it to file now
            result.pop('raw_xml', None)
            
            return result
            
        except ET.ParseError as e:
            return {"error": f"XML parsing failed: {e}", "error_type": "xml_parse_error"}
        except Exception as e:
            return {"error": f"Unexpected parsing error: {e}", "error_type": "unknown_error"}
    
    def _get_xml_text(self, element, path: str, namespaces: dict) -> str:
        """Safely extract text from XML element"""
        found = element.find(path, namespaces)
        return found.text.strip() if found is not None and found.text else ""
    
    def _create_manifest(self, result: BackupResult, disk_info: Dict[str, Any], iso_analysis: Dict[str, Any],
                         metadata: Optional[RecordMetadata] = None):
        """Create comprehensive manifest file with improved organization
        (metadata: the run's ISO/isolyzer check shared with the log)"""
        # Get current timestamp
        now = datetime.datetime.now()
        record = RecordView.from_run(self.config, result)
        if metadata is None:
            metadata = RecordMetadata.check(record.current)
        
        # Extract tree listing summary
        tree_summary = self._extract_tree_summary(record.current.tree)
        
        # Calculate additional metrics
        # Get ISO file size (None when absent, or when it could not be checked)
        iso_file_size = metadata.iso_size
        
        # Build comprehensive manifest
        manifest = {
            "backup_metadata": {
                "run_id": self.run.run_id if self.run else f"{now.strftime('%Y%m%dT%H%M%S')}_{self.config.operator}_{self.config.disk_id}",
                "uuid": self.run.run_uuid if self.run else str(uuid4()),
                "tool_name": "Optical Disc ISO Backup Utility",
                "tool_version": "v14-streamlined",
                "created": now.isoformat(timespec="seconds"),
                "operator": self.config.operator,
                "dry_run": self.config.dry_run
            },
            
            "backup_status": {
                "overall_status": ("dry_run" if self.config.dry_run
                                   else (("success_with_warnings" if result.warnings else "success") if result.success
                                         else ("verification_failed" if result.checksum_match is False else "failed"))),
                # true/false: the ISO was/was not there; null: that could not be checked
                "iso_created": None if metadata.iso_problem else iso_file_size is not None,
                "verification_performed": result.md5_iso is not None,
                "verification_passed": result.checksum_match if result.checksum_match is not None else "skipped",
                "warnings": result.warnings,
                "errors": result.error_message if result.error_message else None
            },
            
            "source_disc": {
                "disk_identifier": self.config.disk_id,
                "device_path": f"/dev/{self.config.disk_id}",
                "raw_device_path": f"/dev/r{self.config.disk_id}",
                "volume_name": self.config.volume_name,
                "media_type": disk_info.get('OpticalMediaType', 'Unknown'),
                "filesystem": disk_info.get('FilesystemName', disk_info.get('FilesystemType', 'Unknown')),
                "drive_model": disk_info.get('MediaName', disk_info.get('IORegistryEntryName', 'Unknown')),
                "drive_capabilities": disk_info.get('OpticalDeviceType', 'Unknown'),
                "connection_type": disk_info.get('BusProtocol', 'Unknown'),
                "erasable": disk_info.get('OpticalMediaErasable', False),
                "read_only": not disk_info.get('Writable', True)
            },
            
            "volume_information": {
                "total_size_bytes": result.disk_size,
                "total_size_formatted": format_bytes(result.disk_size),
                "used_space_bytes": disk_info.get('VolumeSize', result.disk_size),
                "used_space_formatted": format_bytes(disk_info.get('VolumeSize', result.disk_size)),
                "allocation_block_size": disk_info.get('VolumeAllocationBlockSize', disk_info.get('DeviceBlockSize', 'Unknown')),
                "mount_point": disk_info.get('MountPoint', f"/Volumes/{self.config.volume_name}")
            },
            
            "output_files": {
                "iso_filename": record.files.iso.name,
                "iso_file_size_bytes": iso_file_size,
                "iso_file_size_formatted": format_bytes(iso_file_size) if iso_file_size else None,
                "output_directory": str(self.config.output_dir),
                "log_filename": record.files.log.name,
                "tree_filename": record.files.tree.name,
                "isolyzer_filename": record.files.isolyzer.name,
                "manifest_filename": record.files.manifest.name
            },
            
            "operations_performed": {
                "tree_listing": {
                    "command": shlex.join(tree_command(self.config)),
                    "output_file": record.files.tree.name,
                    "summary": tree_summary
                },
                "disk_unmount": {
                    "command": shlex.join(["diskutil", "unmountDisk", f"/dev/{self.config.disk_id}"]),
                    "status": ("interrupted" if result.unmount_interrupted else
                               ("skipped" if self.config.dry_run else "not_attempted")
                               if result.unmount_ok is None else ("success" if result.unmount_ok else "failed"))
                },
                "iso_creation": {
                    "command": (shlex.join(dd_command(self.config.disk_id))
                                if self.config.dry_run or result.creation_path else None),
                    "method": ("streamed copy with in-stream source hash calculation: makeiso.py "
                               "reads dd's output, writes the ISO from it and hashes it"),
                    "publish_method": (("run-private attempt ISO promoted to canonical filename after verification"
                                        if result.success else
                                        "run-private attempt ISO was published to the canonical filename, "
                                        "then publication failed and it could not be moved back: it "
                                        "remains at the canonical filename"
                                        if result.iso_path == self.config.canonical_output_path else
                                        "run-private attempt ISO was not promoted because the run failed")
                                       if result.creation_path and result.creation_path != self.config.canonical_output_path
                                       else "written directly to canonical filename"),
                    "block_size": "4MB",
                    "performed": bool(result.creation_path) and not self.config.dry_run
                },
                "verification": {
                    "method": "md5 + sha-256 hash comparison (written ISO re-read vs raw device stream)",
                    "algorithms": ["MD5", "SHA-256"],
                    "performed": result.md5_iso is not None,
                    "note": "The script does not perform a second raw-device read during verification; it compares the source hash captured during the copy stream to a re-read of the written ISO.",
                    "optional_manual_spot_check": spot_check_command(f"./{record.files.iso.name}",
                                                                     self.config.disk_id),
                    "optional_manual_spot_check_note": ("Not run by makeiso.py. Run it in the folder "
                                                        "that holds the ISO, with the disc back in the "
                                                        "drive under the same disk identifier (check "
                                                        "with diskutil list); it re-reads the whole disc.")
                },
                "finalization": {
                    "eject_command": shlex.join(["diskutil", "eject", f"/dev/{self.config.disk_id}"]),
                    "status": ("interrupted" if result.eject_interrupted else
                               "skipped" if self.config.dry_run or result.unmount_ok is not True
                               else ("completed" if result.finalized else "failed"))
                }
            },
            
            "timing_performance": {
                "iso_creation": {
                    "duration_seconds": result.creation_time,
                    "duration_formatted": format_duration(result.creation_time),
                    "average_speed_mb_s": result.speed_mb_s(result.creation_time),
                    "average_speed_formatted": f"{result.speed_mb_s(result.creation_time):.2f} MB/s",
                    "average_speed_multiplier": calculate_disc_speed_multiplier(result.speed_mb_s(result.creation_time), self.config.media_type)
                },
                "verification": {
                    "duration_seconds": result.verification_time,
                    "duration_formatted": format_duration(result.verification_time),
                    "note": "Written ISO re-read and hashed after creation"
                },
                "total_operation": {
                    "duration_seconds": result.total_time,
                    "duration_formatted": format_duration(result.total_time),
                    "efficiency_note": "Source hash computed in-stream during creation; verification adds one sequential read of the ISO"
                }
            },
            
            "integrity_verification": {
                "hash_algorithms": ["MD5", "SHA-256"],
                "iso_hash": result.md5_iso,
                "source_hash": result.md5_raw,
                "sha256_iso_hash": result.sha256_iso,
                "sha256_source_hash": result.sha256_raw,
                "hashes_match": result.checksum_match,
                "verification_method": f"Source hashed in-stream during creation; {reread_wording(result.cache_bypass)} and hashed",
                # What was applied, as recorded by the copy: null when not recorded
                "flush_before_verification": result.flush_method,
                "verification_read_cache_bypass": result.cache_bypass,
                "flush_note": flush_wording(result.flush_method),
                "integrity_status": "verified" if result.checksum_match else ("failed" if result.checksum_match is False else "not_performed"),
                "notes": "Re-read ISO matches the source stream (MD5 and SHA-256)" if result.checksum_match else None
            },
            
            "structural_analysis": {
                "tool_used": iso_analysis.get('tool_info', {}).get('name', 'isolyzer'),
                "tool_version": iso_analysis.get('tool_info', {}).get('version', 'unknown'),
                "analysis_performed": not iso_analysis.get('skipped', False) and 'error' not in iso_analysis,
                "analysis_successful": iso_analysis.get('status_success', False),
                "valid_iso9660": iso_analysis.get('valid_iso9660', False),
                "contains_udf": iso_analysis.get('has_udf', False),
                "size_validation": {
                    "size_as_expected": iso_analysis.get('size_as_expected', True),
                    "size_expected_bytes": iso_analysis.get('size_expected', 0),
                    "size_actual_bytes": iso_analysis.get('size_actual', 0),
                    "size_difference_bytes": iso_analysis.get('size_difference_bytes', 0),
                    "size_difference_sectors": iso_analysis.get('size_difference_sectors', 0),
                    "smaller_than_expected": iso_analysis.get('smaller_than_expected', False),
                    "contains_known_filesystem": iso_analysis.get('contains_known_filesystem', True),
                    "size_expected_formatted": f"{iso_analysis.get('size_expected', 0)/1024/1024:.1f} MB",
                    "size_actual_formatted": f"{iso_analysis.get('size_actual', 0)/1024/1024:.1f} MB",
                    "note": "Small differences (1-2 sectors) are normal for optical media" if iso_analysis.get('size_difference_bytes', 0) > 0 else None
                },
                "filesystems_detected": self._extract_filesystem_info(iso_analysis),
                "warnings": iso_analysis.get('warnings', []),
                "error": iso_analysis.get('error') if 'error' in iso_analysis else None,
                "xml_field_mappings": {
                    "note": "This section documents which XML elements from the raw isolyzer output map to the fields above",
                    "tool_info": {
                        "tool_used": "/isolyzer/toolInfo/toolName",
                        "tool_version": "/isolyzer/toolInfo/toolVersion"
                    },
                    "status_fields": {
                        "analysis_successful": "/isolyzer/image/statusInfo/success"
                    },
                    "test_fields": {
                        "contains_known_filesystem": "/isolyzer/image/tests/containsKnownFileSystem",
                        "size_expected_bytes": "/isolyzer/image/tests/sizeExpected",
                        "size_actual_bytes": "/isolyzer/image/tests/sizeActual",
                        "size_difference_bytes": "/isolyzer/image/tests/sizeDifference",
                        "size_difference_sectors": "/isolyzer/image/tests/sizeDifferenceSectors",
                        "size_as_expected": "/isolyzer/image/tests/sizeAsExpected",
                        "smaller_than_expected": "/isolyzer/image/tests/smallerThanExpected",
                        "valid_iso9660": "presence of /isolyzer/image/fileSystems/fileSystem[@TYPE='ISO 9660']",
                        "contains_udf": "presence of /isolyzer/image/fileSystems/fileSystem[@TYPE='UDF']"
                    },
                    "iso9660_fields": {
                        "volume_identifier": "/isolyzer/image/fileSystems/fileSystem[@TYPE='ISO 9660']/primaryVolumeDescriptor/volumeIdentifier",
                        "creation_date": "/isolyzer/image/fileSystems/fileSystem[@TYPE='ISO 9660']/primaryVolumeDescriptor/volumeCreationDateAndTime",
                        "publisher": "/isolyzer/image/fileSystems/fileSystem[@TYPE='ISO 9660']/primaryVolumeDescriptor/publisherIdentifier",
                        "data_preparer": "/isolyzer/image/fileSystems/fileSystem[@TYPE='ISO 9660']/primaryVolumeDescriptor/dataPreparerIdentifier",
                        "logical_block_size": "/isolyzer/image/fileSystems/fileSystem[@TYPE='ISO 9660']/primaryVolumeDescriptor/logicalBlockSize",
                        "volume_space_size": "/isolyzer/image/fileSystems/fileSystem[@TYPE='ISO 9660']/primaryVolumeDescriptor/volumeSpaceSize"
                    },
                    "udf_fields": {
                        "logical_volume_identifier": "/isolyzer/image/fileSystems/fileSystem[@TYPE='UDF']/logicalVolumeDescriptor/logicalVolumeIdentifier",
                        "logical_block_size": "/isolyzer/image/fileSystems/fileSystem[@TYPE='UDF']/logicalVolumeDescriptor/logicalBlockSize",
                        "implementation": "/isolyzer/image/fileSystems/fileSystem[@TYPE='UDF']/logicalVolumeDescriptor/implementationIdentifier"
                    }
                }
            },
            
            "system_environment": {
                "operating_system": platform.system(),
                "os_version": platform.release(),
                "kernel_version": platform.version(),
                "machine_architecture": platform.machine(),
                "processor": platform.processor(),
                "hostname": platform.node(),
                "python_version": platform.python_version(),
                "user_context": "sudo required for raw device access"
            },
            
            "technical_details": {
                "source_device_properties": {
                    # Essential disk properties only
                    "device_identifier": disk_info.get('DeviceIdentifier'),
                    "device_node": disk_info.get('DeviceNode'),
                    "ejectable": disk_info.get('Ejectable'),
                    "removable": disk_info.get('Removable'),
                    "internal": disk_info.get('Internal'),
                    "smart_status": disk_info.get('SMARTStatus'),
                    "bootable": disk_info.get('Bootable'),
                    "writable": disk_info.get('Writable')
                },
                "backup_parameters": {
                    "dd_block_size": "4MB",
                    "read_device": f"/dev/r{self.config.disk_id}",
                    "write_destination": (str(result.creation_path or self.config.output_path)
                                          if self.config.dry_run or result.creation_path else None),
                    "final_destination": str(record.files.iso),
                    "parallel_processing": True,
                    "verification_during_creation": False,
                    "error_handling": "standard dd error handling"
                }
            },
            
            "quality_assurance": {
                "verification_levels": [
                    {
                        "type": "bit_perfect_copy",
                        "method": "MD5 + SHA-256 hash comparison",
                        "status": "verified" if result.checksum_match else ("failed" if result.checksum_match is False else "not_performed"),
                        "confidence": "100%" if result.checksum_match else ("0%" if result.checksum_match is False else "n/a")
                    },
                    {
                        "type": "structural_integrity", 
                        "method": "isolyzer analysis",
                        "status": "verified" if iso_analysis.get('valid_iso9660') and iso_analysis.get('status_success') else ("failed" if iso_analysis.get('error') else "not_performed"),
                        "confidence": "high" if iso_analysis.get('valid_iso9660') else "unknown"
                    }
                ],
                "recommended_verifications": [
                    "Mount created ISO and compare file structure",
                    "Test ISO in target environment", 
                    "Verify specific files if critical data",
                    "Check isolyzer analysis for structural issues"
                ],
                "backup_best_practices": [
                    "Store ISO in multiple locations",
                    "Create checksum file for long-term storage",
                    "Document any disc damage or read errors",
                    "Keep isolyzer analysis for format validation"
                ],
                "manifest_notes": "This manifest provides comprehensive backup documentation including both bit-level and structural verification for audit and validation purposes"
            }
        }
        
        # Write manifest with proper formatting
        with (self._create_dry_run_file(self.config.manifest_path) if self.config.dry_run
              else open(self.config.manifest_path, "w")) as f:
            json.dump(manifest, f, indent=2, sort_keys=False)

        log(colorize("cyan", "Manifest saved to:") + " " + colorize("yellow", str(self.config.manifest_path)))
    
    def _extract_filesystem_info(self, iso_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract filesystem information from isolyzer analysis"""
        filesystems = []
        
        for fs in iso_analysis.get('filesystems', []):
            fs_info = {
                "type": fs.get('type'),
                "details": {}
            }
            
            if fs.get('type') == 'ISO 9660':
                fs_info['details'] = {
                    "volume_identifier": fs.get('volume_identifier'),
                    "creation_date": fs.get('volume_creation_date'),
                    "publisher": fs.get('publisher'),
                    "data_preparer": fs.get('data_preparer'),
                    "logical_block_size": fs.get('logical_block_size'),
                    "volume_space_size": fs.get('volume_space_size')
                }
            elif fs.get('type') == 'UDF':
                fs_info['details'] = {
                    "logical_volume_identifier": fs.get('logical_volume_identifier'),
                    "logical_block_size": fs.get('logical_block_size'),
                    "implementation": fs.get('implementation')
                }
            
            # Remove None values
            fs_info['details'] = {k: v for k, v in fs_info['details'].items() if v is not None}
            filesystems.append(fs_info)
        
        return filesystems

    def _analyze_iso_structure(self, iso_path: Path) -> Dict[str, Any]:
        """Analyze ISO structure with isolyzer"""
        log_divider("Analyzing ISO Structure")
        if self.run:
            self.run.mark("isolyzer")
        log(colorize("cyan", "Running isolyzer analysis..."))
        
        try:
            # Simplify the command to just call isolyzer with the file path
            # since isolyzer outputs XML by default
            cmd = ["isolyzer", str(iso_path)]
            log(colorize("cyan", "Running command:") + " " + colorize("yellow", shlex.join(cmd)))
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            # Save raw XML output to file if successful
            with open(self.config.isolyzer_path, 'w', encoding='utf-8') as f:
                f.write(result.stdout)
            log(colorize("cyan", "Isolyzer XML saved to:") + " " + colorize("yellow", str(self.config.isolyzer_path)))
            
            # Parse XML output
            analysis = self._parse_isolyzer_xml(result.stdout)
            
            # Display key results
            log(colorize("green", "ISO analysis complete"))
            log(colorize("cyan", "File System Tests:"))
            log(colorize("cyan", "  Contains Known File System:") + " " + colorize("yellow", str(analysis.get('contains_known_filesystem', 'Unknown'))))
            
            # Display sizes in bytes rather than MB
            size_expected = analysis.get('size_expected', 0)
            size_actual = analysis.get('size_actual', 0)
            log(colorize("cyan", "  Expected Size:") + " " + colorize("yellow", f"{size_expected} bytes"))
            log(colorize("cyan", "  Actual Size:") + " " + colorize("yellow", f"{size_actual} bytes"))
            
            # Continue with other output
            log(colorize("cyan", "  Size Difference:") + " " + colorize("yellow", f"{analysis.get('size_difference_bytes', 0)} bytes ({analysis.get('size_difference_sectors', 0)} sectors)"))
            log(colorize("cyan", "  Size as Expected:") + " " + colorize("yellow", str(analysis.get('size_as_expected', 'Unknown'))))
            log(colorize("cyan", "  Smaller Than Expected:") + " " + colorize("yellow", str(analysis.get('smaller_than_expected', 'Unknown'))))
            log(colorize("cyan", "  Valid ISO 9660:") + " " + colorize("yellow", str(analysis.get('valid_iso9660', 'Unknown'))))
            log(colorize("cyan", "  Contains UDF:") + " " + colorize("yellow", str(analysis.get('has_udf', 'Unknown'))))
            
            # Show size difference note / warning
            size_diff = analysis.get('size_difference_bytes', 0)
            smaller = analysis.get('smaller_than_expected', False)
            size_as_expected = analysis.get('size_as_expected', True)

            if size_as_expected:
                log(colorize("green", "  Size check: ISO size matches expected size exactly."))
            elif smaller:
                log(colorize("red",    f"  Size check: WARNING — ISO is smaller than expected by {size_diff} bytes ({analysis.get('size_difference_sectors', 0)} sectors)."))
                log(colorize("red",    "  This may indicate a truncated read. Verify the ISO carefully before use."))
            else:
                log(colorize("yellow", f"  Size check: ISO is {size_diff} bytes ({analysis.get('size_difference_sectors', 0)} sectors) larger than the filesystem declares."))
                log(colorize("yellow",  "  This is normal for optical media — extra sectors are lead-out/padding and do not indicate data loss."))
            
            return analysis
            
        except subprocess.CalledProcessError as e:
            log(colorize("red", f"isolyzer failed: {e}"))
            # Check if isolyzer is installed
            try:
                subprocess.run(["which", "isolyzer"], check=True, capture_output=True)
            except subprocess.CalledProcessError:
                log(colorize("yellow", "isolyzer command not found. Please install with:"))
                log(colorize("yellow", "pip install isolyzer"))
            
            # Try to create a minimal XML file for tracking purposes
            with open(self.config.isolyzer_path, 'w', encoding='utf-8') as f:
                f.write('<?xml version="1.0" ?>\n')
                f.write('<isolyzer xmlns="http://kb.nl/ns/isolyzer/v1/">\n')
                f.write('  <toolInfo>\n')
                f.write('    <toolName>isolyzer</toolName>\n')
                f.write('    <toolVersion>unknown</toolVersion>\n')
                f.write('  </toolInfo>\n')
                f.write('  <image>\n')
                f.write('    <statusInfo>\n')
                f.write('      <success>False</success>\n')
                f.write(f'      <error>{xml_escape(str(e))}</error>\n')
                f.write('    </statusInfo>\n')
                f.write('  </image>\n')
                f.write('</isolyzer>\n')
            log(colorize("cyan", "Created minimal XML for tracking"))
            
            return {"error": str(e), "error_type": "execution_failed"}
        except FileNotFoundError:
            log(colorize("yellow", "isolyzer not found - skipping structural analysis"))
            log(colorize("cyan", "Tip:") + " Install isolyzer with: " + colorize("yellow", "pip install isolyzer"))
            return {"error": "isolyzer not installed", "error_type": "not_found"}
        except Exception as e:
            log(colorize("red", f"Unexpected error during ISO analysis: {e}"))
            return {"error": str(e), "error_type": "parsing_failed"}
        
    def _extract_tree_summary(self, tree_path: Path) -> Dict[str, Any]:
        """Extract summary from the tree listing at tree_path"""
        try:
            with open(tree_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                for line in reversed(lines):
                    stripped = line.strip()
                    if "directories" in stripped and "files" in stripped:
                        # Matches both formats:
                        #   "X used in N directories, M files"  (tree --si)
                        #   "Total: N directories, M files, X"  (tree default)
                        match = re.search(r"(\d+)\s+directories,\s+(\d+)\s+files", stripped)
                        size_match = re.search(r"([\d.]+\s*\w+)\s+used in", stripped)
                        if match:
                            result = {
                                "total_directories": int(match.group(1)),
                                "total_files": int(match.group(2)),
                            }
                            if size_match:
                                result["reported_size"] = size_match.group(1)
                            return result
            return {}
        except Exception as e:
            return {"error": str(e)}
    
### === HELPER FUNCTIONS ===

def list_disks() -> str:
    """List available disks"""
    try:
        return run_cmd(["diskutil", "list"])
    except subprocess.CalledProcessError as e:
        log(colorize("red", f"Failed to list disks: {e}"))
        return ""

def print_help():
    """Print custom colored help message"""
    print()  # Blank line before usage
    
    help_text = f"""
{colorize('blue', 'OPTICAL DISC TO ISO BACKUP UTILITY')}
{colorize('cyan', 'Creates bit-perfect ISO backups with verification')}

{colorize('yellow', 'USAGE:')}
  sudo python3 makeiso.py [options]

{colorize('yellow', 'OPTIONS:')}
  {colorize('green', '-h, --help')}              Show this help message and exit
  {colorize('green', '--dry-run')}               Rehearse without an ISO; still writes tree, log and manifest
  {colorize('green', '--force')}                 Skip the optical-media safety check
  {colorize('green', '--filename NAME')}         ISO filename (without extension)
  {colorize('green', '--dir PATH')}              Output directory path (can use ~)
  {colorize('green', '--operator NAME')}         Operator name or initials

{colorize('yellow', 'EXAMPLES:')}
  {colorize('cyan', '# Interactive mode - prompts for all inputs')}
  sudo python3 makeiso.py

  {colorize('cyan', '# Specify filename, directory, and operator')}
  sudo python3 makeiso.py --filename MyDisc --dir ~/Backups --operator JD

  {colorize('cyan', '# Rehearsal without an ISO (writes tree, log and manifest; see DRY RUNS)')}
  sudo python3 makeiso.py --dry-run

{colorize('yellow', 'KEY FEATURES:')}
  • {colorize('green', 'Parallel processing:')} Creates ISO while calculating the source checksum
  • {colorize('green', 'Progress tracking:')} Real-time speed and remaining time
  • {colorize('green', 'Comprehensive logging:')} Detailed logs saved to .log.txt
  • {colorize('green', 'Metadata manifest:')} JSON file with complete backup metadata
  • {colorize('green', 'Tree listing:')} Directory structure saved to .txt file
  • {colorize('green', 'Cross-verification:')} Compares ISO and raw disk MD5 and SHA-256 hashes

{colorize('yellow', 'REPLACING AN EXISTING BUNDLE:')}
  Nothing is touched until the new copy verifies. Then the current bundle is
  moved, unchanged, into {colorize('cyan', '<name>_overwritten-<stamp>-<rand>/')} and the new one is
  published. Ctrl-C is held during those few renames, so it cannot stop them
  halfway; if a rename fails they are rolled back as far as the filesystem
  allows, and the records say where every file is.

{colorize('yellow', 'DRY RUNS:')}
  A --dry-run makes no ISO, but writes its own tree listing, log and manifest
  under the canonical names. It is refused (exit 1, nothing written) if any
  <name>.iso, .iso.log.txt, _manifest.json, _tree.txt or _isolyzer.xml is
  already there, as a file, folder or symlink: choose a fresh --dir or
  --filename. Other files in the folder are left alone. A dry run that fails
  exits 1 and lists the files it created, which may be incomplete; remove only
  those. Ctrl-C simply stops it and can leave them incomplete too. Under sudo,
  the files a dry run creates are handed to you, but a folder it creates stays
  owned by root; create the folder yourself first to manage it without sudo.

{colorize('yellow', 'EXIT STATUS:')}
  0 success; 1 failure (including a refused or failed dry run); 2 checksum
  mismatch; 130 a Ctrl-C was received but the master had verified and was
  kept (only an interrupted eject is also recorded, as a warning). In a
  --dry-run, Ctrl-C simply stops the run.

{colorize('red', 'IMPORTANT:')} This script must be run with sudo privileges.
"""
    print(help_text)

def parse_args() -> argparse.Namespace:
    """Parse command line arguments"""
    # Check for help first to show our custom help
    if '--help' in sys.argv or '-h' in sys.argv:
        print_help()
        sys.exit(0)
    
    parser = argparse.ArgumentParser(
        prog='makeiso.py',
        description='Optical Disc to ISO Backup Utility',
        add_help=False  # Disable default help to use our custom one
    )
    
    parser.add_argument(
        '--dry-run', 
        action='store_true', 
        help='Run without writing ISO or verifying checksum'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Skip the optical-media safety check'
    )
    
    parser.add_argument(
        '--filename', 
        type=str, 
        metavar='NAME',
        help='ISO filename without extension'
    )
    
    parser.add_argument(
        '--dir', 
        type=str, 
        metavar='PATH',
        help='Output directory path'
    )
    
    parser.add_argument(
        '--operator', 
        type=str, 
        metavar='NAME',
        help='Operator name or initials'
    )
    
    return parser.parse_args()

def gather_user_inputs(args: argparse.Namespace) -> BackupConfig:
    """Gather all necessary inputs from user and command line"""
    # Get disk ID
    disk_id = get_input(colorize("cyan", "Enter disk ID (e.g. disk2): "))
    # Whole-disk identifiers only: this tool images/unmounts/ejects an entire
    # optical disc, so reject partition slices (diskNsM) and anything that could
    # turn into a traversing device path like /dev/rdisk2/../... reaching dd.
    if not re.fullmatch(r"disk\d+", disk_id):
        log(colorize("red", f"Invalid disk ID: {disk_id!r}. Expected a whole-disk "
                            f"identifier like 'disk2' (no partition slice, no path)."))
        sys.exit(1)

    # One plist fetch covers volume name, size, and media type (the same
    # structured source create_backup() uses, rather than scraping text output)
    volume_name = "Unknown"
    try:
        disk_info = run_cmd(["diskutil", "info", "-plist", f"/dev/{disk_id}"], plist=True)
        volume_name = disk_info.get("VolumeName") or "Unknown"
        log(colorize("cyan", "Volume name detected:") + " " + colorize("yellow", volume_name))
        disk_size_mb = disk_info.get("TotalSize", 0) / (1024 * 1024)
        log(colorize("cyan", "Volume size:") + " " + colorize("yellow", f"{disk_size_mb:.0f} MB"))
        media_type = disk_info.get("OpticalMediaType", "Unknown")
        log(colorize("cyan", "Media type:") + " " + colorize("yellow", media_type))
    except Exception:
        log(colorize("yellow", f"Could not read disk info for /dev/{disk_id} (re-checked at backup start)."))
    
    # Get other inputs
    filename = args.filename or get_input(colorize("cyan", "Enter output ISO filename (no extension): "))
    # The filename becomes a directory component and its files are chowned as
    # root later; require a simple basename so it cannot point outside the
    # output directory (rejects '..', absolute paths, and anything with '/').
    if (not filename or filename != Path(filename).name or filename in (".", "..")
            or filename.startswith(("-", "~"))):
        log(colorize("red", f"Invalid filename: {filename!r}. Use a simple name with no path separators "
                            f"that does not start with '-' or '~'."))
        sys.exit(1)
    base_dir = Path(args.dir or get_input(colorize("cyan", "Enter output directory: "))).expanduser()
    operator = args.operator or get_input(colorize("cyan", "Enter operator name or initials: "))
    
    # Create output directory path
    output_dir = base_dir / filename
    
    return BackupConfig(
        disk_id=disk_id,
        volume_name=volume_name,
        filename=filename,
        operator=operator,
        output_dir=output_dir,
        dry_run=args.dry_run,
        force=args.force
    )

### === MAIN FUNCTION ===

def main():
    """Main entry point"""
    # Parse command line arguments
    args = parse_args()
    
    # First show the title divider after a blank line (right after sudo password)
    log("")
    log_divider("Optical Disc to ISO Backup Utility")
    
    # Then show available disks
    log("Available disks:")
    output = list_disks()
    for line in output.strip().splitlines():
        log(line)
    
    # Gather user inputs
    config = gather_user_inputs(args)
    
    # Create and run backup
    backup = OpticalDiscBackup(config)
    result = backup.create_backup()
    
    # Exit with appropriate code. A failure keeps its own code even if Ctrl-C
    # was pressed; a run whose master verified but that saw a Ctrl-C (during
    # the eject, held during publication, or after recording) exits 130.
    if result.success:
        if config.dry_run:
            log(colorize("green", "Dry run completed — no ISO was created."))
        else:
            log(colorize("green", "Backup completed successfully!"))
        if backup.interrupted and not config.dry_run:
            log(colorize("yellow", "Ctrl-C was received, but the master had verified and was "
                                   "kept (an interrupted eject is also recorded as a warning). "
                                   "Exit status 130."))
            sys.exit(130)
        sys.exit(0)
    elif result.checksum_match is False:
        log(colorize("red", "Backup failed verification: checksum mismatch!"))
        sys.exit(2)  # Distinct exit code for verification failure
    else:
        log(colorize("red", result.error_message if config.dry_run
                     else f"Backup failed: {result.error_message}"))
        sys.exit(1)

### === SCRIPT ENTRYPOINT ===

if __name__ == "__main__":
    main()
