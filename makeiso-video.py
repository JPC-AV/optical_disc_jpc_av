#!/usr/bin/env python3

"""
makeiso-video.py - ISO to MP4 Access Copy Utility
Extracts VIDEO_TS content from ISO files and converts to MP4 access copies.
Part of the Johnson Publishing Company Archive (JPCA) optical disc digitization workflow.

A collaboration between the Getty Research Institute and the 
Smithsonian National Museum of African American History and Culture.
"""

import os
import subprocess
import sys
import datetime
import time
import hashlib
import re
import platform
import argparse
import json
import logging
import math
import plistlib
import stat
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass, field
from uuid import uuid4

### === DATA STRUCTURES ===

@dataclass
class ConversionConfig:
    """Configuration for conversion operation"""
    iso_path: Path
    output_dir: Path
    operator: str
    dry_run: bool = False
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    crf: int = 18  # Quality: 0=lossless, 18=visually lossless, 23=default, 28=smaller
    preset: str = "medium"  # Speed/compression: ultrafast, fast, medium, slow, veryslow
    audio_bitrate: str = "192k"
    
    @property
    def iso_name(self) -> str:
        return self.iso_path.stem
    
    @property
    def mp4_path(self) -> Path:
        return self.output_dir / f"{self.iso_name}.mp4"
    
    @property
    def log_path(self) -> Path:
        return self.output_dir / f"{self.iso_name}.mp4.log.txt"
    
    @property
    def dry_run_log_path(self) -> Path:
        return self.output_dir / f"{self.iso_name}.mp4.dryrun.log.txt"
    
    @property
    def manifest_path(self) -> Path:
        return self.output_dir / f"{self.iso_name}_access_manifest.json"

    @property
    def skip_manifest_path(self) -> Path:
        return self.output_dir / f"{self.iso_name}_skip_manifest.json"

@dataclass
class DiscContentAnalysis:
    """Analysis of ISO content structure"""
    has_video_ts: bool = False
    has_audio_ts: bool = False  # For DVD-Audio discs
    vob_files: List[Path] = field(default_factory=list)
    ifo_files: List[Path] = field(default_factory=list)
    bup_files: List[Path] = field(default_factory=list)
    total_vob_size: int = 0
    disc_type: str = "unknown"  # "dvd_video", "dvd_audio", "data", "mixed", "unknown"
    mount_point: Optional[Path] = None
    error: Optional[str] = None

@dataclass
class TitlesetPlan:
    """One titleset's planned encode.

    Titleset-aware, not title-aware: grouping is by VTS number only. A single
    titleset may still contain multiple programs/PGCs/angles — this is not
    DVD-player-equivalent title parsing.
    """
    vts_number: int          # 0 = pseudo-titleset of unrecognized VOB names
    content_vobs: List[Path] = field(default_factory=list)
    output_path: Optional[Path] = None
    total_size: int = 0
    is_main: bool = False    # lowest-numbered content titleset; keeps unsuffixed name

@dataclass
class IfoDuration:
    """What a titleset's VTS IFO says about its playback time"""
    status: str                      # supported | used | advisory | invalid | missing
    seconds: Optional[float] = None  # program chain playback time, when it parsed
    reason: Optional[str] = None     # why it is advisory, invalid or missing

@dataclass
class DurationExpectation:
    """Expected duration of a titleset's encode, and where it came from"""
    seconds: Optional[float] = None  # usable expectation only; None = nothing to compare
    source: Optional[str] = None     # ifo | vob_probe
    reason: Optional[str] = None     # why there is no usable expectation
    ifo: Optional[IfoDuration] = None
    vob_probe_seconds: Optional[float] = None  # sum of the VOB durations that measured
    vob_probe_complete: Optional[bool] = None  # None = VOBs not probed (IFO used)

@dataclass
class TitleOutput:
    """Outcome of one titleset's encode.

    status: planned (dry run) | not_attempted | encoded (checked, awaiting
    the promotion decision) | completed (passed and promoted this run) |
    held (passed, but not promoted because the ISO's run did not pass) |
    unverified (check could not be completed) | failed"""
    vts_number: int
    output_path: Path                # final (canonical) name this titleset is meant for
    status: str = "not_attempted"
    output_size: int = 0
    video_duration: Optional[float] = None
    expected_duration: Optional[float] = None       # usable expectation only
    expected_duration_source: Optional[str] = None  # ifo | vob_probe
    expectation: Optional[DurationExpectation] = None
    duration_check: Optional[str] = None             # passed | unverified | failed
    duration_check_reason: Optional[str] = None
    duration_tolerance: Optional[float] = None
    encode_time: float = 0.0
    md5: Optional[str] = None
    sha256: Optional[str] = None
    source_audio_streams: Optional[int] = None
    source_subtitle_streams: Optional[int] = None
    # None = not measured (output not probed, or the probe failed);
    # 0 only when a successful probe found none
    mapped_video_streams: Optional[int] = None
    mapped_audio_streams: Optional[int] = None
    error: Optional[str] = None
    temp_path: Optional[Path] = None   # checked output awaiting promotion or retention
    promoted_this_run: bool = False
    replaced_existing_canonical: bool = False
    hold_reason: Optional[str] = None
    # Where this run's non-canonical output actually is: kept as a .partial
    # ("partial"), a review copy ("review_copy"), or left at its temp name
    # ("encoding_temp") when it could not be moved
    retained_path: Optional[Path] = None
    retained_as: Optional[str] = None
    retention_error: Optional[str] = None

@dataclass
class ConversionResult:
    """Result of conversion operation"""
    success: bool
    mp4_path: Optional[Path] = None
    iso_path: Optional[Path] = None
    source_size: int = 0
    output_size: int = 0
    duration_seconds: float = 0.0
    video_duration: Optional[float] = None  # Duration of the video content
    conversion_time: float = 0.0
    md5_mp4: Optional[str] = None
    sha256_mp4: Optional[str] = None
    error_message: Optional[str] = None
    disc_analysis: Optional[DiscContentAnalysis] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    titles: List[TitleOutput] = field(default_factory=list)  # per-titleset outcomes
    warnings: List[str] = field(default_factory=list)        # non-fatal problems (run still valid)
    aborted: bool = False        # operator declined to proceed (recorded as aborted, not error)
    interrupted: bool = False    # Ctrl-C mid-run (batch processing stops)
    records_unsafe: bool = False # record paths themselves are suspect (symlink guard); write nothing
    # Nothing failed, but at least one titleset could not be verified, so
    # nothing was promoted (success is False; failure outranks this)
    unverified: bool = False
    unverified_reason: Optional[str] = None
    promotion_error: Optional[str] = None  # promotion stopped partway (run failed)

    @property
    def canonical_files_changed(self) -> List[Path]:
        """Final-name MP4s this run actually created or replaced"""
        return [t.output_path for t in self.titles if t.promoted_this_run]

    @property
    def compression_ratio(self) -> Optional[float]:
        """Calculate compression ratio (original/compressed)"""
        if self.source_size > 0 and self.output_size > 0:
            return round(self.source_size / self.output_size, 2)
        return None

    @property
    def speed_factor(self) -> Optional[float]:
        """Calculate encoding speed relative to realtime"""
        if self.video_duration and self.conversion_time > 0:
            return round(self.video_duration / self.conversion_time, 2)
        return None

@dataclass
class RunContext:
    """Identity and step timing for a single conversion run.

    Generated once per run and shared by the log and manifest writers so
    all records carry the same run_id/uuid and real step timestamps.
    """
    run_id: str
    run_uuid: str
    start_time: datetime.datetime
    step_times: Dict[str, datetime.datetime] = field(default_factory=dict)

    def mark(self, step: str):
        """Record the moment a step actually happened"""
        self.step_times[step] = datetime.datetime.now()

### === COLOR UTILITIES ===

COLOR = {
    "blue": "\033[94m",
    "cyan": "\033[96m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "magenta": "\033[95m",
    "off": "\033[0m"
}

# ANSI output (color + progress carriage returns) only when stdout is a
# terminal and NO_COLOR is unset (https://no-color.org). Decided at import.
ANSI_ENABLED = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

def colorize(color: str, text: str) -> str:
    """Apply ANSI color to text (no-op when output is not a terminal)"""
    if not ANSI_ENABLED:
        return text
    return f"{COLOR[color]}{text}{COLOR['off']}"

### === LOGGING SETUP ===

def setup_logging() -> logging.Logger:
    """Setup console logging"""
    logger = logging.getLogger("iso_to_mp4")
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

def run_cmd(cmd: List[str], capture_output: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    """Run command and return result"""
    return subprocess.run(cmd, capture_output=capture_output, text=True, check=check)

def format_bytes(bytes_val: int) -> str:
    """Format bytes in human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} TB"

def format_duration(seconds: float) -> str:
    """Format duration in human readable format"""
    if seconds == 0:
        return "0 seconds"
    hours = int(seconds) // 3600
    minutes = (int(seconds) % 3600) // 60
    remaining_seconds = round(seconds % 60, 2)
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{remaining_seconds:.1f}s")
    
    return " ".join(parts)

def format_count(count: Optional[int]) -> str:
    """A stream count for people: "unknown" when it was not measured"""
    return "unknown" if count is None else str(count)

def format_timecode(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm timecode"""
    hours = int(seconds) // 3600
    minutes = (int(seconds) % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"

def calculate_checksums(filepath: Path) -> Tuple[Optional[str], Optional[str]]:
    """Calculate MD5 and SHA-256 of a file in one read.

    MD5 is kept for continuity with existing collection records; SHA-256 for
    current archival fixity practice. Returns (None, None) on error — callers
    surface that as a run warning."""
    try:
        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):  # 4MB chunks
                md5.update(chunk)
                sha256.update(chunk)
        return md5.hexdigest(), sha256.hexdigest()
    except Exception as e:
        log(colorize("yellow", f"Warning: checksum calculation failed: {e}"))
        return None, None

### === ISO MOUNTING ===

class ISOMount:
    """Context manager for mounting ISO files.

    Multi-session/multi-partition ISOs can mount several volumes; the
    returned mount point is the one containing VIDEO_TS when present,
    falling back to the first volume."""

    def __init__(self, iso_path: Path):
        self.iso_path = iso_path
        self.mount_point: Optional[Path] = None
        self.mount_points: List[Path] = []
        self.devices: List[str] = []
        # Detach failures recorded here so the caller can surface them into the
        # conversion record (the context manager has no access to it directly).
        self.detach_errors: List[str] = []

    def __enter__(self) -> Optional[Path]:
        """Mount the ISO and return the most relevant mount point"""
        try:
            # Use hdiutil to mount the ISO
            result = run_cmd([
                "hdiutil", "attach", str(self.iso_path),
                "-readonly", "-nobrowse", "-plist", "-owners", "off"
            ])

            # Parse plist output: collect ALL mounted volumes and devices
            plist_data = plistlib.loads(result.stdout.encode())
            for entity in plist_data.get("system-entities", []):
                mount_point = entity.get("mount-point")
                if mount_point:
                    self.mount_points.append(Path(mount_point))
                if entity.get("dev-entry"):
                    self.devices.append(entity["dev-entry"])

            if not self.mount_points:
                log(colorize("red", "Failed to find mount point in hdiutil output"))
                return None

            # Prefer the volume that actually contains VIDEO_TS
            self.mount_point = self.mount_points[0]
            if len(self.mount_points) > 1:
                log(colorize("yellow", f"ISO mounted {len(self.mount_points)} volumes: "
                                       + ", ".join(str(p) for p in self.mount_points)))
                for candidate in self.mount_points:
                    try:
                        has_video_ts = any(item.is_dir() and item.name.upper() == "VIDEO_TS"
                                           for item in candidate.iterdir())
                    except OSError:
                        # Unlistable root (e.g. mode --x on recorder-authored
                        # UDF): probe the standard path by name instead
                        has_video_ts = (candidate / "VIDEO_TS").is_dir()
                    if has_video_ts:
                        self.mount_point = candidate
                        log(colorize("cyan", f"Using volume containing VIDEO_TS: {candidate}"))
                        break

            log(colorize("green", f"Mounted ISO at: {self.mount_point}"))
            return self.mount_point

        except subprocess.CalledProcessError as e:
            log(colorize("red", f"Failed to mount ISO: {e}"))
            return None
        except Exception as e:
            log(colorize("red", f"Unexpected error mounting ISO: {e}"))
            return None

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Unmount the ISO (all volumes for multi-session images).

        Detach failures are recorded in self.detach_errors so the caller can
        surface them: a leaked attach holds a /dev/diskN that can block later
        mounts across a batch."""
        detached_any = False
        for mount_point in self.mount_points:
            if not mount_point.exists():
                detached_any = True  # an earlier detach already released this volume
                continue
            try:
                run_cmd(["hdiutil", "detach", str(mount_point), "-force"])
                log(colorize("green", f"Unmounted: {mount_point}"))
                detached_any = True
            except subprocess.CalledProcessError as e:
                log(colorize("yellow", f"Warning: Failed to unmount cleanly: {e}"))
                # Fall back to detaching the backing device(s) directly
                for device in self.devices:
                    try:
                        run_cmd(["hdiutil", "detach", device, "-force"])
                        detached_any = True
                    except Exception as fallback_error:
                        msg = f"failed to detach {device}: {fallback_error}"
                        log(colorize("yellow", f"Warning: fallback {msg}"))
                        self.detach_errors.append(f"unmount of {self.iso_path.name}: {msg}")

        # Leak sweep: if the image attached devices but exposed NO mount points
        # (e.g. a raw session macOS would not auto-mount), the loop above never
        # released them. Detach by device so they do not accumulate across a batch.
        if not detached_any and self.devices:
            for device in self.devices:
                try:
                    run_cmd(["hdiutil", "detach", device, "-force"])
                    log(colorize("green", f"Detached leaked device: {device}"))
                except Exception as e:
                    msg = f"failed to detach leaked device {device}: {e}"
                    log(colorize("yellow", f"Warning: {msg}"))
                    self.detach_errors.append(f"unmount of {self.iso_path.name}: {msg}")

### === CONTENT ANALYSIS ===

def analyze_iso_content(mount_point: Path) -> DiscContentAnalysis:
    """Analyze the content structure of a mounted ISO"""
    analysis = DiscContentAnalysis()
    analysis.mount_point = mount_point
    
    try:
        # Check for VIDEO_TS folder (case-insensitive search)
        video_ts_path = None
        audio_ts_path = None

        root_listable = True
        try:
            entries = list(mount_point.iterdir())
        except PermissionError:
            # Some recorder-authored UDF discs give the volume root search
            # permission but no read permission (mode --x), so it cannot be
            # listed — but its well-known children can still be reached by
            # name, which only requires search permission.
            root_listable = False
            log(colorize("yellow", "Volume root is not listable (no read permission); "
                                   "probing standard VIDEO_TS/AUDIO_TS paths directly"))
            entries = [p for p in (mount_point / "VIDEO_TS", mount_point / "AUDIO_TS")
                       if p.is_dir()]
            if not entries:
                analysis.error = ("Volume root is unreadable and no VIDEO_TS or AUDIO_TS "
                                  "directory was found at the standard paths")
                analysis.disc_type = "error"
                return analysis

        for item in entries:
            if item.is_dir():
                if item.name.upper() == "VIDEO_TS":
                    video_ts_path = item
                    analysis.has_video_ts = True
                elif item.name.upper() == "AUDIO_TS":
                    audio_ts_path = item
                    analysis.has_audio_ts = True
        
        # Analyze VIDEO_TS content
        if video_ts_path:
            for file in video_ts_path.iterdir():
                if file.is_file():
                    suffix = file.suffix.upper()
                    if suffix == ".VOB":
                        analysis.vob_files.append(file)
                        analysis.total_vob_size += file.stat().st_size
                    elif suffix == ".IFO":
                        analysis.ifo_files.append(file)
                    elif suffix == ".BUP":
                        analysis.bup_files.append(file)
        
        # Determine disc type
        if analysis.has_video_ts and analysis.vob_files:
            if analysis.has_audio_ts:
                analysis.disc_type = "mixed"
            else:
                analysis.disc_type = "dvd_video"
        elif analysis.has_audio_ts:
            analysis.disc_type = "dvd_audio"
        elif not root_listable:
            # Root can't be enumerated, so we can't tell data from empty
            analysis.disc_type = "data"
        else:
            # Check if there's any recognizable content
            file_count = sum(1 for _ in mount_point.rglob("*") if _.is_file())
            if file_count > 0:
                analysis.disc_type = "data"
            else:
                analysis.disc_type = "empty"
        
        # Sort VOB files for proper concatenation order
        analysis.vob_files.sort(key=lambda p: p.name.upper())
        
    except Exception as e:
        analysis.error = str(e)
        analysis.disc_type = "error"
    
    return analysis

### === TITLESET PLANNING ===

MENU_VOB_RE = re.compile(r'VTS_(\d+)_0\.VOB$', re.IGNORECASE)
CONTENT_VOB_RE = re.compile(r'VTS_(\d+)_([1-9])\.VOB$', re.IGNORECASE)

def plan_titlesets(vob_files: List[Path], output_dir: Path, iso_name: str
                   ) -> Tuple[List[TitlesetPlan], List[Path], List[Path]]:
    """Group content VOBs by titleset (VTS_NN) and assign output paths.

    Returns (plans, menu_vobs, unrecognized_vobs). The lowest-numbered
    content titleset is "main" and keeps the unsuffixed output name; others
    get _titleNN suffixes matching their VTS number. Titleset-aware only —
    a titleset may still contain multiple programs/PGCs.
    """
    groups: Dict[int, List[Path]] = {}
    menu_vobs: List[Path] = []
    unrecognized: List[Path] = []

    for vob in vob_files:
        name = vob.name.upper()
        if name == "VIDEO_TS.VOB" or MENU_VOB_RE.match(name):
            menu_vobs.append(vob)
            continue
        content_match = CONTENT_VOB_RE.match(name)
        if content_match:
            groups.setdefault(int(content_match.group(1)), []).append(vob)
        else:
            unrecognized.append(vob)

    # Spec-shaped titlesets win; otherwise fall back to one pseudo-titleset
    # of unrecognized-but-not-menu VOBs (preserves old behavior for odd
    # discs with real content under nonstandard names)
    if not groups and unrecognized:
        groups = {0: list(unrecognized)}
        unrecognized = []

    plans: List[TitlesetPlan] = []
    for index, vts in enumerate(sorted(groups)):
        vobs = sorted(groups[vts], key=lambda p: p.name.upper())
        is_main = index == 0
        if is_main:
            output = output_dir / f"{iso_name}.mp4"
        else:
            output = output_dir / f"{iso_name}_title{vts:02d}.mp4"
        plans.append(TitlesetPlan(
            vts_number=vts,
            content_vobs=vobs,
            output_path=output,
            total_size=sum(v.stat().st_size for v in vobs if v.exists()),
            is_main=is_main,
        ))
    return plans, menu_vobs, unrecognized

### === FFMPEG OPERATIONS ===

def check_ffmpeg() -> Tuple[bool, Optional[str]]:
    """Check if ffmpeg is available and get version"""
    try:
        result = run_cmd(["ffmpeg", "-version"])
        # Extract version from first line
        first_line = result.stdout.split('\n')[0]
        version_match = re.search(r'ffmpeg version (\S+)', first_line)
        version = version_match.group(1) if version_match else "unknown"
        return True, version
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False, None

def check_ffprobe() -> Tuple[bool, Optional[str]]:
    """Check if ffprobe is available and get version"""
    try:
        result = run_cmd(["ffprobe", "-version"])
        first_line = result.stdout.split('\n')[0]
        version_match = re.search(r'ffprobe version (\S+)', first_line)
        version = version_match.group(1) if version_match else "unknown"
        return True, version
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False, None

def get_video_duration(target) -> Optional[float]:
    """Get duration in seconds via ffprobe; target may be a path or a concat: URL.

    None when it cannot be measured, including a non-finite reading."""
    try:
        result = run_cmd([
            "ffprobe",
            "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(target)
        ])
        seconds = float(result.stdout.strip())
        return seconds if math.isfinite(seconds) else None
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None

def _usable_seconds(value) -> bool:
    """A duration that can take part in a comparison: finite and above zero"""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)

SECTOR_SIZE = 2048

def _bcd_time_frames(raw: bytes) -> Optional[Tuple[int, int]]:
    """Decode a DVD playback time (BCD hh:mm:ss:ff, frame rate in the top
    two bits of ff: 01 = 25 fps, 11 = 30 fps) to (frames, fps).
    None unless every field is valid BCD within range."""
    if len(raw) != 4:
        return None
    fps = {0b01: 25, 0b11: 30}.get(raw[3] >> 6)
    if fps is None:
        return None

    def bcd(byte: int) -> Optional[int]:
        high, low = byte >> 4, byte & 0x0F
        return high * 10 + low if high <= 9 and low <= 9 else None

    hours, minutes, seconds, frames = bcd(raw[0]), bcd(raw[1]), bcd(raw[2]), bcd(raw[3] & 0x3F)
    if None in (hours, minutes, seconds, frames) or minutes >= 60 or seconds >= 60 or frames >= fps:
        return None
    return (hours * 3600 + minutes * 60 + seconds) * fps + frames, fps

def parse_ifo_playback(ifo_path: Optional[Path], content_vobs: List[Path]) -> IfoDuration:
    """Playback time from a titleset's VTS_nn_0.IFO, and whether it applies.

    The IFO's program chain (PGC) records the authored playback time, which
    stays meaningful on recorder-authored discs whose VOB timestamps reset.
    It describes what this script encodes (the content VOBs in file order)
    only for a straight-through titleset: exactly one PGC, played in order,
    whose cells each play once, in sector order, covering the content VOBs
    exactly — no angle or interleaved blocks, still frames or cell commands —
    and whose time matches the sum of its cells' (within a frame per cell).
    Every offset is bounds-checked and every time BCD-validated, and the
    PGC's required tables (libdvdread's ifoRead_PGC structures) must be
    present and consistent with its declared program and cell counts.

    status "supported" for that layout; "advisory" when the time parsed but
    the layout is anything else (recorded, never decides an outcome);
    "invalid" or "missing" when there is no trustworthy time at all."""
    if ifo_path is None:
        return IfoDuration("missing", reason="no VTS IFO for this titleset")
    try:
        data = ifo_path.read_bytes()
    except OSError as e:
        return IfoDuration("missing", reason=f"IFO could not be read: {e}")

    def u16(offset: int) -> int:
        return int.from_bytes(data[offset:offset + 2], "big")

    def u32(offset: int) -> int:
        return int.from_bytes(data[offset:offset + 4], "big")

    def invalid(reason: str) -> IfoDuration:
        return IfoDuration("invalid", reason=f"IFO {reason}")

    if len(data) < 0x100 or not data.startswith(b"DVDVIDEO-VTS"):
        return invalid("is not a VTS information file")
    table = u32(0xCC) * SECTOR_SIZE  # VTS_PGCI: the titleset's program chains
    if table < SECTOR_SIZE or table + 8 > len(data):
        return invalid("program chain table pointer is out of bounds")
    srp_count = u16(table)
    table_end = table + u32(table + 4) + 1  # the end address is the table's last byte
    if srp_count == 0:
        return invalid("lists no program chains")
    if table_end > len(data) or table + 8 + 8 * srp_count > table_end:
        return invalid("program chain table runs past its end or the file")
    if srp_count != 1:
        return IfoDuration("advisory", reason=f"IFO lists {srp_count} program chains; "
                                              "only a single straight-through chain is supported")

    pgc = table + u32(table + 8 + 4)
    if pgc < table + 16 or pgc + 0xEC > table_end:
        return invalid("program chain is out of bounds")
    pgc_time = _bcd_time_frames(data[pgc + 4:pgc + 8])
    if pgc_time is None:
        return invalid("program chain playback time is not a valid BCD time")
    pgc_frames, fps = pgc_time
    if pgc_frames == 0:
        return invalid("program chain playback time is zero")
    seconds = pgc_frames / fps
    # Required PGC tables, as libdvdread reads them: the program map (one
    # byte per program: its entry cell), the cell playback table (24 bytes
    # per cell) and the cell position table (4 bytes per cell: VOB ID, a
    # zero byte, cell ID). Each must lie after the 0xEC-byte PGC header and
    # inside the program chain table, without overlapping another, and
    # there can be no more programs than cells. The optional command table,
    # when its pointer is set, must be just as readable.
    program_count, cell_count = data[pgc + 2], data[pgc + 3]
    if cell_count == 0 or program_count == 0:
        return invalid("program chain has no cells or no programs")
    if program_count > cell_count:
        return invalid(f"program chain lists {program_count} programs "
                       f"but only {cell_count} cells")
    spans: Dict[str, Tuple[int, int]] = {}
    for label, field, size in (("program map", 0xE6, program_count),
                               ("cell table", 0xE8, 24 * cell_count),
                               ("cell position table", 0xEA, 4 * cell_count)):
        offset = u16(pgc + field)
        if offset < 0xEC or pgc + offset + size > table_end:
            return invalid(f"{label} is missing or out of bounds")
        spans[label] = (pgc + offset, pgc + offset + size)
    commands_offset = u16(pgc + 0xE4)
    if commands_offset:
        # 8-byte header: pre, post and cell command counts, last byte;
        # then 8 bytes per command
        commands = pgc + commands_offset
        if commands_offset < 0xEC or commands + 8 > table_end:
            return invalid("command table is out of bounds")
        command_count = u16(commands) + u16(commands + 2) + u16(commands + 4)
        length = u16(commands + 6) + 1
        if command_count > 255 or length < 8 + 8 * command_count or commands + length > table_end:
            return invalid("command table is inconsistent or out of bounds")
        spans["command table"] = (commands, commands + length)
    ordered = sorted(spans.values())
    if any(end > next_start for (_, end), (next_start, _) in zip(ordered, ordered[1:])):
        return invalid("program chain tables overlap")
    entry_cells = data[spans["program map"][0]:spans["program map"][1]]
    if (entry_cells[0] != 1 or entry_cells[-1] > cell_count
            or any(earlier >= later for earlier, later in zip(entry_cells, entry_cells[1:]))):
        return invalid("program map is not increasing cell numbers starting at cell 1")
    positions_at = spans["cell position table"][0]
    positions = [(u16(positions_at + 4 * i), data[positions_at + 4 * i + 2],
                  data[positions_at + 4 * i + 3]) for i in range(cell_count)]
    for number, (vob_id, zero, cell_id) in enumerate(positions, 1):
        if vob_id == 0 or zero != 0 or cell_id == 0:
            return invalid(f"cell position {number} is not a valid VOB/cell ID")
    cells = spans["cell table"][0]

    advisory: List[str] = []
    if len({(vob_id, cell_id) for vob_id, _, cell_id in positions}) != cell_count:
        advisory.append("cell positions repeat a VOB/cell ID")
    if data[pgc + 0xA3] != 0:
        advisory.append("program chain plays in shuffle or random order")
    if data[pgc + 0xA2] != 0:
        advisory.append("program chain ends in a still frame")
    cell_frames = 0
    next_sector = 0
    tiled = True
    for index in range(cell_count):
        cell = cells + 24 * index
        number = index + 1
        cell_time = _bcd_time_frames(data[cell + 4:cell + 8])
        if cell_time is None:
            return invalid(f"cell {number} playback time is not a valid BCD time")
        if cell_time[1] != fps:
            return invalid(f"cell {number} uses a different frame rate from its program chain")
        cell_frames += cell_time[0]
        first, last_vobu, last = u32(cell + 8), u32(cell + 16), u32(cell + 20)
        if not first <= last_vobu <= last:
            return invalid(f"cell {number} sector range is inconsistent")
        category, flags = data[cell], data[cell + 1]
        if category & 0xF1:  # block mode, block type, seamless angle change
            advisory.append(f"cell {number} is part of an angle block")
        if category & 0x04:
            advisory.append(f"cell {number} is interleaved")
        if data[cell + 2] != 0 or flags & 0x40:
            advisory.append(f"cell {number} has a still frame")
        if data[cell + 3] != 0:
            advisory.append(f"cell {number} runs a cell command")
        if tiled and first != next_sector:
            advisory.append(f"cells do not cover the content VOBs straight through "
                            f"(cell {number} starts at sector {first}, expected {next_sector})")
            tiled = False
        next_sector = last + 1

    try:
        sizes = [v.stat().st_size for v in content_vobs]
    except OSError as e:
        return invalid(f"cannot be checked against the content VOBs: {e}")
    if any(size % SECTOR_SIZE for size in sizes):
        advisory.append("content VOB sizes are not whole sectors")
    elif tiled and next_sector != sum(sizes) // SECTOR_SIZE:
        advisory.append(f"cells cover {next_sector} sectors but the content VOBs "
                        f"hold {sum(sizes) // SECTOR_SIZE}")
    if abs(cell_frames - pgc_frames) > cell_count:
        advisory.append(f"program chain time ({pgc_frames} frames) differs from "
                        f"the sum of its cells ({cell_frames} frames)")
    if advisory:
        return IfoDuration("advisory", seconds=seconds, reason="IFO " + "; ".join(advisory))
    return IfoDuration("supported", seconds=seconds)

# DVD-Video's program stream tops out at ~10.08 Mbps; an expected duration
# implying more than this (with margin) is provably wrong, not evidence
# about the encode. Recorder-authored discs hit this constantly: their VOB
# timestamps reset at every recording, so probed durations come out absurd.
# This rule can only reject an expectation, never confirm one.
PLAUSIBLE_DVD_BPS = 12_000_000

def expected_titleset_duration(vobs: List[Path], ifo_path: Optional[Path]) -> DurationExpectation:
    """Best usable expected duration for a titleset, or none with the reason.

    The IFO's playback time is used only for a straight-through titleset
    (see parse_ifo_playback). Otherwise the fallback is the sum of per-VOB
    ffprobe durations — ffprobe on a concat: input can report only the
    first segment — usable only when every VOB measures (a partial sum can
    hide exactly the segment an encode dropped). Either is rejected when it
    implies an impossible DVD bitrate. An unusable IFO never blocks a usable
    VOB fallback."""
    total_bytes = sum(v.stat().st_size for v in vobs if v.exists())

    def implied_bps(seconds: float) -> float:
        return total_bytes * 8 / seconds

    ifo = parse_ifo_playback(ifo_path, vobs)
    if ifo.status == "supported":
        if implied_bps(ifo.seconds) <= PLAUSIBLE_DVD_BPS:
            ifo.status = "used"
            return DurationExpectation(seconds=ifo.seconds, source="ifo", ifo=ifo)
        ifo.status = "advisory"
        ifo.reason = (f"IFO time implies {implied_bps(ifo.seconds) / 1e6:.1f} Mbps over the "
                      f"content VOBs, more than DVD-Video allows")

    expectation = DurationExpectation(ifo=ifo)
    parts = [get_video_duration(v) for v in vobs]
    measured = [p for p in parts if _usable_seconds(p)]
    expectation.vob_probe_complete = bool(parts) and len(measured) == len(parts)
    expectation.vob_probe_seconds = sum(measured) if measured else None
    if not expectation.vob_probe_complete:
        vob_reason = (f"{len(parts) - len(measured)} of {len(parts)} VOB duration(s) "
                      f"could not be measured")
    elif not _usable_seconds(expectation.vob_probe_seconds):
        vob_reason = "VOB durations do not add up to a usable total"
    elif implied_bps(expectation.vob_probe_seconds) > PLAUSIBLE_DVD_BPS:
        vob_reason = (f"VOB durations imply {implied_bps(expectation.vob_probe_seconds) / 1e6:.1f} "
                      f"Mbps, more than DVD-Video allows (typical of recorder-authored "
                      f"discs whose timestamps reset)")
    else:
        expectation.seconds = expectation.vob_probe_seconds
        expectation.source = "vob_probe"
        return expectation
    expectation.reason = (f"no usable expected duration ({ifo.reason or 'IFO ' + ifo.status}; "
                          f"{vob_reason})")
    return expectation

# Duration check tolerance: max(5% of the expected duration, 1 second)
DURATION_TOLERANCE = 0.05
DURATION_TOLERANCE_FLOOR = 1.0

# Names tried when keeping a non-canonical output before giving up
RETENTION_ATTEMPTS = 5

def duration_check(expected: Optional[float], no_expectation_reason: Optional[str],
                   output_seconds: Optional[float], video_streams: Optional[int]
                   ) -> Tuple[str, str, Optional[float]]:
    """Check one encoded titleset: returns (result, reason, tolerance_seconds).

    failed:     a probe confirmed the output has no video, or it is shorter
                than a usable expectation by more than the tolerance
    unverified: the check could not be completed — the stream probe failed,
                the output's duration is not measurable, there is no usable
                expectation (never compared, so never a truncation by
                itself), or the output is longer by more than the tolerance
    passed:     otherwise; within (or exactly on) the tolerance
    Failure outranks unverified."""
    failed: List[str] = []
    unverified: List[str] = []
    if video_streams is None:
        unverified.append("output stream probe failed, so its video could not be confirmed")
    elif video_streams == 0:
        failed.append("output has no video stream")
    tolerance = None
    detail = ""
    if not _usable_seconds(output_seconds):
        unverified.append("output duration could not be measured")
    elif not _usable_seconds(expected):
        unverified.append(no_expectation_reason or "no usable expected duration")
    else:
        tolerance = max(expected * DURATION_TOLERANCE, DURATION_TOLERANCE_FLOOR)
        difference = output_seconds - expected
        detail = (f"output {format_timecode(output_seconds)}, expected "
                  f"{format_timecode(expected)}, tolerance {tolerance:.2f} s")
        if difference < -tolerance:
            failed.append(f"output is shorter than expected beyond the tolerance ({detail}) "
                          f"— truncated encode")
        elif difference > tolerance:
            unverified.append(f"output is longer than expected beyond the tolerance ({detail})")
    if failed:
        return "failed", "; ".join(failed), tolerance
    if unverified:
        return "unverified", "; ".join(unverified), tolerance
    return "passed", f"within tolerance ({detail})", tolerance

def probe_stream_counts(target) -> Optional[Dict[str, int]]:
    """Count video/audio/subtitle streams via ffprobe.

    None when the probe failed or its answer has no streams list, so
    presence is unknown — as distinct from a successful probe that listed
    no streams (all counts 0)."""
    info = get_video_info(target)
    streams = info.get("streams") if isinstance(info, dict) else None
    if not isinstance(streams, list):
        return None
    counts = {"video": 0, "audio": 0, "subtitle": 0}
    for stream in streams:
        codec_type = stream.get("codec_type") if isinstance(stream, dict) else None
        if codec_type in counts:
            counts[codec_type] += 1
    return counts

def get_video_info(target) -> Dict[str, Any]:
    """Get detailed video information using ffprobe; target may be a path or concat: URL"""
    try:
        result = run_cmd([
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(target)
        ])
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        return {}

### === MAIN CONVERTER CLASS ===

class ISOToMP4Converter:
    """Main class for ISO to MP4 conversion operations"""
    
    def __init__(self, config: ConversionConfig):
        self.config = config
        self.start_time: Optional[datetime.datetime] = None
        self.run: Optional[RunContext] = None
        self._token_suffix: Optional[str] = None

    def _run_token(self, kind: str = "failed") -> str:
        """One timestamp-and-random suffix per run names every non-canonical
        artifact — partials, review copies, failure/unverified records — with
        a kind prefix (failed | unverified | aborted), so one attempt's
        evidence groups together and retries never overwrite it"""
        if self._token_suffix is None:
            self._token_suffix = (f"{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}"
                                  f"-{uuid4().hex[:6]}")
        return f"{kind}-{self._token_suffix}"

    def convert(self) -> ConversionResult:
        """Main entry point for conversion"""
        self.start_time = datetime.datetime.now()
        self.run = RunContext(
            run_id=f"{self.start_time.strftime('%Y%m%dT%H%M%S')}_{self.config.operator}_{self.config.iso_name}",
            run_uuid=str(uuid4()),
            start_time=self.start_time,
        )

        result = self._convert_inner()

        # Generate records — every processed ISO gets a log and a manifest of
        # some kind, unless the record paths themselves failed the symlink guard.
        # Record writing is best-effort: a filesystem failure here must not
        # crash the run — but records are part of the deliverable, so a
        # nominally-successful conversion whose records can't be written is
        # reported as failed.
        if not result.records_unsafe:
            try:
                if result.success and not result.skipped:
                    self._generate_summary(result)
                    self._create_log_file(result)
                    self._create_manifest(result)
                elif result.skipped and not result.error_message:
                    self._create_skip_manifest(result)
                    self._create_log_file(result, dry_run=self.config.dry_run)
                elif result.error_message:
                    token = self._run_token("aborted" if result.aborted else "failed")
                    self._create_error_manifest(result, token)
                    self._create_log_file(result, is_error=True, failure_token=token)
                elif result.unverified:
                    # Nothing was promoted: records go under the run's token,
                    # leaving any existing canonical log and manifest untouched
                    token = self._run_token("unverified")
                    self._create_error_manifest(result, token)
                    self._create_log_file(result, is_error=True, failure_token=token)
            except Exception as record_error:
                log(colorize("red", f"Could not write run records: {record_error}"))
                if result.success or result.unverified:
                    result.success = False
                    result.unverified = False
                    # A skip without its record is not a valid skip — clear the
                    # flag so batch accounting counts this as a failure too
                    result.skipped = False
                    result.error_message = f"Run records could not be written: {record_error}"

        return result

    def _check_symlinks(self, paths: List[Path]) -> Optional[str]:
        """Refuse pre-existing symlinks at planned output paths (writing
        through one could clobber an arbitrary file elsewhere)"""
        linked = [p.name for p in paths if p.is_symlink()]
        if linked:
            message = f"Symlink at planned output path(s): {', '.join(linked)}"
            log(colorize("red", f"Refusing to continue: {message}"))
            return message
        return None

    def _convert_inner(self) -> ConversionResult:
        """Conversion flow; every return path gets records via convert()"""
        # Validate ISO exists
        if not self.config.iso_path.exists():
            return ConversionResult(
                success=False,
                iso_path=self.config.iso_path,
                error_message=f"ISO file not found: {self.config.iso_path}"
            )

        # Stat the source early so every record (including dependency
        # failures below) carries the real ISO size
        iso_size = self.config.iso_path.stat().st_size

        # Dependencies: ffmpeg (encode) AND ffprobe (analysis/verification)
        # are both required before any disc activity
        ffmpeg_available, ffmpeg_version = check_ffmpeg()
        if not ffmpeg_available:
            return ConversionResult(
                success=False,
                iso_path=self.config.iso_path,
                source_size=iso_size,
                error_message="ffmpeg not found. Please install ffmpeg."
            )
        ffprobe_available, _ = check_ffprobe()
        if not ffprobe_available:
            return ConversionResult(
                success=False,
                iso_path=self.config.iso_path,
                source_size=iso_size,
                error_message="ffprobe not found (it ships with ffmpeg). Please install ffmpeg."
            )
        log(colorize("cyan", f"Using ffmpeg version: {ffmpeg_version}"))

        # Symlink guard, phase 1: statically-known record/output paths.
        # (Per-title MP4 paths are guarded after titleset planning; failure
        # records use unpredictable tokened names and need no guard.)
        static_paths = [self.config.mp4_path, self.config.log_path,
                        self.config.dry_run_log_path, self.config.manifest_path,
                        self.config.skip_manifest_path]
        symlink_error = self._check_symlinks(static_paths)
        if symlink_error:
            return ConversionResult(
                success=False,
                iso_path=self.config.iso_path,
                source_size=iso_size,
                error_message=symlink_error,
                records_unsafe=True  # record paths themselves are suspect
            )

        log(colorize("cyan", f"ISO size: {format_bytes(iso_size)}"))

        # Mount and analyze ISO
        log_divider("Mounting ISO")
        result = None
        self.run.mark("mount")

        mount = ISOMount(self.config.iso_path)
        with mount as mount_point:
            if not mount_point:
                result = ConversionResult(
                    success=False,
                    iso_path=self.config.iso_path,
                    source_size=iso_size,
                    error_message="Failed to mount ISO"
                )
            else:
                # Analyze content
                log_divider("Analyzing Disc Content")
                self.run.mark("analyze")
                analysis = analyze_iso_content(mount_point)

                # An analysis error means the inventory may be incomplete
                # (e.g. the VIDEO_TS listing failed partway), so it is never
                # logged, planned or encoded as if it were complete
                analysis_failed = analysis.error is not None or analysis.disc_type == "error"
                if not analysis_failed:
                    self._log_analysis(analysis)

                if analysis_failed:
                    error = f"Analysis error: {analysis.error or 'unspecified failure'}"
                    log(colorize("red", f"{error} — nothing planned or encoded"))
                    result = ConversionResult(
                        success=False,
                        iso_path=self.config.iso_path,
                        source_size=iso_size,
                        disc_analysis=analysis,
                        error_message=error
                    )
                # Check if this is a VIDEO_TS disc
                elif not analysis.has_video_ts or not analysis.vob_files:
                    skip_reason = self._get_skip_reason(analysis)
                    log(colorize("yellow", f"Skipping: {skip_reason}"))

                    # Analysis errors should be counted as failures, not skips
                    is_error = analysis.disc_type == "error"

                    result = ConversionResult(
                        success=not is_error,
                        iso_path=self.config.iso_path,
                        source_size=iso_size,
                        disc_analysis=analysis,
                        skipped=not is_error,
                        skip_reason=skip_reason if not is_error else None,
                        error_message=skip_reason if is_error else None
                    )
                else:
                    # Plan titlesets (titleset-aware: grouped by VTS number;
                    # a titleset may still contain multiple programs/PGCs)
                    plans, menu_vobs, unrecognized = plan_titlesets(
                        analysis.vob_files, self.config.output_dir, self.config.iso_name)
                    self._log_titleset_plan(plans, menu_vobs, unrecognized)

                    existing_outputs = [] if self.config.dry_run else \
                        [p.output_path for p in plans if p.output_path.exists()]

                    # Symlink guard, phase 2: per-title output paths are only
                    # knowable now that titlesets are planned
                    output_symlink_error = self._check_symlinks([p.output_path for p in plans])

                    if output_symlink_error:
                        result = ConversionResult(
                            success=False,
                            iso_path=self.config.iso_path,
                            source_size=iso_size,
                            disc_analysis=analysis,
                            error_message=output_symlink_error
                        )
                    elif not plans:
                        # Menu-only structure: skip, never convert menus
                        skip_reason = "Menu-only VIDEO_TS structure (no content VOBs)"
                        log(colorize("yellow", f"Skipping: {skip_reason}"))
                        result = ConversionResult(
                            success=True,
                            iso_path=self.config.iso_path,
                            source_size=iso_size,
                            disc_analysis=analysis,
                            skipped=True,
                            skip_reason=skip_reason
                        )
                    elif existing_outputs and not self._confirm_overwrite(existing_outputs):
                        result = ConversionResult(
                            success=False,
                            iso_path=self.config.iso_path,
                            source_size=iso_size,
                            disc_analysis=analysis,
                            aborted=True,
                            error_message="Aborted by operator to avoid overwrite"
                        )
                    elif self.config.dry_run:
                        log(colorize("cyan", "Dry run:") + " " +
                            colorize("yellow", f"Would convert {len(plans)} titleset(s) to MP4"))
                        result = ConversionResult(
                            success=True,
                            mp4_path=self.config.mp4_path,
                            iso_path=self.config.iso_path,
                            source_size=iso_size,
                            disc_analysis=analysis,
                            skipped=True,
                            skip_reason="Dry run mode",
                            titles=[TitleOutput(vts_number=p.vts_number,
                                                output_path=p.output_path,
                                                status="planned")
                                    for p in plans]
                        )
                    else:
                        # Create output directory (only for actual conversion)
                        self.config.output_dir.mkdir(parents=True, exist_ok=True)
                        result = self._perform_conversion(analysis, iso_size, plans, unrecognized)

        # Surface any unmount failures into the record. A volume that fails to
        # detach (e.g. a handle still open) leaks an attached /dev/diskN that can
        # block later mounts in a batch, so it must be visible, not just logged.
        if mount.detach_errors:
            result.warnings.extend(mount.detach_errors)

        self.run.mark("detach")
        return result
    
    def _log_analysis(self, analysis: DiscContentAnalysis):
        """Log disc content analysis results"""
        log(colorize("cyan", f"Disc type: {analysis.disc_type}"))
        log(colorize("cyan", f"Has VIDEO_TS: {analysis.has_video_ts}"))
        log(colorize("cyan", f"Has AUDIO_TS: {analysis.has_audio_ts}"))
        
        if analysis.vob_files:
            log(colorize("cyan", f"VOB files found: {len(analysis.vob_files)}"))
            log(colorize("cyan", f"Total VOB size: {format_bytes(analysis.total_vob_size)}"))
            
            # Log VOB files to file only
            for vob in analysis.vob_files:
                log(colorize("cyan", f"  VOB: {vob.name} ({format_bytes(vob.stat().st_size)})"))
        
        if analysis.ifo_files:
            log(colorize("cyan", f"IFO files found: {len(analysis.ifo_files)}"))
        
        if analysis.error:
            log(colorize("red", f"Analysis error: {analysis.error}"))
    
    def _get_skip_reason(self, analysis: DiscContentAnalysis) -> str:
        """Get human-readable skip reason"""
        if analysis.disc_type == "data":
            return "Data disc (no VIDEO_TS folder)"
        elif analysis.disc_type == "dvd_audio":
            return "DVD-Audio disc (AUDIO_TS only, no VIDEO_TS)"
        elif analysis.disc_type == "empty":
            return "Empty or unreadable disc"
        elif analysis.disc_type == "error":
            return f"Analysis error: {analysis.error}"
        elif not analysis.vob_files:
            return "No VOB files found in VIDEO_TS"
        else:
            return "Unknown reason"
    
    def _confirm_overwrite(self, existing_paths: List[Path]) -> bool:
        """Confirm overwrite of pre-existing planned outputs"""
        names = ", ".join(p.name for p in existing_paths)
        response = get_input(colorize("yellow",
            f"Output exists ({names}). Overwrite? (y/n): "))
        return response.lower() == 'y'

    def _log_titleset_plan(self, plans: List[TitlesetPlan], menu_vobs: List[Path],
                           unrecognized: List[Path]):
        """Log the titleset conversion plan"""
        log(colorize("cyan", f"Content titlesets detected: {len(plans)}"))
        for plan in plans:
            label = "main" if plan.is_main else f"title{plan.vts_number:02d}"
            log(colorize("cyan", f"  VTS_{plan.vts_number:02d} ({label}) → {plan.output_path.name}: "
                                 f"{len(plan.content_vobs)} VOB(s), {format_bytes(plan.total_size)}"))
        if menu_vobs:
            log(colorize("yellow", f"Skipping {len(menu_vobs)} menu VOB(s): "
                                   + ", ".join(v.name for v in menu_vobs)))
        if unrecognized:
            log(colorize("yellow", "Unrecognized VOB name(s), not converted: "
                                   + ", ".join(v.name for v in unrecognized)))
    
    def _perform_conversion(self, analysis: DiscContentAnalysis, iso_size: int,
                            plans: List[TitlesetPlan],
                            unrecognized_vobs: List[Path]) -> ConversionResult:
        """Convert each planned titleset to its own MP4"""
        log_divider("Converting VIDEO_TS to MP4")
        log(colorize("cyan", "Encoding settings:"))
        log(colorize("cyan", f"  Video codec: {self.config.video_codec}"))
        log(colorize("cyan", f"  CRF: {self.config.crf} (lower = higher quality)"))
        log(colorize("cyan", f"  Preset: {self.config.preset}"))
        log(colorize("cyan", f"  Audio codec: {self.config.audio_codec}"))
        log(colorize("cyan", f"  Audio bitrate: {self.config.audio_bitrate}"))
        log(colorize("cyan", "  Stream mapping: -map 0:v:0 -map 0:a? -sn (all audio, no subtitles)"))

        result = ConversionResult(
            success=True,
            mp4_path=self.config.mp4_path,
            iso_path=self.config.iso_path,
            source_size=iso_size,
            disc_analysis=analysis,
        )

        if unrecognized_vobs:
            names = ", ".join(v.name for v in unrecognized_vobs)
            result.warnings.append(f"unrecognized VOB name(s) not converted: {names}")

        # Every planned titleset is recorded, attempted or not
        result.titles = [TitleOutput(vts_number=p.vts_number, output_path=p.output_path)
                         for p in plans]
        for plan, title in zip(plans, result.titles):
            label = "main title" if plan.is_main else f"title {plan.vts_number:02d}"
            log_divider(f"Encoding {label} → {plan.output_path.name}")

            # A titleset that does not pass blocks promotion of the whole
            # ISO, but remaining titlesets are still attempted so the record
            # is complete. Ctrl-C stops here: the interrupted title is
            # recorded as failed and remaining titlesets stay "not_attempted".
            try:
                # Source stream inventory (first content VOB is representative)
                source_counts = probe_stream_counts(plan.content_vobs[0]) or {}
                title.source_audio_streams = source_counts.get("audio")
                title.source_subtitle_streams = source_counts.get("subtitle")

                # Expected duration for the check after the encode: the VTS
                # IFO's playback time when it applies, else complete VOB probes
                ifo_path = next(
                    (f for f in analysis.ifo_files
                     if f.name.upper() == f"VTS_{plan.vts_number:02d}_0.IFO"), None)
                title.expectation = expected_titleset_duration(plan.content_vobs, ifo_path)
                title.expected_duration = title.expectation.seconds
                title.expected_duration_source = title.expectation.source

                if self.run:
                    self.run.mark(f"encode_vts_{plan.vts_number:02d}")
                self._encode_titleset(plan, title, result)
            except KeyboardInterrupt:
                result.interrupted = True
                result.conversion_time += title.encode_time
                log(colorize("red", "Remaining titleset(s) not attempted."))
                break
            except Exception as e:
                # e.g. a VOB that cannot be read while preparing: fail this
                # titleset, never strand earlier titlesets' checked outputs
                if title.status == "not_attempted":
                    title.status = "failed"
                    title.error = f"could not prepare the encode: {e}"
                    log(colorize("red", f"{plan.output_path.name}: {title.error}"))
            result.conversion_time += title.encode_time

        self._finish_titlesets(result)

        # Mirror the main title into the legacy top-level fields so existing
        # log/manifest consumers keep working — only for a file this run
        # actually promoted to the final name
        main = result.titles[0] if result.titles else None
        if main and main.promoted_this_run:
            result.output_size = main.output_size
            result.video_duration = main.video_duration
            result.md5_mp4 = main.md5
            result.sha256_mp4 = main.sha256
        else:
            result.mp4_path = None

        for t in result.titles:
            if t.retention_error:
                where = f"it is at {t.retained_path}" if t.retained_path else "it was not found"
                result.warnings.append(f"{t.output_path.name}: output could not be kept under "
                                       f"its {'.partial' if t.status == 'failed' else 'review'} "
                                       f"name ({t.retention_error}); {where}")

        # Run outcome: failure outranks unverified
        errors = [f"{t.output_path.name}: {t.error}" for t in result.titles if t.status == "failed"]
        if result.promotion_error:
            errors.append(result.promotion_error)
        if result.interrupted and not any("Interrupted" in e for e in errors):
            errors.append("Interrupted by operator (Ctrl-C)")
        if errors:
            result.success = False
            result.error_message = "; ".join(errors)
        elif not all(t.status == "completed" for t in result.titles):
            result.success = False
            result.unverified = True
            result.unverified_reason = "; ".join(
                f"{t.output_path.name}: {t.duration_check_reason}"
                for t in result.titles if t.status == "unverified")
        self._log_title_outcomes(result)
        return result

    def _finish_titlesets(self, result: ConversionResult):
        """Deferred promotion: the ISO's MP4s get their final names only when
        every planned titleset passed its check. Otherwise nothing is promoted,
        existing final files are left as they are, and each checked output
        (unverified, or passed but held) is kept as a review copy."""
        titles = result.titles
        if not result.interrupted and all(
                t.status == "encoded" and t.duration_check == "passed" for t in titles):
            self._promote_titlesets(result)

        remaining = [t for t in titles if t.status == "encoded"]
        if not remaining:
            return
        if result.promotion_error:
            why = f"promotion stopped partway ({result.promotion_error})"
        else:
            blockers = []
            for t in titles:
                if t.status == "failed":
                    blockers.append(f"{t.output_path.name} failed")
                elif t.status == "not_attempted":
                    blockers.append(f"{t.output_path.name} was not attempted")
                elif t.status == "encoded" and t.duration_check != "passed":
                    blockers.append(f"{t.output_path.name} is unverified")
            if result.interrupted:
                blockers.append("the run was interrupted")
            why = ", ".join(blockers)
        for title in remaining:
            if title.duration_check == "passed":
                title.status = "held"
                title.hold_reason = f"duration check passed, but not promoted: {why}"
            else:
                title.status = "unverified"
            # Until it is moved, the output is wherever it was encoded
            title.retained_path, title.retained_as = title.temp_path, "encoding_temp"
        for title in remaining:
            try:
                self._retain_attempt(title, title.temp_path, "unverified")
            except KeyboardInterrupt:
                result.interrupted = True
                log(colorize("red", "Interrupted while keeping review copies; "
                                    "the rest stay at their temporary names."))
                break

    def _promote_titlesets(self, result: ConversionResult):
        """Give every passed titleset its final name, in plan order.

        This is not one atomic transaction: if a rename fails or is
        interrupted, promotion stops, the run fails and the records name
        exactly which final files this run changed. There is no rollback."""
        if self.run:
            self.run.mark("promotion")
        for title in result.titles:
            existed = os.path.lexists(title.output_path)
            try:
                os.replace(title.temp_path, title.output_path)
            except KeyboardInterrupt:
                result.interrupted = True
                if not os.path.lexists(title.temp_path):  # the rename completed first
                    self._mark_promoted(title, existed)
                result.promotion_error = "Interrupted by operator (Ctrl-C) during promotion"
                break
            except OSError as e:
                result.promotion_error = (f"{title.output_path.name}: could not be promoted "
                                          f"to its final name: {e}")
                break
            self._mark_promoted(title, existed)
        else:
            return
        log(colorize("red", f"Promotion stopped: {result.promotion_error}"))
        changed = result.canonical_files_changed
        if changed:
            names = ", ".join(p.name for p in changed)
            old_records = [p.name for p in (self.config.manifest_path, self.config.log_path)
                           if p.exists()]
            if old_records:
                result.warnings.append(
                    f"this run replaced or created {names} but stopped before writing its "
                    f"own records: the existing {' and '.join(old_records)} describe an "
                    f"earlier run and no longer match {'it' if len(changed) == 1 else 'them'}")
            else:
                result.warnings.append(
                    f"this run replaced or created {names} but stopped before writing its "
                    f"own records: no canonical manifest describes "
                    f"{'it' if len(changed) == 1 else 'them'} (see this run's failure records)")

    @staticmethod
    def _mark_promoted(title: TitleOutput, existed: bool):
        title.promoted_this_run = True
        title.replaced_existing_canonical = existed
        title.status = "completed"
        title.temp_path = None

    def _retain_attempt(self, title: TitleOutput, path: Path, kind: str):
        """Keep this run's output for a titleset under a non-clobbering name.

        kind "failed"     -> <stem>_failed-<token>.mp4.partial
             "unverified" -> <stem>_unverified-<token>.mp4.unverified
                             (a review copy, not a deliverable)
        The name is reserved with an exclusive create first, so an existing
        file is never replaced; a taken name gets another random suffix. If
        the move fails, only this attempt's own empty reservation is removed
        and the output stays where it is; the record names its real location.
        Sets title.retained_path / retained_as / retention_error."""
        title.retained_path, title.retained_as, title.retention_error = None, None, None
        if not os.path.lexists(path):
            return
        # Truthful even if interrupted below: until moved, it is still here
        title.retained_path, title.retained_as = path, "encoding_temp"
        extension = ".mp4.partial" if kind == "failed" else ".mp4.unverified"
        base = f"{title.output_path.stem}_{self._run_token(kind)}"
        error = f"no free name found after {RETENTION_ATTEMPTS} attempts"
        for attempt in range(RETENTION_ATTEMPTS):
            name = f"{base}{extension}" if attempt == 0 else f"{base}-{uuid4().hex[:6]}{extension}"
            target = path.with_name(name)
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            except OSError as e:
                error = f"could not reserve {name}: {e}"
                break
            try:
                reserved = os.fstat(fd)
            finally:
                os.close(fd)
            try:
                os.replace(path, target)
            except BaseException as e:
                leftover = self._remove_reservation(target, reserved)
                if not isinstance(e, OSError):
                    raise
                error = f"could not move it to {name}: {e}{leftover}"
                break
            title.retained_path = target
            title.retained_as = "partial" if kind == "failed" else "review_copy"
            log(colorize("yellow", f"{title.output_path.name}: kept as {target.name}"
                                   + ("" if kind == "failed" else " (review copy, not a deliverable)")))
            return
        title.retention_error = error
        if not os.path.lexists(path):
            title.retained_path, title.retained_as = None, None
        log(colorize("yellow", f"Warning: {title.output_path.name}: {error}; "
                               f"the output stays at {path}"))

    @staticmethod
    def _remove_reservation(target: Path, reserved: os.stat_result) -> str:
        """Delete this attempt's own empty placeholder, never anything else.
        Returns a note for the record if a placeholder had to be left."""
        try:
            current = os.lstat(target)
        except FileNotFoundError:
            return ""
        except OSError as e:
            return f" (could not check the empty placeholder {target.name}: {e})"
        if not ((current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino)
                and stat.S_ISREG(current.st_mode) and current.st_size == 0):
            return ""  # not our placeholder any more: leave it alone
        try:
            os.unlink(target)
        except OSError as e:
            return f" (an empty placeholder {target.name} could not be removed: {e})"
        return ""

    def _log_title_outcomes(self, result: ConversionResult):
        """One line per titleset: what the check found and where the file is"""
        log_divider("Titleset Outcomes")
        for t in result.titles:
            name = t.output_path.name
            kept = f" Kept as {t.retained_path.name}." if t.retained_path else ""
            if t.status == "completed":
                log(colorize("green", f"{name}: duration check passed; promoted"
                                      + (" (replaced the earlier file)" if t.replaced_existing_canonical else "")))
            elif t.status == "held":
                log(colorize("yellow", f"{name}: HELD — {t.hold_reason}.{kept}"))
            elif t.status == "unverified":
                log(colorize("yellow", f"{name}: UNVERIFIED — {t.duration_check_reason}.{kept}"))
            elif t.status == "failed":
                log(colorize("red", f"{name}: FAILED — {t.error}.{kept}"))
            else:
                log(colorize("yellow", f"{name}: not attempted"))
            if t.retention_error:
                log(colorize("yellow", f"  could not be kept under its review name: {t.retention_error}"))
        if result.titles and not all(t.status == "completed" for t in result.titles):
            if result.canonical_files_changed:
                log(colorize("red", "Final MP4s changed by this run: "
                                    + ", ".join(p.name for p in result.canonical_files_changed)))
            else:
                log(colorize("yellow", "Nothing was promoted: existing final MP4s and records "
                                       "were left as they were."))
            if any(t.retained_as == "review_copy" for t in result.titles):
                log(colorize("yellow", "Review copies (*.mp4.unverified) are not deliverables."))

    def _encode_titleset(self, plan: TitlesetPlan, title: TitleOutput,
                         result: ConversionResult):
        """Encode one titleset's content VOBs to a temp MP4 and check it.

        A failed attempt is kept as .partial at once. A passed or unverified
        output stays at title.temp_path (status "encoded") until every
        titleset is done and the promotion decision is made."""
        for vob in plan.content_vobs:
            log(colorize("cyan", f"  + {vob.name} ({format_bytes(vob.stat().st_size)})"))

        # Encode to a run-private temp path; the final name is only ever
        # written by an atomic replace() after every titleset of the ISO
        # passes. ffmpeg never opens the final path, so a failed retry cannot
        # damage a pre-existing good file there.
        while True:
            temp_path = plan.output_path.with_name(
                f"{plan.output_path.stem}.encoding-{uuid4().hex[:6]}.mp4")
            if not temp_path.exists():
                break

        concat_input = "concat:" + "|".join(str(v) for v in plan.content_vobs)
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",  # Overwrite output
            "-i", concat_input,
            "-map", "0:v:0",   # first video stream
            "-map", "0:a?",    # ALL audio streams ('?' tolerates audio-less input)
            "-sn",             # subtitles deliberately excluded (recorded in manifest)
            "-c:v", self.config.video_codec,
            "-preset", self.config.preset,
            "-crf", str(self.config.crf),
            "-c:a", self.config.audio_codec,
            "-b:a", self.config.audio_bitrate,
            "-movflags", "+faststart",  # Enable streaming
            "-pix_fmt", "yuv420p",  # Compatibility
            str(temp_path)
        ]
        log(colorize("cyan", "Command:") + " " + colorize("yellow", " ".join(ffmpeg_cmd)))

        encode_start = time.time()
        try:
            # Run ffmpeg with progress output. stdout is discarded rather than
            # piped: we only read stderr below, and an unread stdout pipe could
            # fill its OS buffer and deadlock the encode if ffmpeg ever writes to
            # it (no reader would drain it).
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )

            # Parse ffmpeg stderr for progress (ffmpeg outputs progress to stderr)
            log("")  # Blank line before progress display
            stderr_output = []
            last_plain_progress = time.time()

            while True:
                line = process.stderr.readline()
                if not line and process.poll() is not None:
                    break

                stderr_output.append(line)

                # Parse timecode from ffmpeg output (format: time=HH:MM:SS.ss)
                match = re.search(r'time=(\d+:\d+:\d+\.\d+)', line)
                if match:
                    elapsed = time.time() - encode_start
                    if ANSI_ENABLED:
                        # Update progress display (overwrite same line)
                        sys.stdout.write(f"\r{colorize('cyan', 'Encoding:')} {match.group(1)}  |  {colorize('cyan', 'Elapsed:')} {format_duration(elapsed)}    ")
                        sys.stdout.flush()
                    elif time.time() - last_plain_progress >= 15:
                        # Redirected output: a plain line every ~15s instead
                        last_plain_progress = time.time()
                        log(f"Encoding {plan.output_path.name}: {match.group(1)}  |  Elapsed: {format_duration(elapsed)}")

            if ANSI_ENABLED:
                # Clear progress line and move to new line
                sys.stdout.write("\r" + " " * 80 + "\r")
                sys.stdout.flush()

            title.encode_time = time.time() - encode_start

            if process.returncode != 0:
                stderr_text = "".join(stderr_output)
                log(colorize("red", f"ffmpeg error:\n{stderr_text}"))
                title.status = "failed"
                title.error = f"ffmpeg failed with return code {process.returncode}"
                self._retain_attempt(title, temp_path, "failed")
                return

        except KeyboardInterrupt:
            log(colorize("red", "\nConversion interrupted by user."))
            process.kill()
            process.wait()
            title.encode_time = time.time() - encode_start
            title.status = "failed"
            title.error = "Interrupted by operator (Ctrl-C)"
            self._retain_attempt(title, temp_path, "failed")
            raise
        except Exception as e:
            title.encode_time = time.time() - encode_start
            title.status = "failed"
            title.error = str(e)
            self._retain_attempt(title, temp_path, "failed")
            return

        # Post-encode checks share the encode's failure handling: any error
        # here fails the title, keeps the temp as .partial, and flows into
        # the normal failure records (it must never escape the record
        # machinery).
        name = plan.output_path.name
        stage = "verification"
        try:
            if not temp_path.exists():
                title.status = "failed"
                title.error = "Output file was not created"
                return

            title.output_size = temp_path.stat().st_size
            if title.output_size == 0:
                title.status = "failed"
                title.error = "Output file is empty"
                log(colorize("red", f"{name}: {title.error}"))
                self._retain_attempt(title, temp_path, "failed")
                return
            title.video_duration = get_video_duration(temp_path)

            # Record what was actually mapped (closes the loop on stream loss);
            # None = the probe failed, as distinct from a probe finding no video
            output_counts = probe_stream_counts(temp_path)
            if output_counts is not None:
                title.mapped_video_streams = output_counts["video"]
                title.mapped_audio_streams = output_counts["audio"]
                if title.source_audio_streams and title.mapped_audio_streams < title.source_audio_streams:
                    result.warnings.append(
                        f"{name}: only {title.mapped_audio_streams} of "
                        f"{title.source_audio_streams} source audio stream(s) mapped")

            # Duration check. A known truncation or missing video FAILS the
            # title; a check that cannot be completed leaves it UNVERIFIED.
            # Neither is promoted (see _finish_titlesets).
            (title.duration_check, title.duration_check_reason,
             title.duration_tolerance) = duration_check(
                title.expected_duration,
                title.expectation.reason if title.expectation else None,
                title.video_duration,
                None if output_counts is None else output_counts["video"])
            if title.duration_check == "failed":
                title.status = "failed"
                title.error = title.duration_check_reason
                log(colorize("red", f"{name}: {title.error}"))
                self._retain_attempt(title, temp_path, "failed")
                return

            # Calculate checksums (MD5 + SHA-256 in one read)
            stage = "checksum"
            if self.run:
                self.run.mark(f"checksum_vts_{plan.vts_number:02d}")
            log(colorize("cyan", "Calculating MD5 and SHA-256 of output file..."))
            title.md5, title.sha256 = calculate_checksums(temp_path)
            if title.md5 is None:
                result.warnings.append(
                    f"{name}: checksum calculation failed — no fixity recorded")

        except KeyboardInterrupt:
            title.status = "failed"
            title.error = f"Interrupted by operator (Ctrl-C) during post-encode {stage}"
            self._retain_attempt(title, temp_path, "failed")
            raise
        except Exception as e:
            title.status = "failed"
            title.error = f"post-encode {stage} failed: {e}"
            log(colorize("red", f"{name}: {title.error}"))
            self._retain_attempt(title, temp_path, "failed")
            return

        title.temp_path = temp_path
        title.status = "encoded"
        colour = "green" if title.duration_check == "passed" else "yellow"
        log(colorize(colour, f"{name} encoded ({format_bytes(title.output_size)}, "
                             f"{format_timecode(title.video_duration) if title.video_duration else 'duration n/a'}, "
                             f"{format_count(title.mapped_audio_streams)} audio track(s)); duration check "
                             f"{title.duration_check}: {title.duration_check_reason}"))
    
    def _generate_summary(self, result: ConversionResult):
        """Generate timing and summary information"""
        log_divider("Summary")
        
        log(colorize("cyan", f"Source ISO: {result.iso_path.name}"))
        log(colorize("cyan", f"Output MP4: {result.mp4_path.name if result.mp4_path else 'N/A'}"))
        if len(result.titles) > 1:
            log(colorize("cyan", f"Titleset outputs ({len(result.titles)}):"))
            for t in result.titles:
                duration_str = format_timecode(t.video_duration) if t.video_duration else "n/a"
                log(colorize("cyan", f"  {t.output_path.name}: {t.status}, "
                                     f"{format_bytes(t.output_size)}, {duration_str}, "
                                     f"{format_count(t.mapped_audio_streams)} audio track(s)"))
        log(colorize("cyan", f"Source size: {format_bytes(result.source_size)}"))
        log(colorize("cyan", f"Output size: {format_bytes(result.output_size) if result.output_size else 'N/A'}"))
        
        if result.compression_ratio:
            log(colorize("cyan", f"Compression ratio: {result.compression_ratio}:1"))
        
        if result.video_duration:
            log(colorize("cyan", f"Video duration: {format_timecode(result.video_duration)}"))
        
        log(colorize("cyan", f"Conversion time: {format_duration(result.conversion_time)}"))
        
        if result.speed_factor:
            log(colorize("cyan", f"Encoding speed: {result.speed_factor}x realtime"))
        
        log(colorize("cyan", f"MD5 checksum: {result.md5_mp4}"))
        log(colorize("cyan", f"SHA-256 checksum: {result.sha256_mp4}"))
        for warning in result.warnings:
            log(colorize("yellow", f"Warning: {warning}"))
        log(colorize("green", "Conversion completed successfully!"))
    
    def _create_log_file(self, result: ConversionResult, dry_run: bool = False,
                         is_error: bool = False, failure_token: Optional[str] = None):
        """Create formatted log file.

        Failure/aborted/unverified logs are written under the run's token so
        such a retry never overwrites the log a prior success manifest
        references (nor an earlier failure's log)."""
        if dry_run:
            log_path = self.config.dry_run_log_path
        elif failure_token:
            log_path = self.config.output_dir / f"{self.config.iso_name}_{failure_token}.log.txt"
        else:
            log_path = self.config.log_path
    
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
    
        end_time = datetime.datetime.now()
        total_duration = (end_time - self.start_time).total_seconds()
        
        with open(log_path, "w") as f:
            f.write("=" * 70 + "\n")
            if dry_run:
                f.write("ISO TO MP4 ACCESS COPY CONVERSION LOG (DRY RUN)\n")
            elif is_error:
                header = 'ABORTED' if result.aborted else ('UNVERIFIED' if result.unverified else 'ERROR')
                f.write(f"ISO TO MP4 ACCESS COPY CONVERSION LOG ({header})\n")
            elif result.skipped:
                f.write("ISO TO MP4 ACCESS COPY CONVERSION LOG (SKIPPED)\n")
            else:
                f.write("ISO TO MP4 ACCESS COPY CONVERSION LOG\n")
            f.write("=" * 70 + "\n\n")
            f.write("CONVERSION SUMMARY\n")
            f.write("-" * 20 + "\n")
            run_id = self.run.run_id if self.run else \
                f"{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}_{self.config.operator}_{self.config.iso_name}"
            f.write(f"Run ID:           {run_id}\n")
            f.write(f"Operator:         {self.config.operator}\n")
            f.write(f"Start Time:       {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"End Time:         {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Total Duration:   {format_duration(total_duration)}\n")
            if dry_run:
                status = 'DRY RUN'
            elif result.aborted:
                status = 'ABORTED'
            elif result.skipped and result.success:
                status = 'SKIPPED'
            elif result.success:
                status = 'SUCCESS (WITH WARNINGS)' if result.warnings else 'SUCCESS'
            elif result.unverified:
                status = 'UNVERIFIED (nothing promoted)'
            else:
                status = 'FAILED'
            f.write(f"Status:           {status}\n")
            if result.skip_reason:
                f.write(f"Skip Reason:      {result.skip_reason}\n")
            if result.error_message:
                f.write(f"Error:            {result.error_message}\n")
            if result.unverified_reason:
                f.write(f"Unverified:       {result.unverified_reason}\n")
            if result.titles and not dry_run and not result.success:
                changed = result.canonical_files_changed
                f.write("Final MP4s:       "
                        + (("changed by this run: " + ", ".join(p.name for p in changed))
                           if changed else "none promoted or changed by this run") + "\n")
                if any(t.retained_as == "review_copy" for t in result.titles):
                    f.write("Note:             review copies (*.mp4.unverified) are not deliverables\n")
            for warning in result.warnings:
                f.write(f"Warning:          {warning}\n")
            f.write("\n")

            if self.run and self.run.step_times:
                f.write("OPERATION TIMELINE\n")
                f.write("-" * 20 + "\n")
                for step, step_time in self.run.step_times.items():
                    f.write(f"[{step_time.strftime('%H:%M:%S')}] {step}\n")
                f.write("\n")
            
            f.write("SOURCE INFORMATION\n")
            f.write("-" * 20 + "\n")
            f.write(f"ISO File:         {result.iso_path.name}\n")
            f.write(f"ISO Path:         {result.iso_path}\n")
            f.write(f"ISO Size:         {format_bytes(result.source_size)}\n")
            
            if result.disc_analysis:
                f.write(f"Disc Type:        {result.disc_analysis.disc_type}\n")
                f.write(f"VOB Files:        {len(result.disc_analysis.vob_files)}\n")
                f.write(f"Total VOB Size:   {format_bytes(result.disc_analysis.total_vob_size)}\n")
            f.write("\n")
            
            f.write("OUTPUT INFORMATION\n")
            f.write("-" * 20 + "\n")
            f.write(f"MP4 File:         {result.mp4_path.name if result.mp4_path else 'N/A (no final MP4 promoted by this run)'}\n")
            f.write(f"MP4 Path:         {result.mp4_path if result.mp4_path else 'N/A'}\n")
            f.write(f"MP4 Size:         {format_bytes(result.output_size) if result.output_size else 'N/A'}\n")
            
            if result.video_duration:
                f.write(f"Duration:         {format_timecode(result.video_duration)}\n")
            
            if result.compression_ratio:
                f.write(f"Compression:      {result.compression_ratio}:1\n")

            f.write(f"MD5 Checksum:     {result.md5_mp4}\n")
            f.write(f"SHA-256 Checksum: {result.sha256_mp4}\n\n")

            if result.titles:
                f.write("TITLESET OUTPUTS\n")
                f.write("-" * 20 + "\n")
                f.write("Note: titleset-aware (VTS grouping); a titleset may contain\n")
                f.write("multiple programs. Subtitles recorded but not encoded.\n")
                for t in result.titles:
                    duration_str = format_timecode(t.video_duration) if t.video_duration else "n/a"
                    f.write(f"{t.output_path.name}\n")
                    f.write(f"  Status:   {t.status}\n")
                    f.write(f"  Size:     {format_bytes(t.output_size)}\n")
                    f.write(f"  Duration: {duration_str}")
                    if t.expected_duration:
                        f.write(f" (expected ~{format_timecode(t.expected_duration)}, "
                                f"from {'IFO' if t.expected_duration_source == 'ifo' else 'VOB probes'})")
                    f.write("\n")
                    if t.duration_check:
                        f.write(f"  Check:    {t.duration_check} — {t.duration_check_reason}\n")
                    if t.expectation and t.expectation.ifo and t.expectation.ifo.status != "used":
                        ifo = t.expectation.ifo
                        advisory = (f" ({format_timecode(ifo.seconds)}, not used)"
                                    if ifo.seconds else "")
                        f.write(f"  IFO:      {ifo.status}{advisory} — {ifo.reason}\n")
                    f.write(f"  Audio:    {format_count(t.mapped_audio_streams)} of "
                            f"{format_count(t.source_audio_streams)} source track(s) mapped\n")
                    f.write(f"  MD5:      {t.md5}\n")
                    f.write(f"  SHA-256:  {t.sha256}\n")
                    if t.status not in ("planned", "not_attempted"):
                        f.write(f"  Intended: {t.output_path}\n")
                        promoted = "no"
                        if t.promoted_this_run:
                            promoted = ("yes, replacing an earlier file" if t.replaced_existing_canonical
                                        else "yes (new file)")
                        f.write(f"  Promoted: {promoted}\n")
                    if t.retained_path:
                        kind = {"partial": "failed attempt",
                                "review_copy": "review copy, not a deliverable",
                                "encoding_temp": "left at its temporary name"}.get(t.retained_as, "")
                        f.write(f"  Kept as:  {t.retained_path} ({kind})\n")
                    if t.retention_error:
                        f.write(f"  Not moved: {t.retention_error}\n")
                    if t.hold_reason:
                        f.write(f"  Held:     {t.hold_reason}\n")
                    if t.error:
                        f.write(f"  Error:    {t.error}\n")
                f.write("\n")
            
            f.write("ENCODING SETTINGS\n")
            f.write("-" * 20 + "\n")
            f.write(f"Video Codec:      {self.config.video_codec}\n")
            f.write(f"CRF Quality:      {self.config.crf}\n")
            f.write(f"Preset:           {self.config.preset}\n")
            f.write(f"Audio Codec:      {self.config.audio_codec}\n")
            f.write(f"Audio Bitrate:    {self.config.audio_bitrate}\n\n")
            
            f.write("PERFORMANCE\n")
            f.write("-" * 20 + "\n")
            f.write(f"Conversion Time:  {format_duration(result.conversion_time)}\n")
            
            if result.speed_factor:
                f.write(f"Encoding Speed:   {result.speed_factor}x realtime\n")
            f.write("\n")
            
            f.write("SYSTEM INFORMATION\n")
            f.write("-" * 20 + "\n")
            f.write(f"Operating System: {platform.system()} {platform.release()}\n")
            f.write(f"Machine:          {platform.machine()}\n")
            f.write(f"Python Version:   {platform.python_version()}\n")
            
            ffmpeg_available, ffmpeg_version = check_ffmpeg()
            f.write(f"FFmpeg Version:   {ffmpeg_version}\n")
            f.write(f"Tool Version:     v1.0\n\n")
            
            f.write("=" * 70 + "\n")
            f.write(f"Log generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 70 + "\n")
        
        log(colorize("cyan", f"Log saved to: {log_path}"))
    
    def _run_identity(self, now: datetime.datetime) -> Dict[str, str]:
        """Shared run_id/uuid for all record writers"""
        if self.run:
            return {"run_id": self.run.run_id, "uuid": self.run.run_uuid}
        return {"run_id": f"{now.strftime('%Y%m%dT%H%M%S')}_{self.config.operator}_{self.config.iso_name}",
                "uuid": str(uuid4())}

    def _titlesets_section(self, result: ConversionResult) -> Dict[str, Any]:
        """Per-titleset outcomes — identical in success, error, and skip manifests
        so a mixed run (one title completed, one failed) loses nothing"""
        return {
            "count": len(result.titles),
            "note": ("Titleset-aware (grouped by VTS number): a single titleset may still "
                     "contain multiple programs/PGCs/angles — this is not DVD-player title "
                     "parsing. Subtitle streams are recorded but deliberately not encoded "
                     "(DVD bitmap subtitles are not MP4-compatible). A passed duration "
                     "check means only that the output's length matches the expectation "
                     "within the tolerance. Review copies (*.mp4.unverified) are not "
                     "deliverables."),
            "outputs": [
                {
                    "vts_number": t.vts_number,
                    "filename": t.output_path.name,
                    "is_main": i == 0,
                    "status": t.status,
                    "size_bytes": t.output_size,
                    "duration_seconds": t.video_duration,
                    "expected_duration_seconds": t.expected_duration,
                    "duration_check": self._duration_check_record(t),
                    "encode_time_seconds": t.encode_time,
                    "md5_checksum": t.md5,
                    "sha256_checksum": t.sha256,
                    "source_audio_streams": t.source_audio_streams,
                    "source_subtitle_streams": t.source_subtitle_streams,
                    "mapped_video_streams": t.mapped_video_streams,
                    "mapped_audio_streams": t.mapped_audio_streams,
                    "final_intended_path": str(t.output_path),
                    "promoted_this_run": t.promoted_this_run,
                    "replaced_existing_canonical": t.replaced_existing_canonical,
                    "retained_path": str(t.retained_path) if t.retained_path else None,
                    "retained_as": t.retained_as,
                    "retention_error": t.retention_error,
                    "hold_reason": t.hold_reason,
                    "partial_renamed_to": (str(t.retained_path)
                                           if t.retained_as == "partial" else None),
                    "error": t.error
                } for i, t in enumerate(result.titles)
            ]
        }

    @staticmethod
    def _duration_check_record(t: TitleOutput) -> Dict[str, Any]:
        """The duration check and the evidence behind it, for one titleset"""
        e = t.expectation
        return {
            "result": t.duration_check,
            "reason": t.duration_check_reason,
            "expected_seconds": t.expected_duration,
            "expected_source": t.expected_duration_source,
            "output_seconds": t.video_duration,
            "tolerance_seconds": t.duration_tolerance,
            "ifo": ({"status": e.ifo.status, "seconds": e.ifo.seconds, "reason": e.ifo.reason}
                    if e and e.ifo else None),
            "vob_probe": ({"seconds": e.vob_probe_seconds, "complete": e.vob_probe_complete}
                          if e and e.vob_probe_complete is not None else None),
        }

    def _create_manifest(self, result: ConversionResult):
        """Create comprehensive JSON manifest"""
        now = datetime.datetime.now()
        
        # Get ffmpeg version
        _, ffmpeg_version = check_ffmpeg()
        
        # Get video info
        video_info = get_video_info(result.mp4_path) if result.mp4_path else {}
        
        manifest = {
            "conversion_metadata": {
                **self._run_identity(now),
                "tool_name": "ISO to MP4 Access Copy Utility",
                "tool_version": "v1.0",
                "created": now.isoformat(timespec="seconds"),
                "operator": self.config.operator,
                "dry_run": self.config.dry_run
            },

            "conversion_status": {
                "overall_status": (("success_with_warnings" if result.warnings else "success")
                                   if result.success else "failed"),
                # Created or replaced by THIS run (never inferred from a file existing)
                "mp4_created": bool(result.titles) and result.titles[0].promoted_this_run,
                "canonical_files_changed": [str(p) for p in result.canonical_files_changed],
                "skipped": result.skipped,
                "skip_reason": result.skip_reason,
                "warnings": result.warnings,
                "errors": result.error_message
            },
            
            "source_iso": {
                "filename": result.iso_path.name if result.iso_path else None,
                "origin_path": str(result.iso_path) if result.iso_path else None,
                "size_bytes": result.source_size,
                "size_formatted": format_bytes(result.source_size)
            },
            
            "disc_analysis": {
                "disc_type": result.disc_analysis.disc_type if result.disc_analysis else None,
                "has_video_ts": result.disc_analysis.has_video_ts if result.disc_analysis else False,
                "has_audio_ts": result.disc_analysis.has_audio_ts if result.disc_analysis else False,
                "vob_file_count": len(result.disc_analysis.vob_files) if result.disc_analysis else 0,
                "total_vob_size_bytes": result.disc_analysis.total_vob_size if result.disc_analysis else 0,
                "total_vob_size_formatted": format_bytes(result.disc_analysis.total_vob_size) if result.disc_analysis else None,
                "ifo_file_count": len(result.disc_analysis.ifo_files) if result.disc_analysis else 0,
                "vob_files": [f.name for f in result.disc_analysis.vob_files] if result.disc_analysis else []
            },
            
            "output_mp4": {
                "filename": result.mp4_path.name if result.mp4_path else None,
                "origin_path": str(result.mp4_path) if result.mp4_path else None,
                "size_bytes": result.output_size,
                "size_formatted": format_bytes(result.output_size),
                "duration_seconds": result.video_duration,
                "duration_formatted": format_timecode(result.video_duration) if result.video_duration else None,
                "md5_checksum": result.md5_mp4,
                "sha256_checksum": result.sha256_mp4,
                "compression_ratio": result.compression_ratio
            },

            "titlesets": self._titlesets_section(result),
            
            "encoding_settings": {
                "video_codec": self.config.video_codec,
                "crf_quality": self.config.crf,
                "preset": self.config.preset,
                "audio_codec": self.config.audio_codec,
                "audio_bitrate": self.config.audio_bitrate,
                "pixel_format": "yuv420p",
                "movflags": "+faststart",
                "stream_mapping": "-map 0:v:0 -map 0:a? -sn (first video, all audio, no subtitles)"
            },
            
            "performance": {
                "conversion_time_seconds": result.conversion_time,
                "conversion_time_formatted": format_duration(result.conversion_time),
                "encoding_speed_factor": result.speed_factor,
                "encoding_speed_description": f"{result.speed_factor}x realtime" if result.speed_factor else None
            },
            
            "video_technical_info": self._extract_video_info(video_info),
            
            "system_environment": {
                "operating_system": platform.system(),
                "os_version": platform.release(),
                "machine_architecture": platform.machine(),
                "processor": platform.processor(),
                "python_version": platform.python_version(),
                "ffmpeg_version": ffmpeg_version
            },
            
            "output_files": {
                "mp4_file": str(result.mp4_path) if result.mp4_path else None,
                "log_file": str(self.config.log_path),
                "manifest_file": str(self.config.manifest_path)
            },
            
            "archival_notes": {
                "purpose": "Access copy for viewing/streaming",
                "quality_level": "Visually lossless (CRF 18)" if self.config.crf == 18 else f"CRF {self.config.crf}",
                "preservation_note": "This is an access copy derived from the preservation ISO. The original ISO is the archival master.",
                "project": "Johnson Publishing Company Archive (JPCA)",
                "institutions": [
                    "Getty Research Institute",
                    "Smithsonian National Museum of African American History and Culture"
                ]
            }
        }
        
        with open(self.config.manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=False)
        
        log(colorize("cyan", f"Manifest saved to: {self.config.manifest_path}"))
    
    def _create_skip_manifest(self, result: ConversionResult):
        """Create manifest for skipped files (and dry runs)"""
        now = datetime.datetime.now()

        manifest = {
            "conversion_metadata": {
                **self._run_identity(now),
                "tool_name": "ISO to MP4 Access Copy Utility",
                "tool_version": "v1.0",
                "created": now.isoformat(timespec="seconds"),
                "operator": self.config.operator,
                "dry_run": self.config.dry_run
            },

            "conversion_status": {
                "overall_status": "dry_run" if self.config.dry_run else "skipped",
                "skipped": True,
                "skip_reason": result.skip_reason,
                "warnings": result.warnings
            },

            "source_iso": {
                "filename": result.iso_path.name if result.iso_path else None,
                "origin_path": str(result.iso_path) if result.iso_path else None,
                "size_bytes": result.source_size,
                "size_formatted": format_bytes(result.source_size)
            },

            "disc_analysis": {
                "disc_type": result.disc_analysis.disc_type if result.disc_analysis else None,
                "has_video_ts": result.disc_analysis.has_video_ts if result.disc_analysis else False,
                "has_audio_ts": result.disc_analysis.has_audio_ts if result.disc_analysis else False,
                "recommendation": self._get_skip_recommendation(result.disc_analysis, result.skip_reason)
            },

            "titlesets": self._titlesets_section(result),

            "archival_notes": {
                "note": result.skip_reason if result.skip_reason else "Skipped",
                "project": "Johnson Publishing Company Archive (JPCA)"
            }
        }

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config.skip_manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=False)

        log(colorize("cyan", f"Skip manifest saved to: {self.config.skip_manifest_path}"))
    
    def _create_error_manifest(self, result: ConversionResult, failure_token: str):
        """Create manifest for failed, operator-aborted or unverified conversions.

        Carries the full per-titleset record so a mixed run (one title
        passed, another failed or unverified) loses nothing machine-readable.
        Written under the run's token so retries never overwrite prior
        evidence (the success manifest or earlier failures)."""
        now = datetime.datetime.now()
        status = "aborted" if result.aborted else ("unverified" if result.unverified else "error")

        manifest = {
            "conversion_metadata": {
                **self._run_identity(now),
                "tool_name": "ISO to MP4 Access Copy Utility",
                "tool_version": "v1.0",
                "created": now.isoformat(timespec="seconds"),
                "operator": self.config.operator,
                "dry_run": self.config.dry_run
            },

            "conversion_status": {
                "overall_status": status,
                "skipped": False,
                "interrupted": result.interrupted,
                "warnings": result.warnings,
                "error_message": result.error_message,
                "unverified_reason": result.unverified_reason,
                "promotion_error": result.promotion_error,
                "canonical_files_changed": [str(p) for p in result.canonical_files_changed]
            },

            "output_files": {
                "log_file": str(self.config.output_dir / f"{self.config.iso_name}_{failure_token}.log.txt")
            },

            "source_iso": {
                "filename": result.iso_path.name if result.iso_path else None,
                "origin_path": str(result.iso_path) if result.iso_path else None,
                "size_bytes": result.source_size,
                "size_formatted": format_bytes(result.source_size)
            },

            "disc_analysis": {
                "disc_type": result.disc_analysis.disc_type if result.disc_analysis else None,
                "has_video_ts": result.disc_analysis.has_video_ts if result.disc_analysis else False,
                "has_audio_ts": result.disc_analysis.has_audio_ts if result.disc_analysis else False,
                "error": result.disc_analysis.error if result.disc_analysis else None
            },

            "titlesets": self._titlesets_section(result),

            "archival_notes": {
                "note": ("Conversion aborted by operator before any encoding"
                         if result.aborted else
                         ("Nothing was promoted: at least one titleset could not be "
                          "verified. Existing final MP4s and records were left as they "
                          "were; review copies (*.mp4.unverified) are not deliverables."
                          if result.unverified else
                          "This ISO encountered an error during processing")),
                "project": "Johnson Publishing Company Archive (JPCA)"
            }
        }

        manifest_path = self.config.output_dir / f"{self.config.iso_name}_{failure_token}_manifest.json"
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=False)

        label = "Aborted" if result.aborted else ("Unverified" if result.unverified else "Error")
        log(colorize("cyan", f"{label} manifest saved to: {manifest_path}"))
    
    def _get_skip_recommendation(self, analysis: Optional[DiscContentAnalysis],
                                 skip_reason: Optional[str] = None) -> str:
        """Get recommendation for handling skipped discs"""
        # Menu-only discs report disc_type dvd_video (VIDEO_TS exists), so the
        # skip reason must take precedence over the disc type here
        if skip_reason and skip_reason.startswith("Menu-only"):
            return ("Menu-only VIDEO_TS structure — nothing to convert; "
                    "menus remain intact in the preservation ISO")
        if skip_reason == "Dry run mode":
            return "Dry run — rerun without --dry-run to convert"

        if not analysis:
            return "Unknown disc type - manual review recommended"

        if analysis.disc_type == "dvd_video":
            return "DVD-Video disc - ready for conversion"
        elif analysis.disc_type == "mixed":
            return "DVD-Video disc with AUDIO_TS - ready for conversion (video only)"
        elif analysis.disc_type == "data":
            return "Data disc - consider alternate extraction method (file copy)"
        elif analysis.disc_type == "dvd_audio":
            return "DVD-Audio disc - requires specialized audio extraction tools"
        elif analysis.disc_type == "empty":
            return "Empty or unreadable disc - verify ISO integrity"
        else:
            return "Manual review recommended"
    
    def _extract_video_info(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """Extract relevant video technical information from ffprobe output"""
        if not info:
            return {}
        
        result = {}
        
        # Format info
        fmt = info.get("format", {})
        result["container_format"] = fmt.get("format_long_name")
        result["duration_seconds"] = float(fmt.get("duration", 0))
        result["bitrate"] = int(fmt.get("bit_rate", 0))
        
        # Stream info
        for stream in info.get("streams", []):
            codec_type = stream.get("codec_type")
            
            if codec_type == "video":
                result["video"] = {
                    "codec": stream.get("codec_long_name"),
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "frame_rate": stream.get("r_frame_rate"),
                    "pixel_format": stream.get("pix_fmt"),
                    "bitrate": int(stream.get("bit_rate", 0)) if stream.get("bit_rate") else None
                }
            elif codec_type == "audio":
                if "audio" not in result:
                    result["audio"] = []
                result["audio"].append({
                    "codec": stream.get("codec_long_name"),
                    "channels": stream.get("channels"),
                    "channel_layout": stream.get("channel_layout"),
                    "sample_rate": stream.get("sample_rate"),
                    "bitrate": int(stream.get("bit_rate", 0)) if stream.get("bit_rate") else None
                })
        
        return result

### === BATCH PROCESSING ===

def _access_dir_name(iso_path: Path) -> str:
    """Return the access directory name for a given ISO path"""
    name = iso_path.stem
    if name.upper().startswith("JPC_AV_"):
        return f"access_{name}"
    return f"access_JPC_AV_{name}"

# makeiso.py keeps a replaced preservation bundle, unchanged and with its
# canonical file names, in <filename>_overwritten-<YYYYmmddTHHMMSS>-<6 hex>/
ARCHIVE_FOLDER_RE = re.compile(r".+_overwritten-\d{8}T\d{6}-[0-9a-f]{6}")

def _in_archive_folder(iso_path: Path, iso_dir: Path) -> bool:
    """True when an ISO sits inside a makeiso.py archive folder below the
    batch root. Those are superseded masters with the current master's file
    name: converting them would write access copies inside the archive
    folder, or, with -o, collide with the current master's access folder."""
    return any(ARCHIVE_FOLDER_RE.fullmatch(part)
               for part in iso_path.relative_to(iso_dir).parts[:-1])

def process_batch(iso_dir: Path, output_dir: Path, operator: str, 
                  dry_run: bool = False, **encoding_kwargs) -> Dict[str, Any]:
    """Process multiple ISOs in a directory"""
    
    # Find all ISO files (case-insensitive, avoids duplicates on macOS),
    # leaving out superseded masters in makeiso.py archive folders
    found = sorted(p for p in iso_dir.rglob("*") if p.suffix.lower() == ".iso")
    archived = [p for p in found if _in_archive_folder(p, iso_dir)]
    iso_files = [p for p in found if not _in_archive_folder(p, iso_dir)]
    if archived:
        log(colorize("yellow", f"Skipping {len(archived)} superseded ISO(s) in makeiso.py "
                               f"archive folders (*_overwritten-*/; convert one with -i if needed):"))
        for p in archived:
            log(colorize("yellow", f"  {p.relative_to(iso_dir)}"))
    
    if not iso_files:
        log(colorize("yellow", f"No ISO files found in: {iso_dir}"))
        return {"processed": 0, "success": 0, "skipped": 0, "unverified": 0, "failed": 0}
    
    log_divider("Batch Processing")
    log(colorize("cyan", f"Found {len(iso_files)} ISO files"))
    
    results = {
        "processed": 0,
        "success": 0,
        "would_convert": 0,  # Dry-run VIDEO_TS discs
        "skipped": 0,        # Non-VIDEO_TS discs
        "unverified": 0,     # Nothing failed, but nothing promoted either
        "failed": 0,
        "details": []
    }
    
    for i, iso_path in enumerate(iso_files, 1):
        log_divider(f"Processing {i}/{len(iso_files)}: {iso_path.name}")
        
        # Create config for this ISO — access folder next to source ISO by default,
        # or under output_dir if explicitly provided
        base = output_dir if output_dir != iso_dir else iso_path.parent
        config = ConversionConfig(
            iso_path=iso_path,
            output_dir=base / _access_dir_name(iso_path),
            operator=operator,
            dry_run=dry_run,
            **encoding_kwargs
        )
        
        # Run conversion
        converter = ISOToMP4Converter(config)
        result = converter.convert()
        
        results["processed"] += 1
        
        # Failure outranks skipped: a run can end up failed-while-skip-shaped
        # (e.g. its skip record could not be written) and must count as failed.
        # An unverified run is never also failed (failure outranks it).
        if result.unverified:
            results["unverified"] += 1
        elif not result.success:
            results["failed"] += 1
        elif result.skipped:
            # Differentiate dry-run skips (VIDEO_TS that would convert) from actual skips
            if result.skip_reason == "Dry run mode":
                results["would_convert"] += 1
            else:
                results["skipped"] += 1
        else:
            results["success"] += 1
        
        results["details"].append({
            "iso": iso_path.name,
            "success": result.success,
            "skipped": result.skipped,
            "aborted": result.aborted,
            "unverified": result.unverified,
            "error": result.error_message,
            "skip_reason": result.skip_reason
        })

        if result.interrupted:
            log(colorize("red", "Batch interrupted by operator — stopping."))
            break
    
    # Summary
    log_divider("Batch Summary")
    log(colorize("cyan", f"Total processed: {results['processed']}"))
    if dry_run:
        log(colorize("green", f"Would convert: {results['would_convert']}"))
    else:
        log(colorize("green", f"Successfully converted: {results['success']}"))
    if results["skipped"] > 0:
        log(colorize("yellow", f"Skipped (no convertible content): {results['skipped']}"))
    if results["unverified"] > 0:
        log(colorize("yellow", f"Unverified (nothing promoted; review copies kept): "
                               f"{results['unverified']}"))
    log(colorize("red", f"Failed: {results['failed']}"))
    
    return results

### === ARGUMENT PARSING ===

def print_help():
    """Print custom colored help message"""
    print()
    
    help_text = f"""
{colorize('blue', 'MAKEISO-VIDEO - ISO TO MP4 ACCESS COPY UTILITY')}
{colorize('cyan', 'Converts DVD-Video ISO files to MP4 access copies')}

{colorize('yellow', 'USAGE:')}
  python3 makeiso-video.py -i <iso_file> [options]
  python3 makeiso-video.py --batch <directory> [options]

{colorize('yellow', 'REQUIRED (one of):')}
  {colorize('green', '-i, --iso PATH')}          Path to ISO file
  {colorize('green', '--batch PATH')}            Process all ISOs in directory

{colorize('yellow', 'OUTPUT OPTIONS:')}
  {colorize('green', '-o, --output PATH')}       Output directory (default: same directory as ISO)
                            Access files placed in: access_JPC_AV_<id>/
  {colorize('green', '--operator NAME')}         Operator name or initials

{colorize('yellow', 'ENCODING OPTIONS:')}
  {colorize('green', '--crf N')}                 Quality (0-51, script default: 18 = visually lossless)
  {colorize('green', '--preset NAME')}           Encoding speed (ultrafast/fast/medium/slow/veryslow, script default: medium)
  {colorize('green', '--audio-bitrate RATE')}    Audio bitrate (script default: 192k)

{colorize('yellow', 'OTHER OPTIONS:')}
  {colorize('green', '-h, --help')}              Show this help message
  {colorize('green', '-n, --dry-run')}           Analyze without converting

{colorize('yellow', 'EXAMPLES:')}
  {colorize('cyan', '# Convert single ISO (output to same directory)')}
  python3 makeiso-video.py -i /path/to/JPC_AV_00001.iso --operator JD
  {colorize('cyan', '# Creates: /path/to/access_JPC_AV_00001/JPC_AV_00001.mp4')}

  {colorize('cyan', '# Convert with custom output location')}
  python3 makeiso-video.py -i disc.iso -o /path/to/access_copies --operator JD

  {colorize('cyan', '# Batch convert directory')}
  python3 makeiso-video.py --batch /path/to/isos --operator JD

  {colorize('cyan', '# Higher quality, slower encoding')}
  python3 makeiso-video.py -i disc.iso --crf 16 --preset slow --operator JD

  {colorize('cyan', '# Preview without converting')}
  python3 makeiso-video.py -i disc.iso -n

{colorize('yellow', 'CRF QUALITY GUIDE:')}
  0  = Lossless (huge file)
  18 = Visually lossless (script default)
  23 = ffmpeg default (not this script's default)
  28 = Smaller file, some quality loss

{colorize('yellow', 'DURATION CHECK AND OUTCOMES:')}
  Each titleset's MP4 is checked against its expected duration (the IFO
  playback time for one simple straight-through layout, else complete VOB
  probes), with a tolerance of 5% but at least 1 second.
  • {colorize('green', 'passed:')}      within the tolerance
  • {colorize('yellow', 'unverified:')}  check not possible (an output probe failed, no usable
                 expected duration) or longer than expected: kept as
                 *_unverified-<token>.mp4.unverified — NOT a deliverable
  • {colorize('red', 'failed:')}      ffmpeg error, empty or video-less output, or shorter
                 than expected: kept as *_failed-<token>.mp4.partial
  MP4s get their final names only if EVERY titleset of the ISO passes;
  otherwise nothing is promoted, existing MP4s and records stay as they
  are, and passed titlesets are held as review copies too. A run that
  fails once promotion has begun (a rename stops partway, or the records
  cannot be written afterwards) keeps the MP4s it promoted: no rollback.
  Exit status: 0 converted, skipped or successful dry run; 1 any failure,
  unverified result or abort, or arguments without -i or --batch; 2 an
  option or value rejected by the argument parser.

{colorize('yellow', 'DISC TYPES HANDLED:')}
  • {colorize('green', 'DVD-Video (VIDEO_TS):')} Converted to MP4
  • {colorize('yellow', 'DVD-Audio (AUDIO_TS):')} Skipped (requires different tools)
  • {colorize('yellow', 'Data discs:')} Skipped (use file copy instead)
  • {colorize('red', 'Analysis errors:')} Failed, nothing encoded (VIDEO_TS unreadable or only partly listed)
  • {colorize('yellow', 'Batch mode:')} skips superseded masters in makeiso.py *_overwritten-* folders (use -i, with -o, for one)

{colorize('cyan', 'Part of the Johnson Publishing Company Archive (JPCA) digitization workflow.')}
"""
    print(help_text)

def print_usage():
    """Print brief usage message when no arguments provided"""
    print()
    print(colorize('blue', 'MAKEISO-VIDEO - ISO TO MP4 ACCESS COPY UTILITY'))
    print()
    print(colorize('yellow', 'Usage:'))
    print(f"  python3 makeiso-video.py -i <iso_file> [options]")
    print(f"  python3 makeiso-video.py --batch <directory> [options]")
    print()
    print(colorize('yellow', 'Required (one of):'))
    print(f"  {colorize('green', '-i, --iso PATH')}    Path to ISO file")
    print(f"  {colorize('green', '--batch PATH')}      Process all ISOs in directory")
    print()
    print(f"Run {colorize('cyan', 'python3 makeiso-video.py --help')} for full options.")
    print()

def parse_args() -> argparse.Namespace:
    """Parse command line arguments"""
    if '--help' in sys.argv or '-h' in sys.argv:
        print_help()
        sys.exit(0)
    
    # Show usage if no arguments provided
    if len(sys.argv) == 1:
        print_usage()
        sys.exit(0)
    
    parser = argparse.ArgumentParser(
        prog='makeiso-video.py',
        description='ISO to MP4 Access Copy Utility',
        add_help=False
    )
    
    # Input options
    parser.add_argument('-i', '--iso', type=str, metavar='PATH',
                        help='Path to ISO file')
    parser.add_argument('--batch', type=str, metavar='PATH',
                        help='Process all ISOs in directory')
    parser.add_argument('-o', '--output', type=str, metavar='PATH',
                        help='Output directory')
    parser.add_argument('--operator', type=str, metavar='NAME',
                        help='Operator name or initials')
    
    # Encoding options
    parser.add_argument('--crf', type=int, default=18,
                        help='CRF quality (0-51, default: 18)')
    parser.add_argument('--preset', type=str, default='medium',
                        choices=['ultrafast', 'superfast', 'veryfast', 'faster', 
                                'fast', 'medium', 'slow', 'slower', 'veryslow'],
                        help='Encoding preset (default: medium)')
    parser.add_argument('--audio-bitrate', type=str, default='192k',
                        help='Audio bitrate (default: 192k)')
    
    # Other options
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='Analyze without converting')
    
    return parser.parse_args()

### === MAIN FUNCTION ===

def main():
    """Main entry point"""
    args = parse_args()
    
    log("")
    log_divider("ISO to MP4 Access Copy Utility")
    
    # Validate inputs
    if not args.iso and not args.batch:
        log(colorize("red", "Error: Must specify either --iso or --batch"))
        log(colorize("cyan", "Use --help for usage information"))
        sys.exit(1)
    
    # Get operator
    operator = args.operator
    if not operator:
        operator = get_input(colorize("cyan", "Enter operator name or initials: "))
    
    # Batch mode
    if args.batch:
        batch_dir = Path(args.batch).expanduser()
        if not batch_dir.exists():
            log(colorize("red", f"Error: Batch directory not found: {batch_dir}"))
            sys.exit(1)
        
        # Default output is same directory as batch input
        output_base = Path(args.output).expanduser() if args.output else batch_dir
        
        results = process_batch(
            iso_dir=batch_dir,
            output_dir=output_base,
            operator=operator,
            dry_run=args.dry_run,
            crf=args.crf,
            preset=args.preset,
            audio_bitrate=args.audio_bitrate
        )
        
        if results["failed"] > 0 or results.get("unverified", 0) > 0:
            sys.exit(1)
        sys.exit(0)
    
    # Single file mode
    iso_path = Path(args.iso).expanduser()

    # If a directory was passed, look for a single ISO inside it
    if iso_path.is_dir():
        iso_files = sorted(p for p in iso_path.iterdir() if p.is_file() and p.suffix.lower() == ".iso")
        if len(iso_files) == 1:
            log(colorize("yellow", f"Directory provided — using ISO found inside: {iso_files[0].name}"))
            iso_path = iso_files[0]
        elif len(iso_files) == 0:
            log(colorize("red", f"Error: Directory provided but no .iso file found inside: {iso_path}"))
            sys.exit(1)
        else:
            iso_list = ", ".join(f.name for f in iso_files)
            log(colorize("red", f"Error: Multiple .iso files found — please specify one directly: {iso_list}"))
            sys.exit(1)

    if not iso_path.exists():
        log(colorize("red", f"Error: ISO file not found: {iso_path}"))
        sys.exit(1)
    
    # Determine output directory — default: access folder next to the ISO
    output_base = Path(args.output).expanduser() if args.output else iso_path.parent
    output_dir = output_base / _access_dir_name(iso_path)
    
    # Create config
    config = ConversionConfig(
        iso_path=iso_path,
        output_dir=output_dir,
        operator=operator,
        dry_run=args.dry_run,
        crf=args.crf,
        preset=args.preset,
        audio_bitrate=args.audio_bitrate
    )
    
    # Run conversion
    converter = ISOToMP4Converter(config)
    result = converter.convert()
    
    # Exit with appropriate code
    if result.success:
        if result.skipped:
            log(colorize("yellow", f"Skipped: {result.skip_reason}"))
            sys.exit(0)
        else:
            log(colorize("green", "Conversion completed successfully!"))
            sys.exit(0)
    elif result.unverified:
        log(colorize("yellow", "Unverified: nothing promoted; review copies kept "
                               f"(not deliverables): {result.unverified_reason}"))
        sys.exit(1)
    elif result.aborted:
        log(colorize("yellow", f"Aborted: {result.error_message}"))
        sys.exit(1)
    else:
        log(colorize("red", f"Conversion failed: {result.error_message}"))
        sys.exit(1)

### === SCRIPT ENTRYPOINT ===

if __name__ == "__main__":
    main()