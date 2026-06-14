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
import platform
import argparse
import json
import logging
import fcntl
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape

### === DATA STRUCTURES ===

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
    warnings: List[str] = field(default_factory=list)  # non-fatal problems (run still valid)
    
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
                        iso_analysis: Dict[str, Any] = None, run: RunContext = None):
    """Create a properly formatted log file"""

    end_time = datetime.datetime.now()
    total_duration = (end_time - start_time).total_seconds()

    def step_time(step: str, fallback: datetime.datetime) -> str:
        """Real captured timestamp for a step, or the fallback if it never ran"""
        t = run.step_times.get(step, fallback) if run else fallback
        return t.strftime('%H:%M:%S')
    
    with open(log_path, "w") as f:
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
        f.write(f"ISO File:         {result.iso_path.name}\n")
        f.write(f"Output Directory: {config.output_dir}\n")
        f.write(f"Log File:         {log_path.name}\n")
        f.write(f"Tree File:        {config.tree_path.name}\n")
        f.write(f"Isolyzer File:    {config.isolyzer_path.name}\n")
        f.write(f"Manifest File:    {config.manifest_path.name}\n")
        if result.iso_path.exists():
            iso_size = result.iso_path.stat().st_size
            f.write(f"ISO File Size:    {iso_size / (1024 * 1024):.0f} MB\n")
        f.write("\n")
        
        # Rest of the log formatting remains the same...
        # === OPERATION DETAILS ===
        f.write("OPERATION DETAILS\n")
        f.write("-" * 20 + "\n")
        
        # Tree generation
        f.write(f"[{step_time('tree', start_time)}] Tree Listing Generation\n")
        tree_target = config.mount_point or f"/Volumes/{config.volume_name}"
        f.write(f"  Command: tree -RapugD --si --du {tree_target}\n")
        f.write(f"  Output:  {config.tree_path.name}\n\n")
        
        # Disk unmount
        if result.unmount_ok is None:
            unmount_status = "Skipped (dry run)"
        else:
            unmount_status = "Success" if result.unmount_ok else "FAILED"
        f.write(f"[{step_time('unmount', start_time)}] Disk Unmount\n")
        f.write(f"  Command: diskutil unmountDisk /dev/{config.disk_id}\n")
        f.write(f"  Status:  {unmount_status}\n\n")
        
        # ISO Creation and Verification (real captured times; approximation
        # only as fallback for steps that never ran)
        create_start = (run.step_times.get("copy_start") if run else None) or (start_time + datetime.timedelta(seconds=1))
        create_end = (run.step_times.get("copy_end") if run else None) or (create_start + datetime.timedelta(seconds=result.creation_time))
        
        f.write(f"[{create_start.strftime('%H:%M:%S')}] ISO Creation\n")
        if config.dry_run:
            f.write(f"  Status:  Skipped (dry run)\n\n")
        elif result.unmount_ok is False:
            f.write(f"  Status:  Not started ({result.error_message or 'aborted before copy'})\n\n")
        else:
            creation_path = result.creation_path or config.output_path
            f.write(f"  Command: dd if=/dev/r{config.disk_id} of={creation_path} bs=4m\n")
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
            f.write(f"Method:         Written ISO re-read from disk, compared to streamed raw disk hashes\n")
            f.write(f"MD5 (ISO):          {result.md5_iso}\n")
            f.write(f"MD5 (Raw Disk):     {result.md5_raw}\n")
            if result.sha256_iso and result.sha256_raw:
                f.write(f"SHA-256 (ISO):      {result.sha256_iso}\n")
                f.write(f"SHA-256 (Raw Disk): {result.sha256_raw}\n")
            algorithms = "MD5 (128-bit), SHA-256 (256-bit)" if result.sha256_iso else "MD5 (128-bit)"
            f.write(f"Match:          {'YES' if result.checksum_match else 'NO'}\n")
            f.write(f"Algorithms:     {algorithms}\n")
            f.write(f"Duration:       {format_duration(result.verification_time)}\n\n")
        
        # ISO Structure Analysis
        if config.isolyzer_path.exists():
            f.write(f"[{step_time('isolyzer', create_end)}] ISO Structure Analysis\n")
            f.write(f"  Tool:    isolyzer\n")
            f.write(f"  Output:  {config.isolyzer_path.name}\n")
            
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
        f.write(f"  Eject:   diskutil eject /dev/{config.disk_id}\n")
        if config.dry_run:
            final_status = "Skipped (dry run)"
        elif result.finalized:
            final_status = "Complete"
        elif result.unmount_ok is False:
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
            # Physical Size = actual ISO/disc size; TotalSize in plist returns volume size on optical media
            physical_size = result.iso_path.stat().st_size if result.iso_path.exists() else disk_info.get('TotalSize')
            volume_size = disk_info.get('TotalSize')
            fmt_field("Physical Size",      fmt_diskutil_size(physical_size))
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

### === MAIN BACKUP CLASS ===

class OpticalDiscBackup:
    """Main class for optical disc backup operations"""
    
    def __init__(self, config: BackupConfig):
        self.config = config
        self.run: Optional[RunContext] = None
        self._progress_marks = set()  # deciles already reported in non-TTY mode

    def _standard_artifacts(self) -> List[Path]:
        """Canonical artifacts that a successful run publishes."""
        return [
            self.config.canonical_output_path,
            self.config.canonical_log_path,
            self.config.canonical_manifest_path,
            self.config.canonical_tree_path,
            self.config.canonical_isolyzer_path,
        ]

    def _archive_existing_artifacts(self) -> List[Path]:
        """Move any existing canonical artifacts aside before publishing a
        verified replacement. Called only after the new ISO has verified, so a
        failed retry leaves the existing master untouched."""
        token = f"overwritten-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"
        archived = []
        for path in self._standard_artifacts():
            if not path.exists():
                continue
            archive_name = path.name.replace(self.config.filename, f"{self.config.filename}_{token}", 1)
            archive_path = path.with_name(archive_name)
            path.rename(archive_path)
            archived.append(archive_path)
            log(colorize("yellow", f"Existing artifact archived to: {archive_path.name}"))
        return archived

    def _promote_successful_attempt(self, result: BackupResult,
                                    attempt_iso: Path, attempt_tree: Path,
                                    attempt_isolyzer: Path):
        """Publish verified attempt artifacts at the canonical names.

        The old canonical artifacts are archived only after the new copy has
        passed completeness and checksum verification; if this promotion fails,
        the attempt falls through the normal failed-run record path."""
        self._archive_existing_artifacts()
        for src, dst in (
            (attempt_tree, self.config.canonical_tree_path),
            (attempt_isolyzer, self.config.canonical_isolyzer_path),
            (attempt_iso, self.config.canonical_output_path),
        ):
            if src.exists():
                src.replace(dst)
                log(colorize("green", f"Published verified artifact: {dst.name}"))
        self.config.attempt_token = None
        result.iso_path = self.config.canonical_output_path
        
    def create_backup(self) -> BackupResult:
        """Main entry point for creating backup"""
        start_time = datetime.datetime.now()
        self.run = RunContext(
            run_id=f"{start_time.strftime('%Y%m%dT%H%M%S')}_{self.config.operator}_{self.config.disk_id}",
            run_uuid=str(uuid4()),
            start_time=start_time,
        )
        
        # No title divider here - already shown in main()
        
        # Validate we're running as root
        if os.geteuid() != 0:
            log(colorize("red", "This script must be run with sudo."))
            return BackupResult(False, self.config.output_path, 0, error_message="Not running as root")
        
        # Get disk info
        disk_info = self._get_disk_info()
        if not disk_info:
            return BackupResult(False, self.config.output_path, 0, error_message=f"Could not get disk info for {self.config.disk_id}")
        
        disk_size = disk_info.get("TotalSize", 0)
        self.config.media_type = disk_info.get("OpticalMediaType", "Unknown")
        self.config.mount_point = disk_info.get("MountPoint") or f"/Volumes/{self.config.volume_name}"

        # Safety guard: refuse to image anything diskutil does not identify as
        # optical media, so a mistyped disk ID can't unmount and read an
        # internal or external drive. --force overrides for drives that
        # misreport their media type.
        if not disk_info.get("OpticalMediaType") and not self.config.force:
            device_desc = disk_info.get('MediaName', disk_info.get('IORegistryEntryName', 'unknown device'))
            log(colorize("red", f"/dev/{self.config.disk_id} does not appear to be an optical disc "
                                f"(device: {device_desc}). Refusing to continue. Use --force to override."))
            return BackupResult(False, self.config.output_path, disk_size,
                                error_message=f"/dev/{self.config.disk_id} is not optical media")

        # Create output directory, remembering whether this run created it so
        # we never chown a pre-existing directory we don't own (e.g. /tmp).
        # mkdir without exist_ok is atomic: no check-then-create race.
        try:
            self.config.output_dir.mkdir(parents=True)
            created_output_dir = True
        except FileExistsError:
            created_output_dir = False
        # A pre-existing path must be a real directory — a regular file or
        # symlink here would fail late (mid-run, after the disc is unmounted)
        if self.config.output_dir.is_symlink() or not self.config.output_dir.is_dir():
            log(colorize("red", f"Output path {self.config.output_dir} exists but is not a real directory "
                                f"(file or symlink). Refusing to continue."))
            return BackupResult(False, self.config.output_path, disk_size,
                                error_message=f"Output path {self.config.output_dir} is not a directory")

        # Likewise refuse to write through a pre-existing symlink at any
        # planned artifact path — running as root, following one could
        # clobber an arbitrary file elsewhere on the system.
        planned = self._standard_artifacts()
        linked = [p.name for p in planned if p.is_symlink()]
        if linked:
            log(colorize("red", f"Refusing to continue: symlink(s) at planned output path(s): {', '.join(linked)}"))
            return BackupResult(False, self.config.output_path, disk_size,
                                error_message=f"Symlink at planned output path(s): {', '.join(linked)}")

        if self.config.dry_run:
            replace_candidates = [
                self.config.canonical_log_path,
                self.config.canonical_manifest_path,
                self.config.canonical_tree_path,
            ]
        else:
            replace_candidates = planned
        existing_artifacts = [p for p in replace_candidates if p.exists()]
        if existing_artifacts and not self._confirm_overwrite(existing_artifacts):
            names = ", ".join(p.name for p in existing_artifacts)
            return BackupResult(False, self.config.output_path, disk_size,
                                error_message=f"Aborted to avoid replacing existing artifact(s): {names}")

        # All new artifacts write to run-private attempt paths first. Existing
        # masters/sidecars are not touched unless this attempt verifies and is
        # promoted below.
        if not self.config.dry_run:
            self.config.attempt_token = f"attempt-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"
        attempt_iso_path = self.config.output_path
        attempt_tree_path = self.config.tree_path
        attempt_isolyzer_path = self.config.isolyzer_path

        # Generate tree listing (this is the first divider after user inputs)
        self.run.mark("tree")
        tree_ok = self._generate_tree_listing()
        
        # Unmount disk; record the failure rather than dd a mounted device.
        # The disc is left as-is (no eject) since it never unmounted.
        backup_error = None
        unmount_ok = None
        finalized = None
        if not self.config.dry_run:
            self.run.mark("unmount")
            unmount_ok = self._unmount_disk()
            if not unmount_ok:
                backup_error = f"Failed to unmount /dev/{self.config.disk_id}"

        # Create ISO (with in-stream source hashing), then verify by re-reading it.
        # Wrapped so the disc is always ejected and the run is always
        # documented, even when the copy or verification fails partway.
        iso_result = None
        iso_analysis = {"skipped": True, "reason": backup_error or "not started"}
        if backup_error is None:
            try:
                if self.config.dry_run:
                    iso_analysis = {"skipped": True, "reason": "dry run"}
                    log(colorize("cyan", "Dry run:") + " " + colorize("yellow", "Skipping .iso creation and verification."))
                else:
                    iso_result = self._create_and_verify_iso(disk_size)
                    # Analyze ISO structure
                    iso_analysis = self._analyze_iso_structure(self.config.output_path)
            except (Exception, KeyboardInterrupt) as e:
                backup_error = "Interrupted by operator (Ctrl-C)" if isinstance(e, KeyboardInterrupt) else str(e)
                iso_analysis = {"skipped": True, "reason": f"backup failed: {backup_error}"}
                log(colorize("red", f"Backup failed: {backup_error}"))
            finally:
                # Eject the disc even if the backup failed
                self.run.mark("finalize")
                finalized = self._finalize_disk()

        # A structurally short ISO is a failed master, not a warning: isolyzer
        # found fewer bytes than the filesystem itself declares, which means a
        # truncated read. (Larger-than-expected is normal optical lead-out
        # padding and stays a benign note.)
        if (backup_error is None and not self.config.dry_run
                and iso_analysis.get('smaller_than_expected')):
            backup_error = (f"ISO is structurally smaller than the filesystem declares by "
                            f"{iso_analysis.get('size_difference_bytes', 0)} bytes — "
                            f"truncated read, master not certified")
            log(colorize("red", f"Backup failed: {backup_error}"))

        # Create result
        result = BackupResult(
            success=backup_error is None,
            iso_path=self.config.output_path,
            disk_size=disk_size,
            creation_path=(iso_result.output_path if iso_result else
                           (attempt_iso_path if attempt_iso_path.exists() else None)),
            md5_iso=iso_result.md5_iso if iso_result else None,
            md5_raw=iso_result.md5_raw if iso_result else None,
            sha256_iso=iso_result.sha256_iso if iso_result else None,
            sha256_raw=iso_result.sha256_raw if iso_result else None,
            creation_time=iso_result.creation_time if iso_result else 0.0,
            verification_time=iso_result.verification_time if iso_result else 0.0,
            error_message=backup_error,
            unmount_ok=unmount_ok,
            finalized=finalized
        )

        # A checksum mismatch is a failed preservation run (distinct exit code 2 in main)
        if result.checksum_match is False:
            result.success = False
            result.error_message = "Checksum mismatch: written ISO does not match source stream"

        # Non-fatal problems on otherwise-valid runs: the master is good, but
        # the record should make these findable (success_with_warnings).
        if result.success and not self.config.dry_run and not result.finalized:
            result.warnings.append("disc finalization (eject) failed")
        if result.success and not tree_ok:
            result.warnings.append("tree listing failed or incomplete")
        if result.success and not self.config.dry_run and iso_analysis.get('error'):
            result.warnings.append(f"isolyzer structural analysis not performed ({iso_analysis['error']})")
        # Degraded-independence notes from the copy/verify pass (fsync or page-cache
        # fallbacks) are recorded regardless of overall success.
        if iso_result and iso_result.warnings:
            result.warnings.extend(iso_result.warnings)

        if result.success and not self.config.dry_run:
            try:
                self._promote_successful_attempt(result, attempt_iso_path,
                                                 attempt_tree_path, attempt_isolyzer_path)
            except OSError as e:
                result.success = False
                result.error_message = f"Could not publish verified attempt artifacts: {e}"
                log(colorize("red", f"Backup failed: {result.error_message}"))

        # Failed or aborted runs keep all their records under failure-marked
        # names, so they are obvious in the output directory and a retry can
        # never overwrite the evidence of a previous attempt.
        if not result.success:
            # Timestamp for readability/sorting plus a random suffix so rapid
            # retries in the same second can never overwrite prior evidence
            fail_token = f"failed-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"
            # Set the token first and unconditionally: it points the log and
            # manifest writes below at failure-marked names. A rename that raises
            # (cross-filesystem move, a symlink pre-placed at the destination)
            # must NOT abort the run before those records are written, or a failed
            # run would be left undocumented — so each rename is best-effort.
            self.config.failure_token = fail_token
            self.config.attempt_token = None
            if attempt_iso_path.exists():
                marker = ".iso.mismatch" if result.checksum_match is False else ".iso.partial"
                marked_path = self.config.output_dir / f"{self.config.filename}_{fail_token}{marker}"
                try:
                    attempt_iso_path.rename(marked_path)
                    result.iso_path = marked_path
                    log(colorize("yellow", f"ISO renamed to: {marked_path.name}"))
                except OSError as e:
                    log(colorize("yellow", f"Could not rename partial ISO to {marked_path.name}: {e} "
                                          f"(leaving it in place; run still recorded as failed)"))
            # Move already-written sidecars (tree, isolyzer) to failure-marked names.
            for src, dst in ((attempt_tree_path, self.config.tree_path),
                             (attempt_isolyzer_path, self.config.isolyzer_path)):
                if src.exists():
                    try:
                        src.rename(dst)
                        log(colorize("yellow", f"{src.name} renamed to: {dst.name}"))
                    except OSError as e:
                        log(colorize("yellow", f"Could not rename {src.name} to {dst.name}: {e}"))
        
        # Generate outputs
        self._generate_summary(result)
        
        # Save formatted log with all the details
        create_formatted_log(self.config.log_path, self.config, result, start_time, disk_info, iso_analysis, self.run)

        self._create_manifest(result, disk_info, iso_analysis)

        # Fix ownership last — everything above ran as root, so the directory
        # and the files this run created would otherwise stay root-owned.
        # Deliberately non-recursive and limited to this run's known artifacts.
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            artifacts = [result.iso_path, self.config.log_path,
                         self.config.manifest_path, self.config.tree_path, self.config.isolyzer_path]
            if created_output_dir:
                artifacts.insert(0, self.config.output_dir)
            existing = [str(p) for p in artifacts if p.exists()]
            try:
                subprocess.run(["chown", sudo_user] + existing, check=True)
            except subprocess.CalledProcessError as e:
                log(colorize("yellow", f"Warning: Could not set ownership of output files: {e}"))

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
        archived under an overwritten-* token before the new artifacts publish.
        """
        names = ", ".join(p.name for p in existing_paths)
        response = get_input(colorize("yellow",
            f"Existing artifact(s) may be replaced or archived by this run ({names}). Continue? (y/n): "))
        return response.lower() == 'y'
    
    def _generate_tree_listing(self) -> bool:
        """Generate tree listing of volume contents; returns True on success"""
        log_divider("Generating Tree Listing")
        target = self.config.mount_point or f"/Volumes/{self.config.volume_name}"
        tree_cmd = ["tree", "-RapugD", "--si", "--du", target]
        try:
            log(colorize("cyan", "Generating tree listing..."))
            log(colorize("cyan", "Running command:") + " " + colorize("yellow", " ".join(tree_cmd)))
            with open(self.config.tree_path, "w", encoding="utf-8") as f:
                proc = subprocess.run(tree_cmd, stdout=f, stderr=subprocess.STDOUT,
                             env={**os.environ, "LANG": "en_US.UTF-8"})
            if proc.returncode != 0:
                log(colorize("yellow", f"Warning: tree exited with status {proc.returncode}; listing may be missing or incomplete."))
                return False
            log(colorize("cyan", "Tree saved to:") + " " + colorize("yellow", str(self.config.tree_path)))
            return True
        except Exception as e:
            log(colorize("red", f"Tree generation failed: {e}"))
            return False
    
    def _create_and_verify_iso(self, disk_size: int) -> IsoCreationResult:
        """Create ISO with in-stream source hashing, then verify by re-reading the written file"""
        log_divider("Creating ISO")
        log(colorize("cyan", "Creating ISO from:") + " " + colorize("yellow", self.config.disk_id))

        raw_disk = f"r{self.config.disk_id}"
        dd_command = f"dd if=/dev/{raw_disk} of={self.config.output_path} bs=4m"
        log(colorize("cyan", "Running command:") + " " + colorize("yellow", dd_command))

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
            with subprocess.Popen(["dd", f"if=/dev/{raw_disk}", "bs=4m"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
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

            # Force data to physical media before the verification pass below;
            # plain fsync on macOS stops at the drive cache, F_FULLFSYNC does not.
            iso_file.flush()
            try:
                fcntl.fcntl(iso_file.fileno(), fcntl.F_FULLFSYNC)
            except (OSError, AttributeError):
                os.fsync(iso_file.fileno())
                warnings.append("F_FULLFSYNC unavailable; fell back to fsync, which on "
                                "macOS may not flush the drive's own write cache before "
                                "verification")

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

        # Independent verification: re-read the written ISO from disk and hash it,
        # so the comparison reflects the bytes that actually landed on disk.
        log_divider("Verifying ISO")
        log(colorize("cyan", "Verifying using:") + " " + colorize("yellow", f"md5 {self.config.output_path.name} (re-read from disk) vs streamed raw disk hash"))

        # Optional operator check: this performs a fresh raw-device read, unlike
        # the script's automated verification, which uses the streamed source hash.
        manual_check = (f'[ "$(md5 -q {self.config.output_path.name})" = "$(dd if=/dev/{raw_disk} bs=4m 2>/dev/null | md5 -q)" ] '
                        f'&& echo "MATCH" || echo "MISMATCH"')
        log(colorize("cyan", "Optional manual spot-check (fresh raw-device read; not run automatically):")
            + " " + colorize("yellow", manual_check))

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
            # Bypass the page cache so we hash what is on disk, not the
            # copy of the data still sitting in memory from the write.
            try:
                fcntl.fcntl(iso_file.fileno(), fcntl.F_NOCACHE, 1)
            except (OSError, AttributeError):
                warnings.append("F_NOCACHE unavailable; the verification read may have "
                                "been served from the page cache rather than re-read from "
                                "disk, weakening the independence of the verification pass")
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
            log(colorize("green", "Checksum match: ISO on disk is a true bit-for-bit copy."))
        else:
            log(colorize("red", "Checksum mismatch! ISO on disk does not match the source stream."))

        return IsoCreationResult(
            creation_time=creation_time,
            verification_time=verification_time,
            md5_raw=raw_hash,
            md5_iso=iso_hash,
            sha256_raw=raw_sha,
            sha256_iso=iso_sha,
            output_path=self.config.output_path,
            warnings=warnings,
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
        log(colorize("cyan", "Running command:") + " " + colorize("yellow", f"diskutil unmountDisk /dev/{self.config.disk_id}"))
        
        try:
            subprocess.run(["diskutil", "unmountDisk", f"/dev/{self.config.disk_id}"], 
                         capture_output=True, text=True, check=True)
            log(colorize("green", "Unmounted successfully."))
            return True
        except subprocess.CalledProcessError as e:
            log(colorize("red", f"Failed to unmount:\n{e.stderr}"))
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
            log(colorize("cyan", "Running command:") + " " + colorize("yellow", f"diskutil eject /dev/{self.config.disk_id}"))
            eject_proc = subprocess.run(["diskutil", "eject", f"/dev/{self.config.disk_id}"],
                                        capture_output=True, text=True)
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
    
    def _create_manifest(self, result: BackupResult, disk_info: Dict[str, Any], iso_analysis: Dict[str, Any]):
        """Create comprehensive manifest file with improved organization"""
        # Get current timestamp
        now = datetime.datetime.now()
        
        # Extract tree listing summary
        tree_summary = self._extract_tree_summary()
        
        # Calculate additional metrics
        # Get ISO file size
        iso_file_size = result.iso_path.stat().st_size if result.iso_path.exists() else None
        
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
                "iso_created": iso_file_size is not None,
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
                "iso_filename": result.iso_path.name,
                "iso_file_size_bytes": iso_file_size,
                "iso_file_size_formatted": format_bytes(iso_file_size) if iso_file_size else None,
                "output_directory": str(self.config.output_dir),
                "log_filename": self.config.log_path.name,
                "tree_filename": self.config.tree_path.name,
                "isolyzer_filename": self.config.isolyzer_path.name,
                "manifest_filename": self.config.manifest_path.name
            },
            
            "operations_performed": {
                "tree_listing": {
                    "command": f"tree -RapugD --si --du {self.config.mount_point or '/Volumes/' + self.config.volume_name}",
                    "output_file": self.config.tree_path.name,
                    "summary": tree_summary
                },
                "disk_unmount": {
                    "command": f"diskutil unmountDisk /dev/{self.config.disk_id}",
                    "status": "skipped" if result.unmount_ok is None else ("success" if result.unmount_ok else "failed")
                },
                "iso_creation": {
                    "command": f"dd if=/dev/r{self.config.disk_id} of={result.creation_path or self.config.output_path} bs=4m",
                    "method": "streamed copy with in-stream source hash calculation",
                    "publish_method": (("run-private attempt ISO promoted to canonical filename after verification"
                                        if result.success else
                                        "run-private attempt ISO was not promoted because the run failed")
                                       if result.creation_path and result.creation_path != self.config.canonical_output_path
                                       else "written directly to canonical filename"),
                    "block_size": "4MB",
                    "performed": result.unmount_ok is True
                },
                "verification": {
                    "method": "md5 + sha-256 hash comparison (written ISO re-read from disk vs raw device stream)",
                    "algorithms": ["MD5", "SHA-256"],
                    "performed": result.md5_iso is not None,
                    "note": "The script does not perform a second raw-device read during verification; it compares the source hash captured during the copy stream to a re-read of the written ISO.",
                    "optional_manual_spot_check": (f'[ "$(md5 -q {result.iso_path.name})" = "$(dd if=/dev/r{self.config.disk_id} bs=4m 2>/dev/null | md5 -q)" ] '
                                                   f'&& echo "MATCH" || echo "MISMATCH"')
                },
                "finalization": {
                    "eject_command": f"diskutil eject /dev/{self.config.disk_id}",
                    "status": ("skipped" if self.config.dry_run or result.unmount_ok is False
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
                    "note": "Written ISO re-read from disk and hashed after creation"
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
                "verification_method": "Source hashed in-stream during creation; written ISO re-read from disk (cache bypassed) and hashed",
                "integrity_status": "verified" if result.checksum_match else ("failed" if result.checksum_match is False else "not_performed"),
                "notes": "ISO verified as bit-perfect copy" if result.checksum_match else None
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
                    "write_destination": str(result.creation_path or self.config.output_path),
                    "final_destination": str(result.iso_path),
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
        with open(self.config.manifest_path, "w") as f:
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
            log(colorize("cyan", "Running command:") + " " + colorize("yellow", " ".join(cmd)))
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
        
    def _extract_tree_summary(self) -> Dict[str, Any]:
        """Extract summary from tree listing"""
        try:
            with open(self.config.tree_path, "r", encoding="utf-8") as f:
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
  {colorize('green', '--dry-run')}               Run without writing ISO or verifying checksum
  {colorize('green', '--force')}                 Skip the optical-media safety check
  {colorize('green', '--filename NAME')}         ISO filename (without extension)
  {colorize('green', '--dir PATH')}              Output directory path (can use ~)
  {colorize('green', '--operator NAME')}         Operator name or initials

{colorize('yellow', 'EXAMPLES:')}
  {colorize('cyan', '# Interactive mode - prompts for all inputs')}
  sudo python3 makeiso.py

  {colorize('cyan', '# Specify filename, directory, and operator')}
  sudo python3 makeiso.py --filename MyDisc --dir ~/Backups --operator JD

  {colorize('cyan', '# Test run without creating ISO')}
  sudo python3 makeiso.py --dry-run

{colorize('yellow', 'KEY FEATURES:')}
  • {colorize('green', 'Parallel processing:')} Creates ISO while calculating the source checksum
  • {colorize('green', 'Progress tracking:')} Real-time speed and remaining time
  • {colorize('green', 'Comprehensive logging:')} Detailed logs saved to .log.txt
  • {colorize('green', 'Metadata manifest:')} JSON file with complete backup metadata
  • {colorize('green', 'Tree listing:')} Directory structure saved to .txt file
  • {colorize('green', 'Cross-verification:')} Compares ISO and raw disk MD5 and SHA-256 hashes

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
    
    # Exit with appropriate code
    if result.success:
        if config.dry_run:
            log(colorize("green", "Dry run completed — no ISO was created."))
        else:
            log(colorize("green", "Backup completed successfully!"))
        sys.exit(0)
    elif result.checksum_match is False:
        log(colorize("red", "Backup failed verification: checksum mismatch!"))
        sys.exit(2)  # Distinct exit code for verification failure
    else:
        log(colorize("red", f"Backup failed: {result.error_message}"))
        sys.exit(1)

### === SCRIPT ENTRYPOINT ===

if __name__ == "__main__":
    main()
