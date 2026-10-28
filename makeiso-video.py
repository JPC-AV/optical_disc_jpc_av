#!/usr/bin/env python3

"""
makeiso-video.py - ISO to MP4 Access Copy Utility
Extracts VIDEO_TS content from ISO files and converts to MP4 access copies.
Part of the Johnson Publishing Company Archive (JPCA) optical disc digitization workflow.

A collaboration between the Getty Research Institute and the 
Smithsonian National Museum of African American History and Culture.
"""

import contextlib
import errno
import os
import shlex
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
import signal
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
    def manifest_path(self) -> Path:
        return self.output_dir / f"{self.iso_name}_access_manifest.json"

    # Fixed names that earlier versions gave every skip and dry-run record,
    # each run writing over the last. Never written now: a success record
    # names any it finds, so they are not taken for its outcome
    @property
    def legacy_skip_manifest_path(self) -> Path:
        return self.output_dir / f"{self.iso_name}_skip_manifest.json"

    @property
    def legacy_dry_run_log_path(self) -> Path:
        return self.output_dir / f"{self.iso_name}.mp4.dryrun.log.txt"

@dataclass
class DiscContentAnalysis:
    """Analysis of ISO content structure"""
    has_video_ts: bool = False
    has_audio_ts: bool = False  # For DVD-Audio discs
    vob_files: List[Path] = field(default_factory=list)
    ifo_files: List[Path] = field(default_factory=list)
    bup_files: List[Path] = field(default_factory=list)
    total_vob_size: int = 0
    vob_sizes: Dict[Path, int] = field(default_factory=dict)  # from the inventory's own check
    # "dvd_video", "dvd_audio", "mixed", "video_ts_without_vobs", "data",
    # "empty", "error", "unknown"
    disc_type: str = "unknown"
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
    # A check of where the output is failed with an error other than "no
    # such file": its current location is unknown (not absent); see
    # retention_error for the reason and the candidate paths
    location_unverified: bool = False
    # Its promotion was interrupted and the final name could not be checked
    # afterwards: that final MP4 may have been replaced by this run
    final_name_unverified: bool = False

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
    # A killed ffmpeg could not be confirmed stopped: this ISO's remaining
    # titlesets and the rest of a batch are not run, and its mount is left
    # attached. Not a user interruption. encoder_cleanup holds the recovery
    # details (pid, source ISO, possible output, mount points, devices, reason)
    cleanup_incomplete: bool = False
    encoder_cleanup: Optional[Dict[str, Any]] = None

    @property
    def canonical_files_changed(self) -> List[Path]:
        """Final-name MP4s this run actually created or replaced"""
        return [t.output_path for t in self.titles if t.promoted_this_run]

    @property
    def canonical_files_uncertain(self) -> List[Path]:
        """Final-name MP4s this run may have replaced: an interrupted
        promotion after which the final name could not be checked"""
        return [t.output_path for t in self.titles
                if t.final_name_unverified and not t.promoted_this_run]

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
    """Get user input (the operator name, asked before anything starts).
    Ctrl-C cancels with exit 130, as a Ctrl-C that stops any run does;
    end of input (no answer) cancels with exit 1. Nothing is started or
    recorded either way."""
    try:
        return input(prompt).strip()
    except KeyboardInterrupt:
        log(colorize("red", "\nCancelled by user (Ctrl-C): nothing was started."))
        sys.exit(130)
    except EOFError:
        log(colorize("red", "\nNo answer (end of input): cancelled."))
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
    falling back to the first volume. A volume whose VIDEO_TS cannot be
    checked is never passed over: selection stops and selection_error
    says why (the run then fails with an analysis error)."""

    def __init__(self, iso_path: Path):
        self.iso_path = iso_path
        self.mount_point: Optional[Path] = None
        self.mount_points: List[Path] = []
        self.devices: List[str] = []
        self.selection_error: Optional[str] = None
        # Detach failures recorded here so the caller can surface them into the
        # conversion record (the context manager has no access to it directly).
        self.detach_errors: List[str] = []
        # Set (to the reason) when an encoder reading this image could not be
        # confirmed stopped: the image is then left attached, never detached
        self.keep_attached: Optional[str] = None
        # A Ctrl-C stopped the detach: the image may still be attached
        self.detach_interrupted = False

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
                    has_video_ts, problem = self._has_video_ts(candidate)
                    if problem:
                        self.selection_error = (f"could not check volume {candidate} for "
                                                f"VIDEO_TS: {problem}")
                        log(colorize("red", self.selection_error))
                        break
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

    @staticmethod
    def _has_video_ts(volume: Path) -> Tuple[bool, Optional[str]]:
        """(True, None) when the volume holds a VIDEO_TS folder, (False, None)
        when it does not, or (False, reason) when that could not be checked:
        only "no such file" counts as absent, and a VIDEO_TS that was listed
        but is then not found cannot be trusted either"""
        try:
            listed = [item for item in volume.iterdir() if item.name.upper() == "VIDEO_TS"]
            names_listed = True
        except OSError:
            # Unlistable root (e.g. mode --x on recorder-authored UDF):
            # probe the standard path by name instead
            listed, names_listed = [volume / "VIDEO_TS"], False
        for item in listed:
            st, problem = inspect_path(item, follow_symlinks=True)
            if problem:
                return False, f"{item}: {problem}"
            if st is None and names_listed:
                return False, f"{item} was listed but then not found"
            if st is not None and stat.S_ISDIR(st.st_mode):
                return True, None
        return False, None

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Unmount the ISO (all volumes for multi-session images).

        Detach failures are recorded in self.detach_errors so the caller can
        surface them: a leaked attach holds a /dev/diskN that can block later
        mounts across a batch.

        With keep_attached set, nothing is detached (no forced detach, no
        device fallback, no leak sweep): an encoder that may still be reading
        this image keeps its source, and the operator resolves it.

        Ctrl-C is not held over hdiutil (it has no timeout): one that arrives
        while detaching stops the detach and is recorded (detach_interrupted,
        and a detach_errors note that the image may still be attached), not
        raised. A mount point that cannot be checked is still detached."""
        if self.keep_attached:
            log(colorize("red", f"Warning: leaving {self.iso_path.name} attached at "
                                f"{', '.join(str(p) for p in self.mount_points) or 'no mount point'}"
                                f" ({', '.join(self.devices) or 'no device'}): {self.keep_attached}"))
            return
        try:
            detached_any = False
            for mount_point in self.mount_points:
                present, problem = inspect_path(mount_point)
                if present is None and problem is None:
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
        except KeyboardInterrupt:
            self.detach_interrupted = True
            msg = (f"unmount of {self.iso_path.name}: interrupted by operator (Ctrl-C); it may "
                   f"still be attached at "
                   f"{', '.join(str(p) for p in self.mount_points) or 'no mount point'}"
                   f" ({', '.join(self.devices) or 'no device'}): check with hdiutil info")
            self.detach_errors.append(msg)
            log(colorize("red", f"Warning: {msg}"))

### === CONTENT ANALYSIS ===

class InventoryError(Exception):
    """An entry that decides the disc type could not be checked, or was
    listed and then not found: the inventory cannot be trusted"""


def _inventory_check(path: Path, listed: bool) -> Optional[os.stat_result]:
    """What path is, following links; None when it does not exist (only
    "no such file" counts as that). Raises InventoryError when the check
    fails, or when a listed entry is then not found."""
    st, problem = inspect_path(path, follow_symlinks=True)
    if problem:
        raise InventoryError(f"could not inspect {path}: {problem}")
    if st is None and listed:
        raise InventoryError(f"{path} was listed but then not found")
    return st


def analyze_iso_content(mount_point: Path) -> DiscContentAnalysis:
    """Analyze the content structure of a mounted ISO.

    Folders are listed with Path.iterdir. Only what decides the disc type
    is checked, once each and following links: a root entry named VIDEO_TS
    or AUDIO_TS (any case), and each VOB, IFO and BUP file in VIDEO_TS.
    Only "no such file" counts as absent: any other failed check, or a
    listed entry that is then not found, is an analysis error, never a
    missing entry. Other root entries are not checked."""
    analysis = DiscContentAnalysis()
    analysis.mount_point = mount_point
    
    try:
        # Check for VIDEO_TS folder (case-insensitive search)
        video_ts_path = None
        audio_ts_path = None
        standard_names = ("VIDEO_TS", "AUDIO_TS")

        root_listable = True
        try:
            entries = [p for p in mount_point.iterdir() if p.name.upper() in standard_names]
        except PermissionError:
            # Some recorder-authored UDF discs give the volume root search
            # permission but no read permission (mode --x), so it cannot be
            # listed — but its well-known children can still be reached by
            # name, which only requires search permission.
            root_listable = False
            log(colorize("yellow", "Volume root is not listable (no read permission); "
                                   "probing standard VIDEO_TS/AUDIO_TS paths directly"))
            entries = [mount_point / name for name in standard_names]

        for item in entries:
            st = _inventory_check(item, listed=root_listable)
            if st is None or not stat.S_ISDIR(st.st_mode):
                continue
            if item.name.upper() == "VIDEO_TS":
                video_ts_path = item
                analysis.has_video_ts = True
            else:
                audio_ts_path = item
                analysis.has_audio_ts = True
        if not root_listable and not (analysis.has_video_ts or analysis.has_audio_ts):
            analysis.error = ("Volume root is unreadable and no VIDEO_TS or AUDIO_TS "
                              "directory was found at the standard paths")
            analysis.disc_type = "error"
            return analysis
        
        # Analyze VIDEO_TS content (regular files only, as before)
        if video_ts_path:
            for file in video_ts_path.iterdir():
                suffix = file.suffix.upper()
                if suffix not in (".VOB", ".IFO", ".BUP"):
                    continue
                st = _inventory_check(file, listed=True)
                if not stat.S_ISREG(st.st_mode):
                    continue
                if suffix == ".VOB":
                    analysis.vob_files.append(file)
                    analysis.vob_sizes[file] = st.st_size
                    analysis.total_vob_size += st.st_size
                elif suffix == ".IFO":
                    analysis.ifo_files.append(file)
                else:
                    analysis.bup_files.append(file)
        
        # Determine disc type
        if analysis.has_video_ts and analysis.vob_files:
            if analysis.has_audio_ts:
                analysis.disc_type = "mixed"
            else:
                analysis.disc_type = "dvd_video"
        elif analysis.has_audio_ts:
            analysis.disc_type = "dvd_audio"
        elif analysis.has_video_ts:
            # IFO/BUP files only, or an empty VIDEO_TS: never "no VIDEO_TS"
            # (an unlistable root ends in one of these branches: it has
            # failed above when neither standard folder was found)
            analysis.disc_type = "video_ts_without_vobs"
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

def plan_titlesets(vob_files: List[Path], output_dir: Path, iso_name: str,
                   sizes: Optional[Dict[Path, int]] = None
                   ) -> Tuple[List[TitlesetPlan], List[Path], List[Path]]:
    """Group content VOBs by titleset (VTS_NN) and assign output paths.

    Returns (plans, menu_vobs, unrecognized_vobs). The lowest-numbered
    content titleset is "main" and keeps the unsuffixed output name; others
    get _titleNN suffixes matching their VTS number. Titleset-aware only —
    a titleset may still contain multiple programs/PGCs. Sizes come from
    the inventory (sizes) when given; a VOB is never left out of them.
    """
    sizes = sizes or {}
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
            total_size=sum(sizes[v] if v in sizes else v.stat().st_size for v in vobs),
            is_main=is_main,
        ))
    return plans, menu_vobs, unrecognized

def vob_number_gaps(plans: List[TitlesetPlan]) -> List[str]:
    """Advisory warnings for titlesets whose content VOBs are not numbered
    1..N. A gap is one sign that a VOB is missing from the image; no gap
    proves nothing (a missing last VOB leaves none). Only names are
    compared: this is not a DVD structure check, and it changes no outcome."""
    gaps = []
    for plan in plans:
        if plan.vts_number == 0:
            continue  # unrecognized names: there is no numbering to check
        numbers = sorted({int(CONTENT_VOB_RE.match(v.name).group(2)) for v in plan.content_vobs})
        missing = [n for n in range(1, numbers[-1] + 1) if n not in numbers]
        if missing:
            gaps.append(f"VTS_{plan.vts_number:02d}: content VOB numbering has a gap "
                        f"(found {', '.join(map(str, numbers))}; "
                        f"missing {', '.join(map(str, missing))})")
    return gaps

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
    VOB fallback. A VOB whose size cannot be read raises (the titleset then
    fails); it is never left out of the bitrate check."""
    total_bytes = sum(v.stat().st_size for v in vobs)

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

# Seconds a killed ffmpeg gets to be confirmed exited (wait() returning)
# before it is reported as possibly still running. A bound on that wait,
# not on every operation of the shutdown.
ENCODER_STOP_TIMEOUT = 10

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

### === ENCODER LIFETIME ===

class DeferredInterrupt:
    """Hold Ctrl-C (SIGINT) for one short section of local work: starting
    ffmpeg (until its handle is assigned), stopping it after a failure,
    reserving a kept name, the promotion renames, and writing and renaming
    the run records. Never over a native tool (hdiutil, ffprobe, ffmpeg
    -version) or a whole conversion. A Ctrl-C that arrives meanwhile only
    sets .received; the caller acts on it afterwards. The previous handler
    is always restored. Holds never nest (an inner one would keep a Ctrl-C
    from the outer). Off the main thread (where handlers cannot be set) the
    section simply runs unprotected. A copy of makeiso.py's helper: the
    scripts are standalone."""

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


class EncoderNotStopped(Exception):
    """A killed ffmpeg could not be confirmed stopped: nothing more may run"""


def stop_encoder(process) -> Optional[str]:
    """Kill an ffmpeg this run started and wait (bounded) for it to exit.

    Returns None once it has exited and been reaped - wait() returning is
    the only proof - else why it may still be running. Never raises an
    Exception."""
    problems = []
    try:
        process.kill()               # SIGKILL; a no-op once it has exited
    except Exception as e:
        problems.append(f"kill failed: {e}")
    try:
        process.wait(timeout=ENCODER_STOP_TIMEOUT)
        return None
    except subprocess.TimeoutExpired:
        problems.append(f"still running {ENCODER_STOP_TIMEOUT} s after it was killed")
    except Exception as e:
        problems.append(f"could not wait for it to exit: {e}")
    return "; ".join(problems)


def inspect_path(path: Path, follow_symlinks: bool = False
                 ) -> Tuple[Optional[os.stat_result], Optional[str]]:
    """(lstat result, None) when path exists, (None, None) when it does not
    (FileNotFoundError), or (None, reason) when the check itself failed - an
    unknown location, never taken as absence. With follow_symlinks, the
    same for what a symlink points to (os.stat): a dangling one is absent."""
    try:
        return (os.stat(path) if follow_symlinks else os.lstat(path)), None
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, str(e)

@dataclass
class RecordFile:
    """One run record this run opened for writing (see _record_file)"""
    final: Path                      # the record's own name
    path: Path                       # where it is written: a temp for the canonical pair
    staged: bool                     # canonical: renamed to final once both are complete
    identity: Optional[Tuple[int, int]] = None  # (st_dev, st_ino) of the file created
    complete: bool = False           # written and closed without an error
    committed: bool = False          # staged, and renamed into place

### === MAIN CONVERTER CLASS ===

# Where a Ctrl-C landed, for the record (see _interrupted_result)
ATTACH_PHASE = "while the ISO was being attached; it may be attached: check with hdiutil info"
DETACH_PHASE = ("while the ISO was being detached; it may still be attached: "
                "check with hdiutil info")
# Why an ISO was aborted when the overwrite prompt got no answer (end of input)
NO_ANSWER_ABORT = "No answer to the overwrite prompt (end of input); nothing was overwritten"

class ISOToMP4Converter:
    """Main class for ISO to MP4 conversion operations"""
    
    def __init__(self, config: ConversionConfig):
        self.config = config
        self.start_time: Optional[datetime.datetime] = None
        self.run: Optional[RunContext] = None
        self._token_suffix: Optional[str] = None
        self._mount: Optional[ISOMount] = None  # this run's own source mount
        # What a Ctrl-C that escapes the conversion is settled from: the
        # run's result once it exists, whether its outcome is decided, where
        # the run was, and what it had found (set where each is assigned)
        self._result: Optional[ConversionResult] = None
        self._settled = False
        self._phase = "before the conversion started"
        self._entered = False  # convert() reached its boundary: this ISO has started
        self._detach_merged = False
        self._iso_size: Optional[int] = None
        self._ffmpeg_checked = False
        self._ffmpeg_version: Optional[str] = None
        # Why the overwrite prompt did not go ahead: (error, interrupted)
        self._overwrite_declined: Tuple[str, bool] = ("Aborted by operator to avoid overwrite", False)
        # The records step (_write_run_records): what it gathered before any
        # record file was opened (no native tool runs while one is open),
        # the record files it opened, how far it got, what stopped it, and
        # what it has to show
        self._records_kind: Optional[str] = None
        self._record_details: Optional[Dict[str, Any]] = None
        self._record_files: List[RecordFile] = []
        self._records_stage = "pending"  # pending | committed | recovered | finished
        self._record_error: Optional[BaseException] = None
        self._records_written: List[Path] = []
        self._record_messages: List[Tuple[str, str]] = []

    def _run_token(self, kind: str = "failed") -> str:
        """One timestamp-and-random suffix per run names every non-canonical
        artifact — partials, review copies, failure/unverified/skip/dry-run
        records, and the temporary files the canonical records are written
        to — with a kind prefix (failed | unverified | aborted | skipped |
        dryrun | writing), so one attempt's evidence groups together and
        retries never overwrite it"""
        if self._token_suffix is None:
            self._token_suffix = (f"{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}"
                                  f"-{uuid4().hex[:6]}")
        return f"{kind}-{self._token_suffix}"

    def convert(self) -> ConversionResult:
        """Main entry point for conversion: the one place a run ends.

        A Ctrl-C that escapes the conversion is settled from what the run
        had recorded (_interrupted_result) instead of ending it. Then every
        processed ISO gets a log and a manifest of some kind, unless the
        record paths themselves failed the symlink guard (_finish_records).
        Only a further Ctrl-C while an interruption is being settled can
        leave here; the batch and main() stop on it."""
        self.start_time = datetime.datetime.now()
        self.run = RunContext(
            run_id=f"{self.start_time.strftime('%Y%m%dT%H%M%S')}_{self.config.operator}_{self.config.iso_name}",
            run_uuid=str(uuid4()),
            start_time=self.start_time,
        )

        try:
            self._entered = True
            result = self._convert_inner()
        except KeyboardInterrupt:
            result = self._interrupted_result()
        self._finish_records(result)
        return result

    def _interrupted_result(self) -> ConversionResult:
        """The run's result after a Ctrl-C escaped the conversion, from the
        state recorded as it ran. A run whose outcome was already decided
        keeps it, with the interruption added as a warning (a failure's
        cause is never replaced). One interrupted before that is settled as
        an interrupted encode is: nothing is promoted, and checked outputs
        are kept as review copies. One interrupted before it had a result
        fails, naming where the Ctrl-C landed. The batch stops after it."""
        note = f"Interrupted by operator (Ctrl-C) {self._phase}"
        result = self._result
        if result is None:
            result = ConversionResult(
                success=False,
                iso_path=self.config.iso_path,
                source_size=self._iso_size or 0,
                error_message=note,
                interrupted=True
            )
        else:
            result.interrupted = True
            if not self._settled:
                self._finish_titlesets(result)  # promotes nothing once interrupted
                self._settle_outcome(result)
            elif note not in result.warnings:
                result.warnings.append(note)
        if self._mount is not None:
            self._merge_detach_errors(result, self._mount)
        self._result, self._settled = result, True
        if self.run:
            self.run.mark("interrupted")
        return result

    def _merge_detach_errors(self, result: ConversionResult, mount):
        """Surface any unmount failures into the record, once. A volume that
        fails to detach (e.g. a handle still open) leaks an attached
        /dev/diskN that can block later mounts in a batch, so it must be
        visible, not just logged. A detach cut short by Ctrl-C also stops
        the batch."""
        if self._detach_merged:
            return
        self._detach_merged = True
        result.warnings.extend(mount.detach_errors)
        if getattr(mount, "detach_interrupted", False):  # test mounts may lack it
            result.interrupted = True

    def _check_output_paths(self, paths: List[Path]
                            ) -> Tuple[Optional[str], Optional[str], List[Path]]:
        """Check each planned output path once (lstat): returns (symlink
        refusal, inspection refusal, the paths that exist). A pre-existing
        symlink is refused (writing through one could clobber an arbitrary
        file elsewhere), and so is a path that cannot be checked: only "no
        such file" counts as absent, so an existing output is never
        replaced without the overwrite prompt."""
        linked, unchecked, existing = [], [], []
        for p in paths:
            st, problem = inspect_path(p)
            if problem:
                unchecked.append(f"could not check planned output {p.name}: {problem}")
            elif st is not None:
                existing.append(p)
                if stat.S_ISLNK(st.st_mode):
                    linked.append(p.name)
        symlink_error = inspection_error = None
        if linked:
            symlink_error = f"Symlink at planned output path(s): {', '.join(linked)}"
            log(colorize("red", f"Refusing to continue: {symlink_error}"))
        if unchecked:
            inspection_error = "Inspection error: " + "; ".join(unchecked)
            log(colorize("red", f"Refusing to continue: {inspection_error}"))
        return symlink_error, inspection_error, existing

    def _convert_inner(self) -> ConversionResult:
        """Conversion flow; every return path gets records via convert()"""
        # Check the ISO once (following a symlink): only "no such file" is
        # "not found"; a check that fails, or anything but a regular file,
        # stops the run before anything is attached. The same check gives
        # the size every record (dependency failures below included) carries
        iso_st, problem = inspect_path(self.config.iso_path, follow_symlinks=True)
        if problem or iso_st is None or not stat.S_ISREG(iso_st.st_mode):
            if problem:
                error = f"Inspection error: could not check ISO {self.config.iso_path}: {problem}"
            elif iso_st is None:
                error = f"ISO file not found: {self.config.iso_path}"
            else:
                error = (f"Not a regular file (a folder, FIFO, device or socket), so not "
                         f"attached: {self.config.iso_path}")
            return ConversionResult(
                success=False,
                iso_path=self.config.iso_path,
                error_message=error
            )
        iso_size = iso_st.st_size
        self._iso_size = iso_size

        # Dependencies: ffmpeg (encode) AND ffprobe (analysis/verification)
        # are both required before any disc activity. The version found here
        # is the one the records give (no native tool runs while they are
        # written)
        self._phase = "while ffmpeg and ffprobe were being checked"
        ffmpeg_available, ffmpeg_version = check_ffmpeg()
        self._ffmpeg_checked, self._ffmpeg_version = True, ffmpeg_version
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
        self._phase = "before the ISO was attached"

        # Output path guard, phase 1: statically-known record/output paths.
        # (Per-title MP4 paths are guarded after titleset planning; failure,
        # skip and dry-run records use unpredictable tokened names and need
        # no guard, so a path that cannot be checked still gets them.)
        static_paths = [self.config.mp4_path, self.config.log_path,
                        self.config.manifest_path]
        symlink_error, inspection_error, _ = self._check_output_paths(static_paths)
        if symlink_error or inspection_error:
            return ConversionResult(
                success=False,
                iso_path=self.config.iso_path,
                source_size=iso_size,
                error_message="; ".join(m for m in (symlink_error, inspection_error) if m),
                # A symlink makes the record paths themselves suspect
                records_unsafe=bool(symlink_error)
            )

        log(colorize("cyan", f"ISO size: {format_bytes(iso_size)}"))

        # Mount and analyze ISO
        log_divider("Mounting ISO")
        result = None
        self.run.mark("mount")

        mount = ISOMount(self.config.iso_path)
        self._mount = mount
        self._phase = ATTACH_PHASE
        with mount as mount_point:
            self._phase = "during the disc analysis"
            # A volume whose VIDEO_TS could not be checked (test mounts may
            # lack the attribute): the right volume is unknown, so nothing
            # is analysed or encoded
            selection_error = getattr(mount, "selection_error", None)
            if selection_error:
                error = f"Analysis error: {selection_error}"
                log(colorize("red", f"{error} — nothing planned or encoded"))
                result = ConversionResult(
                    success=False,
                    iso_path=self.config.iso_path,
                    source_size=iso_size,
                    error_message=error
                )
            elif not mount_point:
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
                # (e.g. the VIDEO_TS listing failed partway, or a VOB, IFO
                # or BUP file could not be checked), so it is never logged,
                # planned or encoded as if it were complete
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
                    if analysis.has_video_ts and not analysis.vob_files:
                        # A VOB-less VIDEO_TS (with or without AUDIO_TS): say
                        # what to check, not only why nothing was converted
                        log(colorize("yellow", self._get_skip_recommendation(analysis, skip_reason)))

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
                        analysis.vob_files, self.config.output_dir, self.config.iso_name,
                        analysis.vob_sizes)
                    self._log_titleset_plan(plans, menu_vobs, unrecognized)

                    # Output path guard, phase 2: per-title output paths are
                    # only knowable now that titlesets are planned. The same
                    # check finds the outputs the overwrite prompt is about
                    output_symlink_error, output_inspection_error, existing = \
                        self._check_output_paths([p.output_path for p in plans])
                    existing_outputs = [] if self.config.dry_run else existing

                    if output_symlink_error or output_inspection_error:
                        result = ConversionResult(
                            success=False,
                            iso_path=self.config.iso_path,
                            source_size=iso_size,
                            disc_analysis=analysis,
                            error_message="; ".join(m for m in (output_symlink_error,
                                                                output_inspection_error) if m)
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
                        error, interrupted = self._overwrite_declined
                        result = ConversionResult(
                            success=False,
                            iso_path=self.config.iso_path,
                            source_size=iso_size,
                            disc_analysis=analysis,
                            aborted=True,
                            interrupted=interrupted,
                            error_message=error
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
                                    for p in plans],
                            warnings=vob_number_gaps(plans)
                        )
                    else:
                        # Create output directory (only for actual conversion)
                        self.config.output_dir.mkdir(parents=True, exist_ok=True)
                        result = self._perform_conversion(analysis, iso_size, plans, unrecognized)
            if result is not self._result:
                # Every other outcome is decided when it is built
                self._result, self._settled = result, True
            self._phase = DETACH_PHASE
        self._phase = "after the ISO was detached"

        self._merge_detach_errors(result, mount)
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
            
            # Log VOB files to file only, with the sizes the inventory found
            for vob in analysis.vob_files:
                size = analysis.vob_sizes.get(vob)
                log(colorize("cyan", f"  VOB: {vob.name} "
                                     f"({'size not recorded' if size is None else format_bytes(size)})"))
        
        if analysis.ifo_files:
            log(colorize("cyan", f"IFO files found: {len(analysis.ifo_files)}"))
        
        if analysis.error:
            log(colorize("red", f"Analysis error: {analysis.error}"))
    
    def _get_skip_reason(self, analysis: DiscContentAnalysis) -> str:
        """Get human-readable skip reason"""
        if analysis.disc_type == "data":
            return "Data disc (no VIDEO_TS folder)"
        elif analysis.disc_type == "dvd_audio":
            if analysis.has_video_ts:
                return (f"DVD-Audio disc (AUDIO_TS; its VIDEO_TS has no VOB files: "
                        f"{len(analysis.ifo_files)} IFO, {len(analysis.bup_files)} BUP)")
            return "DVD-Audio disc (AUDIO_TS only, no VIDEO_TS)"
        elif analysis.disc_type == "video_ts_without_vobs":
            return (f"No VOB files found in VIDEO_TS ({len(analysis.ifo_files)} IFO, "
                    f"{len(analysis.bup_files)} BUP); nothing to convert")
        elif analysis.disc_type == "empty":
            return "Empty or unreadable disc"
        elif analysis.disc_type == "error":
            return f"Analysis error: {analysis.error}"
        else:
            return "Unknown reason"
    
    def _confirm_overwrite(self, existing_paths: List[Path]) -> bool:
        """Confirm overwrite of pre-existing planned outputs: only "y" goes
        ahead. No answer (end of input, as in an unattended run) counts as
        "n"; Ctrl-C cancels, and also stops a batch. Why it did not go ahead
        is left in self._overwrite_declined (error, interrupted)."""
        names = ", ".join(p.name for p in existing_paths)
        try:
            response = input(colorize("yellow",
                f"Output exists ({names}). Overwrite? (y/n): ")).strip()
        except EOFError:
            self._overwrite_declined = (NO_ANSWER_ABORT, False)
            log(colorize("yellow", "\nNo answer (end of input): not overwriting."))
            return False
        except KeyboardInterrupt:
            self._overwrite_declined = ("Cancelled at the overwrite prompt (Ctrl-C); "
                                        "nothing was overwritten", True)
            log(colorize("red", "\nCancelled by user."))
            return False
        if response.lower() == 'y':
            return True
        self._overwrite_declined = ("Aborted by operator to avoid overwrite", False)
        return False

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
        for gap in vob_number_gaps(plans):
            log(colorize("yellow", f"Warning: {gap} (advisory; the outcome is unchanged)"))
    
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
            # Every planned titleset is recorded, attempted or not
            titles=[TitleOutput(vts_number=p.vts_number, output_path=p.output_path)
                    for p in plans],
        )
        self._result, self._phase = result, "during the conversion"

        if unrecognized_vobs:
            names = ", ".join(v.name for v in unrecognized_vobs)
            result.warnings.append(f"unrecognized VOB name(s) not converted: {names}")
        result.warnings.extend(vob_number_gaps(plans))  # advisory: no outcome change

        for plan, title in zip(plans, result.titles):
            label = "main title" if plan.is_main else f"title {plan.vts_number:02d}"
            log_divider(f"Encoding {label} → {plan.output_path.name}")

            # A titleset that does not pass blocks promotion of the whole
            # ISO, but remaining titlesets are still attempted so the record
            # is complete. Ctrl-C stops here: a title interrupted while it
            # encodes is recorded as failed (one interrupted while its encode
            # is being prepared stays "not_attempted"), and remaining
            # titlesets stay "not_attempted". So does an ffmpeg that could
            # not be confirmed stopped (cleanup_incomplete): nothing more may
            # run beside it.
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
            except EncoderNotStopped as e:
                result.conversion_time += title.encode_time
                log(colorize("red", f"Remaining titleset(s) not attempted: {e}."))
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
        self._settle_outcome(result)
        self._settled, self._phase = True, "while the titleset outcomes were being shown"
        self._log_title_outcomes(result)
        return result

    def _settle_outcome(self, result: ConversionResult):
        """Decide the run's outcome once its titlesets are finished. It may
        run again for the same result (after a Ctrl-C) and adds each warning
        once. A Ctrl-C fails the run only if it stopped work: once every
        titleset is promoted it is a warning and the outcome stands."""
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
                if t.location_unverified:
                    where = "its current location could not be verified"
                    if t.retained_path:
                        where += f" (last known at {t.retained_path})"
                else:
                    where = f"it is at {t.retained_path}" if t.retained_path else "it was not found"
                warning = (f"{t.output_path.name}: output could not be kept under "
                           f"its {'.partial' if t.status == 'failed' else 'review'} "
                           f"name ({t.retention_error}); {where}")
                if warning not in result.warnings:
                    result.warnings.append(warning)

        # Run outcome: failure outranks unverified
        errors = [f"{t.output_path.name}: {t.error}" for t in result.titles if t.status == "failed"]
        if result.promotion_error:
            errors.append(result.promotion_error)
        if result.cleanup_incomplete:
            details = result.encoder_cleanup or {}
            errors.append(f"encoder cleanup incomplete: ffmpeg (pid {details.get('pid')}) could "
                          f"not be confirmed stopped; the batch stops and the source stays attached")
        if result.interrupted and not any("Interrupted" in e for e in errors):
            if result.titles and all(t.status == "completed" for t in result.titles):
                if not any("Interrupted" in w for w in result.warnings):
                    result.warnings.append("Interrupted by operator (Ctrl-C) after every "
                                           "titleset was promoted")
            else:
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
            # Until it is moved, the output is wherever it was encoded, unless
            # an interrupted promotion left its location unverified (that
            # finding stands until a check finds the output)
            if not title.location_unverified:
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

        Ctrl-C is held across these renames (no native tool runs among
        them) and acknowledged once they are done: the run then keeps its
        outcome, with the interruption as a warning. This is not one atomic
        transaction: if a rename fails, or a final name or temp file cannot
        be checked before its rename (only "no such file" counts as absent),
        promotion stops before that rename, the run fails and the records
        name exactly which final files this run changed. There is no
        rollback. A Ctrl-C that still reaches a rename (raised directly, or
        off the main thread, where nothing can be held) stops promotion too,
        once where that output is has been settled by device and inode,
        never by name."""
        if self.run:
            self.run.mark("promotion")
        held = DeferredInterrupt()
        with held:
            for title in result.titles:
                final, problem = inspect_path(title.output_path)
                if problem:
                    result.promotion_error = (f"{title.output_path.name}: could not check the "
                                              f"final name before promotion ({problem}); "
                                              f"not promoted")
                    break
                existed = final is not None
                source, problem = inspect_path(title.temp_path)
                if problem:
                    result.promotion_error = (f"{title.output_path.name}: could not check "
                                              f"{title.temp_path} ({problem}); not promoted")
                    break
                try:
                    os.replace(title.temp_path, title.output_path)
                    self._mark_promoted(title, existed)
                except KeyboardInterrupt:
                    result.interrupted = True
                    if source is not None:  # else there was nothing to rename
                        self._reconcile_promotion(title, existed, source, result)
                    result.promotion_error = "Interrupted by operator (Ctrl-C) during promotion"
                    break
                except OSError as e:
                    result.promotion_error = (f"{title.output_path.name}: could not be promoted "
                                              f"to its final name: {e}")
                    break
        if held.received:
            result.interrupted = True
            if not result.promotion_error:
                result.warnings.append("Interrupted by operator (Ctrl-C) during promotion; "
                                       "it was held until every titleset was promoted")
        if result.promotion_error:
            log(colorize("red", f"Promotion stopped: {result.promotion_error}"))

    def _reconcile_promotion(self, title: TitleOutput, existed: bool,
                             source: os.stat_result, result: ConversionResult):
        """After an interrupted promotion rename: the title is promoted only
        if its final name now holds the output (same device and inode). A
        location that cannot be verified is recorded as such, and a final
        name that could not be checked is named as possibly replaced. A temp
        name found not to hold the output is never recorded as its location
        (as in _retain_attempt)."""
        where, problems = self._located_after_move(title.temp_path, title.output_path, source)
        if where == "target":
            self._mark_promoted(title, existed)
        elif where is None:
            if title.temp_path not in problems:  # the temp name is known not to hold it
                title.retained_path, title.retained_as = None, None
            if problems:
                title.retention_error = (
                    f"location could not be verified after an interrupted promotion to "
                    f"{title.output_path} (candidates {title.temp_path}, {title.output_path}): "
                    + "; ".join(f"{p}: {e}" for p, e in problems.items()))
                title.location_unverified = True
            else:
                title.retention_error = (f"after an interrupted promotion, neither "
                                         f"{title.temp_path} nor {title.output_path} holds this "
                                         f"output")
            if title.output_path in problems:
                title.final_name_unverified = True
                result.warnings.append(f"{title.output_path.name} may have been replaced by this "
                                       f"run: its promotion was interrupted and it could not be "
                                       f"checked afterwards")

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
        The new location is recorded in the same try as the rename, so a
        Ctrl-C seen once the rename call has returned (the rename may already
        have happened) is reconciled before it propagates: the record then
        names where the output is, decided by device and inode (never by name
        alone). Once both fields name the new location they stand; no fresh
        check is allowed to undo them. A repeat call keeps what an earlier
        call established: an output it moved, a location it could not verify
        (adding any new check problem to the candidates already recorded),
        or its finding that neither name holds the output. Only "no such
        file" counts as absence: any other error while checking leaves what
        is known in place and records that the location could not be
        verified. Sets title.retained_path / retained_as / retention_error /
        location_unverified."""
        source, problem = inspect_path(path)
        if problem:
            note = f"could not check {path}: {problem}"
            if title.retained_as in ("partial", "review_copy"):
                # An earlier call moved it: where it is stays established
                log(colorize("yellow", f"Warning: {title.output_path.name}: {note} "
                                       f"(already kept as {title.retained_path.name})"))
                return
            # Never drop earlier findings (candidate locations): add this one
            title.retention_error = f"{title.retention_error}; then {note}" if title.retention_error else note
            title.location_unverified = True
            log(colorize("yellow", f"Warning: {title.output_path.name}: {note}; "
                                   f"its current location could not be verified"))
            return
        if source is None:
            # Gone: keep what an earlier call established (a move, a location
            # it could not verify, or its finding that neither name holds the
            # output); otherwise nothing was kept
            if title.retained_as not in ("partial", "review_copy") and not title.location_unverified:
                title.retained_path, title.retained_as = None, None
            return
        # Truthful even if interrupted below: until moved, it is still here
        title.retained_path, title.retained_as = path, "encoding_temp"
        title.retention_error, title.location_unverified = None, False
        kept_as = "partial" if kind == "failed" else "review_copy"
        extension = ".mp4.partial" if kind == "failed" else ".mp4.unverified"
        base = f"{title.output_path.stem}_{self._run_token(kind)}"
        error = f"no free name found after {RETENTION_ATTEMPTS} attempts"
        for attempt in range(RETENTION_ATTEMPTS):
            name = f"{base}{extension}" if attempt == 0 else f"{base}-{uuid4().hex[:6]}{extension}"
            target = path.with_name(name)
            # Ctrl-C is held while the name is reserved, so a placeholder is
            # never left without its identity; one that came is acted on
            # once this attempt's own placeholder is removed again
            fd, reserved, reserve_error, held = None, None, None, DeferredInterrupt()
            with held:
                try:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                except FileExistsError:
                    pass
                except OSError as e:
                    reserve_error = e
                if fd is not None:
                    try:
                        reserved = os.fstat(fd)
                    finally:
                        os.close(fd)
            if held.received:
                leftover = self._remove_reservation(target, reserved) if reserved is not None else ""
                if leftover and self._result is not None:
                    # An empty placeholder could not be removed: say so
                    note = (f"{title.output_path.name}: after a Ctrl-C while its kept name was "
                            f"reserved, {leftover.strip()[1:-1]}")
                    if note not in self._result.warnings:
                        self._result.warnings.append(note)
                    log(colorize("yellow", f"Warning: {note}"))
                raise KeyboardInterrupt
            if reserve_error is not None:
                error = f"could not reserve {name}: {reserve_error}"
                break
            if fd is None:
                continue
            try:
                os.replace(path, target)
                title.retained_path, title.retained_as = target, kept_as
            except OSError as e:
                leftover = self._remove_reservation(target, reserved)
                error = f"could not move it to {name}: {e}{leftover}"
                break
            except BaseException:
                if (title.retained_path, title.retained_as) != (target, kept_as):
                    # Interrupted, possibly after the rename committed but before
                    # both fields recorded it: find where the output really is
                    where, problems = self._located_after_move(path, target, source)
                    if where == "target":
                        title.retained_path, title.retained_as = target, kept_as
                    elif where == "path":
                        title.retained_path, title.retained_as = path, "encoding_temp"
                    else:
                        if path not in problems:      # the temp name is known not to hold it
                            title.retained_path, title.retained_as = None, None
                        if problems:
                            title.retention_error = (
                                f"location could not be verified after an interrupted move to "
                                f"{target} (candidates {path}, {target}): "
                                + "; ".join(f"{p}: {e}" for p, e in problems.items()))
                            title.location_unverified = True
                        else:
                            title.retention_error = (f"after an interrupted move, neither {path} "
                                                     f"nor {target} holds this output")
                    self._remove_reservation(target, reserved)
                raise
            log(colorize("yellow", f"{title.output_path.name}: kept as {target.name}"
                                   + ("" if kind == "failed" else " (review copy, not a deliverable)")))
            return
        title.retention_error = error
        current, problem = inspect_path(path)
        if problem:
            title.retention_error += f"; could not check {path} afterwards: {problem}"
            title.location_unverified = True
        elif current is None:
            title.retained_path, title.retained_as = None, None
        log(colorize("yellow", f"Warning: {title.output_path.name}: {error}; "
                               f"the output stays at {path}"))

    @staticmethod
    def _located_after_move(path: Path, target: Path,
                            source: os.stat_result) -> Tuple[Optional[str], Dict[Path, str]]:
        """After an interrupted rename: ("target" | "path", {}) for the name
        that now holds the output (same device and inode as before the move),
        else (None, {name: error}) for the names whose check failed - unknown,
        not absent; an empty dict means neither holds it."""
        problems: Dict[Path, str] = {}
        for label, p in (("target", target), ("path", path)):
            current, problem = inspect_path(p)
            if problem:
                problems[p] = problem
            elif current is not None and \
                    (current.st_dev, current.st_ino) == (source.st_dev, source.st_ino):
                return label, {}
        return None, problems

    def _stop_failed_encode(self, title: TitleOutput, temp_path: Path, process,
                            result: ConversionResult) -> bool:
        """For a titleset already recorded as failed: stop its ffmpeg if this
        run started one, and only then keep its output (whose existence is
        checked only after the stop, so output ffmpeg created late is found).
        Ctrl-C is held during the bounded stop, and, when ffmpeg could not be
        confirmed stopped, until the run is marked cleanup_incomplete, the
        mount is told to stay attached and the recovery details are
        recorded; a held Ctrl-C is raised afterwards. Returns False when
        ffmpeg could not be confirmed stopped."""
        problem, held = None, DeferredInterrupt()
        if process is not None:
            with held:
                problem = stop_encoder(process)
                if problem is not None:
                    self._record_unstopped_encoder(temp_path, process, result, problem)
        try:
            if problem is None:
                self._retain_attempt(title, temp_path, "failed")
            else:
                self._encoder_not_stopped(title, temp_path, result)
        except Exception as e:
            # Never replaces the title's recorded cause
            note = f"{title.output_path.name}: could not finish recording the stopped encode: {e}"
            result.warnings.append(note)
            log(colorize("red", f"Warning: {note}"))
        if held.received:
            if "Interrupted by operator" not in (title.error or ""):
                title.error = (f"{title.error}; then Interrupted by operator (Ctrl-C) while "
                               f"ffmpeg was being stopped")
            raise KeyboardInterrupt
        return problem is None

    def _record_unstopped_encoder(self, temp_path: Path, process, result: ConversionResult,
                                  problem: str):
        """Called with Ctrl-C held, the moment a killed ffmpeg is not confirmed
        exited: the run stops (cleanup_incomplete, not a user interruption),
        this run's own source mount is told to stay attached, and the
        recovery details (pid, source ISO, possible output, mount points,
        devices, reason) are recorded. Attribute work only, no file checks."""
        result.cleanup_incomplete = True
        pid = getattr(process, "pid", None)
        mount = self._mount
        if mount is not None:
            mount.keep_attached = f"ffmpeg (pid {pid}) could not be confirmed stopped ({problem})"
        result.encoder_cleanup = {
            "pid": pid,
            "source_iso": str(self.config.iso_path),
            "possible_output": str(temp_path),
            "mount_points": [str(p) for p in getattr(mount, "mount_points", None) or []],
            "devices": [str(d) for d in getattr(mount, "devices", None) or []],
            "reason": problem,
        }

    def _encoder_not_stopped(self, title: TitleOutput, temp_path: Path, result: ConversionResult):
        """After _record_unstopped_encoder: nothing the possibly running
        ffmpeg may still be writing is moved or renamed; the title's record
        says where its output is, and the recovery details are shown and go
        into the warnings."""
        details = result.encoder_cleanup or {}
        pid = details.get("pid")
        current, check_problem = inspect_path(temp_path)
        if check_problem:
            title.retention_error = f"could not check {temp_path}: {check_problem}"
            title.location_unverified = True
        elif current is not None:
            title.retained_path, title.retained_as = temp_path, "encoding_temp"
        warning = (f"{title.output_path.name}: ffmpeg (pid {pid}) could not be confirmed "
                   f"stopped ({details.get('reason')}); it may still be running and writing "
                   f"{temp_path}. {self.config.iso_path.name} stays attached at "
                   f"{', '.join(details.get('mount_points') or []) or 'no mount point'} "
                   f"({', '.join(details.get('devices') or []) or 'no device'}); resolve that "
                   f"process and mount before retrying or detaching")
        log(colorize("red", f"Warning: {warning}"))
        result.warnings.append(warning)

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
                what = ("its location could not be verified" if t.location_unverified
                        else "could not be kept under its review name")
                log(colorize("yellow", f"  {what}: {t.retention_error}"))
        if result.titles and not all(t.status == "completed" for t in result.titles):
            changed, uncertain = result.canonical_files_changed, result.canonical_files_uncertain
            if changed:
                log(colorize("red", "Final MP4s changed by this run: "
                                    + ", ".join(p.name for p in changed)))
            if uncertain:
                log(colorize("red", "Final MP4s this run may have replaced (could not be "
                                    "checked): " + ", ".join(p.name for p in uncertain)))
            if not changed and not uncertain:
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
        # damage a pre-existing good file there. A name whose check fails is
        # never taken as free (the titleset then fails before ffmpeg starts).
        while True:
            temp_path = plan.output_path.with_name(
                f"{plan.output_path.stem}.encoding-{uuid4().hex[:6]}.mp4")
            taken, problem = inspect_path(temp_path, follow_symlinks=True)
            if problem:
                raise OSError(f"could not check {temp_path}: {problem}")
            if taken is None:
                break

        concat_input = "concat:" + "|".join(str(v) for v in plan.content_vobs)
        ffmpeg_cmd = [
            "ffmpeg",
            "-nostdin",  # hardening: no terminal input (stdin is /dev/null too)
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
        log(colorize("cyan", "Command:") + " " + colorize("yellow", shlex.join(ffmpeg_cmd)))

        process = None                   # None = no ffmpeg acquired by this run
        launch_hold = DeferredInterrupt()
        encode_start = time.time()
        try:
            # Run ffmpeg with progress output. stdout is discarded rather than
            # piped: we only read stderr below, and an unread stdout pipe could
            # fill its OS buffer and deadlock the encode if ffmpeg ever writes to
            # it (no reader would drain it). stdin is /dev/null (DEVNULL, with
            # -nostdin), as hardening: ffmpeg never reads the operator's
            # terminal. Ctrl-C is held only until the handle is assigned,
            # then acted on, so a started ffmpeg is always stopped by the
            # handler below. Undecodable stderr bytes are replaced rather
            # than raising (cleanup does not depend on this).
            with launch_hold:
                process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                    encoding="utf-8",
                    errors="replace"
                )
            if launch_hold.received:
                raise KeyboardInterrupt

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
                # The cause is recorded before the (possibly long) error
                # output is shown, so a Ctrl-C meanwhile cannot replace it
                title.status = "failed"
                title.error = f"ffmpeg failed with return code {process.returncode}"
                stderr_text = "".join(stderr_output)
                log(colorize("red", f"ffmpeg error:\n{stderr_text}"))
                self._retain_attempt(title, temp_path, "failed")
                return

        except KeyboardInterrupt:
            # The cause is recorded first, so the record stays truthful
            # whatever happens while ffmpeg is being stopped; a cause already
            # recorded is kept, with the interruption added
            title.encode_time = time.time() - encode_start
            if title.status == "failed" and title.error:
                if "Interrupted by operator" not in title.error:
                    title.error = f"{title.error}; then Interrupted by operator (Ctrl-C)"
            else:
                title.status = "failed"
                title.error = "Interrupted by operator (Ctrl-C)"
            log(colorize("red", "\nConversion interrupted by user."))
            self._stop_failed_encode(title, temp_path, process, result)
            raise
        except Exception as e:
            title.encode_time = time.time() - encode_start
            title.status = "failed"
            title.error = str(e) if process is not None else f"could not start ffmpeg: {e}"
            if launch_hold.received:     # Ctrl-C held while the launch itself failed
                title.error += "; Interrupted by operator (Ctrl-C) during the launch"
            stopped = self._stop_failed_encode(title, temp_path, process, result)
            if launch_hold.received:
                raise KeyboardInterrupt
            if not stopped:
                raise EncoderNotStopped(f"ffmpeg for {title.output_path.name} could not be "
                                        f"confirmed stopped")
            return

        # Post-encode checks share the encode's failure handling: any error
        # here fails the title, keeps the temp as .partial, and flows into
        # the normal failure records (it must never escape the record
        # machinery).
        name = plan.output_path.name
        stage = "verification"
        try:
            # Only "no such file" means it was not created: a check that
            # fails leaves its location unknown, and the record says so
            output, problem = inspect_path(temp_path, follow_symlinks=True)
            if problem:
                title.status = "failed"
                title.error = f"post-encode {stage} failed: could not check {temp_path}: {problem}"
                log(colorize("red", f"{name}: {title.error}"))
                self._retain_attempt(title, temp_path, "failed")
                return
            if output is None:
                title.status = "failed"
                title.error = "Output file was not created"
                return

            title.output_size = output.st_size
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
            interruption = f"Interrupted by operator (Ctrl-C) during post-encode {stage}"
            if title.status == "failed" and title.error:  # never replace a recorded cause
                title.error = f"{title.error}; then {interruption}"
            else:
                title.status = "failed"
                title.error = interruption
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
    
    def _token_records(self, token: str) -> Tuple[Path, Path]:
        """(log, manifest) paths for records kept under a run token"""
        stem = f"{self.config.iso_name}_{token}"
        return (self.config.output_dir / f"{stem}.log.txt",
                self.config.output_dir / f"{stem}_manifest.json")

    ### --- run records ---

    def _record_kind(self, result: ConversionResult) -> Optional[str]:
        """Which records a result gets: "canonical" (the final log and
        manifest), or a pair under the run's token of that kind; None when
        the record paths themselves failed the symlink guard"""
        if result.records_unsafe:
            return None
        if result.success and not result.skipped:
            return "canonical"
        if result.skipped and not result.error_message:
            # A skip or dry run describes only this attempt: its log and
            # manifest go under its own token, created new, so they never
            # write over another run's records
            return "dryrun" if self.config.dry_run else "skipped"
        if result.error_message:
            return "aborted" if result.aborted else "failed"
        if result.unverified:
            # Nothing was promoted: records go under the run's token,
            # leaving any existing canonical log and manifest untouched
            return "unverified"
        return None

    def _finish_records(self, result: ConversionResult):
        """Write the run's records (_write_run_records). A Ctrl-C that lands
        between its held steps is noted (the batch stops) and the steps not
        yet done run once more; a further one is left to the batch and
        main()."""
        try:
            self._write_run_records(result)
        except KeyboardInterrupt:
            result.interrupted = True
            self._write_run_records(result)

    def _write_run_records(self, result: ConversionResult):
        """Every processed ISO gets a log and a manifest of some kind, log
        first, unless the record paths themselves failed the symlink guard.
        Record writing must not crash the run, but records are part of the
        deliverable: a run whose records cannot be written fails.

        1. What only a native tool can tell (the ffmpeg version, if it was
           not checked at the start; for the access manifest, ffprobe's view
           of the promoted MP4 and the processor name) is gathered first,
           with Ctrl-C not held: one there leaves the rest out, with a
           warning, and stops the batch.
        2. The records are written with Ctrl-C held. The canonical log and
           manifest go to temporary files beside them, renamed into place
           only once both are complete, log first, so an error, a Ctrl-C or
           a killed process never leaves either half-written under its
           final name (nothing is flushed to the storage: a power loss, or a
           volume that does not rename atomically, is not covered). The
           pair is still not one transaction: the log can be renamed into
           place and the manifest fail. Tokened records are written at
           their own names.
        3. If that fails, this run's own temporary, incomplete and orphaned
           record files are removed (nothing else), and the run fails.
           Unless failure records were what failed, it then writes
           failed-<token> records, naming any canonical records that no
           longer match the MP4s it promoted. Anything that could not be
           written or removed is listed on screen.
        4. The outcome is shown; a Ctrl-C there only cuts it short."""
        if self._records_kind is None:
            self._records_kind = self._record_kind(result)
        kind = self._records_kind
        if kind is None:
            return  # the record paths themselves failed the symlink guard
        if self._record_details is None:
            self._gather_record_details(result, kind)
        if self._records_stage == "pending":
            token = None if kind == "canonical" else self._run_token(kind)
            held = DeferredInterrupt()
            with held:
                if kind != "canonical":
                    self._add_stale_records_warning(result, [])
                self._record_error, self._records_written = self._commit_records(result, kind, token)
                self._records_stage = "committed"
            if held.received:
                result.interrupted = True
                self._record_messages.append(
                    ("yellow", "Ctrl-C was received while the run records were being written; "
                               "it was held until they were finished."))
        if self._records_stage == "committed":
            held = DeferredInterrupt()
            with held:
                if self._record_error is None:
                    self._record_messages.extend(self._saved_messages(kind))
                else:
                    self._recover_records(result, kind)
                self._records_stage = "recovered"
            if held.received:
                result.interrupted = True
        if self._records_stage == "recovered":
            self._records_stage = "finished"
            self._report_records(result)

    def _gather_record_details(self, result: ConversionResult, kind: Optional[str]):
        """What the records need from native tools, gathered before any
        record file is opened and with Ctrl-C not held (they have no
        timeout). These details are optional: one whose tool fails (any
        error) is left out with a warning naming the cause, and a Ctrl-C
        here leaves out what is still missing, with a warning, and stops
        the batch; the records are written all the same."""
        details: Dict[str, Any] = {"ffmpeg_version": self._ffmpeg_version}
        wanted = [] if self._ffmpeg_checked else ["ffmpeg version"]
        if kind == "canonical":
            wanted += ["video technical information", "processor"]
        done = []
        try:
            for item in wanted:
                try:
                    if item == "ffmpeg version":
                        details["ffmpeg_version"] = check_ffmpeg()[1]
                    elif item == "processor":
                        details["processor"] = platform.processor()
                    else:
                        details["video_info"] = (get_video_info(result.mp4_path)
                                                 if result.mp4_path else {})
                except Exception as e:
                    result.warnings.append(f"{item} not gathered for the records: "
                                           f"{str(e) or type(e).__name__}")
                done.append(item)
        except KeyboardInterrupt:
            result.interrupted = True
            missing = [item for item in wanted if item not in done]
            result.warnings.append(f"{' and '.join(missing)} not gathered for the records: "
                                   f"interrupted by operator (Ctrl-C)")
        self._record_details = details

    @contextlib.contextmanager
    def _record_file(self, path: Path, mode: str = "w"):
        """Open one run record for writing, as open(path, mode) would. The
        canonical log and manifest are written to a temporary file beside
        them, <name>.writing-<token>.tmp, that the commit renames into
        place; other records are written at their own (tokened) names, "x"
        creating them new. Each file is noted with the identity it was
        created with, so this run can remove its own temporary, incomplete
        or orphaned records, and never anything else."""
        staged = path in (self.config.log_path, self.config.manifest_path)
        target = path.with_name(f"{path.name}.{self._run_token('writing')}.tmp") if staged else path
        exclusive = staged or mode == "x"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC),
                     0o666)
        record = RecordFile(final=path, path=target, staged=staged)
        self._record_files.append(record)
        try:
            st = os.fstat(fd)
            record.identity = (st.st_dev, st.st_ino)
            f = open(fd, "w")
        except BaseException:
            os.close(fd)
            raise
        with f:
            yield f
        record.complete = True

    def _commit_records(self, result: ConversionResult, kind: str,
                        token: Optional[str]) -> Tuple[Optional[BaseException], List[Path]]:
        """Write one log and manifest, log first, with Ctrl-C held by the
        caller; the canonical pair is then renamed into place, log first.
        Returns what stopped it, if anything (a KeyboardInterrupt here was
        raised directly, not held), and the canonical records renamed."""
        self._record_files = []
        written: List[Path] = []
        try:
            if kind == "canonical":
                self._create_log_file(result)
                self._create_manifest(result)
                for record in self._record_files:
                    if record.staged:
                        os.replace(record.path, record.final)
                        record.committed = True
                        written.append(record.final)
            elif kind in ("skipped", "dryrun"):
                self._create_log_file(result, dry_run=self.config.dry_run, token=token)
                self._create_skip_manifest(result, token)
            else:
                self._create_log_file(result, is_error=True, token=token)
                self._create_error_manifest(result, token)
        except (Exception, KeyboardInterrupt) as e:
            return e, written
        return None, written

    def _saved_messages(self, kind: str) -> List[Tuple[str, str]]:
        """Where each record of a completed write is, for the screen"""
        label = {"canonical": "Manifest", "skipped": "Skip manifest",
                 "dryrun": "Dry-run manifest", "aborted": "Aborted manifest",
                 "unverified": "Unverified manifest"}.get(kind, "Error manifest")
        return [("cyan", f"{label if r.final.name.endswith('_manifest.json') else 'Log'} "
                         f"saved to: {r.final}") for r in self._record_files]

    def _recover_records(self, result: ConversionResult, kind: str):
        """After a record write failed (Ctrl-C held by the caller): remove
        this run's own leftovers, fail the run, and write failed-<token>
        records instead, unless failure records were what failed. What
        cannot be recorded is listed on screen."""
        error = self._record_error
        if isinstance(error, KeyboardInterrupt):
            result.interrupted = True
        reason = "interrupted by operator (Ctrl-C)" if isinstance(error, KeyboardInterrupt) else str(error)
        self._record_messages.append(("red", f"Could not write run records: {reason}"))
        failure_records = kind in ("failed", "aborted")
        # A pair that failure records replace goes entirely; complete failure
        # records are kept, as the only record of this run
        leftovers = self._discard_records(keep_complete=failure_records)
        if result.success or result.unverified:
            result.success = False
            result.unverified = False
            # A skip without its record is not a valid skip — clear the
            # flag so batch accounting counts this as a failure too
            result.skipped = False
            result.error_message = f"Run records could not be written: {reason}"
        if failure_records:
            self._record_messages.append(("red", f"Not recorded: {result.error_message}"))
            self._report_unrecorded(result, leftovers)
            return
        if kind == "canonical":
            self._add_stale_records_warning(result, self._records_written)
        result.warnings.extend(leftovers)
        token = self._run_token("failed")
        fallback_error, _ = self._commit_records(result, "failed", token)
        if fallback_error is None:
            self._record_messages.extend(self._saved_messages("failed"))
            return
        if isinstance(fallback_error, KeyboardInterrupt):
            result.interrupted = True
            fallback_reason = "interrupted by operator (Ctrl-C)"
        else:
            fallback_reason = str(fallback_error)
        self._record_messages.append(("red", f"Could not write failure records either: "
                                             f"{fallback_reason}"))
        self._report_unrecorded(result, self._discard_records(keep_complete=True))

    def _discard_records(self, keep_complete: bool) -> List[str]:
        """Remove this run's own files from a record write that failed:
        every temporary or incomplete one, and complete ones too unless
        keep_complete. A file is removed only while it is still the one this
        run created (same device and inode). Returns a note for each one
        that is left, or that could not be checked."""
        notes = []
        for record in self._record_files:
            if record.committed or (keep_complete and record.complete):
                continue
            current, problem = inspect_path(record.path)
            what = f"{record.path}, from this run's record write that failed (not a record)"
            if problem:
                notes.append(f"could not check {what}: {problem}")
                continue
            if current is None or (record.identity is not None and (
                    (current.st_dev, current.st_ino) != record.identity
                    or not stat.S_ISREG(current.st_mode))):
                continue  # gone, or not this run's file any more: left alone
            if record.identity is None:
                notes.append(f"{what} was left: it could not be identified as this run's own")
                continue
            try:
                os.unlink(record.path)
            except OSError as e:
                notes.append(f"{what} could not be removed: {e}")
        return notes

    def _report_unrecorded(self, result: ConversionResult, leftovers: List[str]):
        """No records could be written for this run: put on screen what they
        would have said (its warnings), its record files that are left, and
        where its MP4s and kept outputs are"""
        for note in result.warnings + leftovers:
            self._record_messages.append(("red", f"Not recorded: {note}"))
        for record in self._record_files:
            if record.complete and not record.staged and inspect_path(record.path)[0] is not None:
                self._record_messages.append(("red", f"Written, without the rest of its "
                                                     f"records: {record.path}"))
        for t in result.titles:
            if t.promoted_this_run:
                self._record_messages.append(("red", f"Promoted by this run: {t.output_path}"))
            elif t.retained_path or t.location_unverified:
                where = (f"location unknown ({t.retention_error})" if t.location_unverified
                         else f"kept as {t.retained_path}")
                self._record_messages.append(("red", f"{t.output_path.name}: {where}"))

    def _add_stale_records_warning(self, result: ConversionResult, written: List[Path]):
        """When this run promoted MP4s but did not write both canonical
        records, warn which records no longer match them: each canonical
        record is named as written by this run, from an earlier run, absent
        or not checkable (only "no such file" counts as absent)"""
        changed = result.canonical_files_changed
        canonical = (self.config.manifest_path, self.config.log_path)
        if not changed or all(p in written for p in canonical):
            return
        them = "it" if len(changed) == 1 else "them"
        parts, earlier = [], []
        for path in canonical:
            if path in written:
                parts.append(f"{path.name} was written by this run")
                continue
            current, problem = inspect_path(path)
            if problem:
                parts.append(f"{path.name} could not be checked ({problem})")
            elif current is not None:
                earlier.append(path.name)
            elif path == self.config.manifest_path:
                parts.append(f"no canonical manifest describes {them}")
            else:
                parts.append("there is no canonical log")
        if earlier:
            parts.append(f"the existing {' and '.join(earlier)} "
                         + (f"describes an earlier run and no longer matches {them}"
                            if len(earlier) == 1 else
                            f"describe an earlier run and no longer match {them}"))
        what = ("stopped before writing its own records" if self._records_kind != "canonical"
                else "could not write its own canonical records")
        warning = (f"this run replaced or created {', '.join(p.name for p in changed)} but "
                   f"{what}: {'; '.join(parts)}")
        if warning not in result.warnings:
            result.warnings.append(warning)

    def _report_records(self, result: ConversionResult):
        """Show what the records step found, then a success's summary. The
        records are final by now, so a Ctrl-C here only cuts this short
        (and stops the batch)."""
        try:
            for colour, message in self._record_messages:
                log(colorize(colour, message))
            if result.success and not result.skipped:
                self._generate_summary(result)
        except KeyboardInterrupt:
            result.interrupted = True

    def _earlier_skip_records(self) -> List[str]:
        """Skip and dry-run records left under the old fixed names (see
        ConversionConfig), for success records to name; one that cannot be
        checked is named with the reason"""
        found = []
        for path in (self.config.legacy_skip_manifest_path, self.config.legacy_dry_run_log_path):
            try:
                os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as e:
                found.append(f"{path} (could not be checked: {e.strerror or e})")
                continue
            found.append(str(path))
        return found

    def _create_log_file(self, result: ConversionResult, dry_run: bool = False,
                         is_error: bool = False, token: Optional[str] = None):
        """Create formatted log file.

        Failure/aborted/unverified, skip and dry-run logs are written under
        the run's token so such a run never overwrites the log a prior
        success manifest references (nor an earlier failure's or skip's
        log). A skip or dry-run log is created new, never over a file. The
        canonical log is written to a temporary file (see _record_file)."""
        log_path = self._token_records(token)[0] if token else self.config.log_path
        skip_record = bool(token) and result.skipped and not is_error
        details = self._record_details or {}
    
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
    
        end_time = datetime.datetime.now()
        total_duration = (end_time - self.start_time).total_seconds()
        
        with self._record_file(log_path, "x" if skip_record else "w") as f:
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
            if skip_record:
                f.write(f"Manifest:         {self._token_records(token)[1].name} "
                        f"(this attempt only; any final MP4, log and manifest are "
                        f"from another run)\n")
            elif not token and not dry_run:
                for earlier in self._earlier_skip_records():
                    f.write(f"Earlier record:   {earlier} (from an earlier skipped or dry "
                            f"run; it does not describe this conversion)\n")
            if result.error_message:
                f.write(f"Error:            {result.error_message}\n")
            if result.unverified_reason:
                f.write(f"Unverified:       {result.unverified_reason}\n")
            if result.titles and not dry_run and not result.success:
                changed, uncertain = result.canonical_files_changed, result.canonical_files_uncertain
                final_mp4s = ("changed by this run: " + ", ".join(p.name for p in changed) if changed
                              else "none confirmed promoted or changed by this run" if uncertain
                              else "none promoted or changed by this run")
                if uncertain:
                    final_mp4s += (f"; {', '.join(p.name for p in uncertain)} may have been "
                                   f"replaced (could not be checked)")
                f.write(f"Final MP4s:       {final_mp4s}\n")
                if any(t.retained_as == "review_copy" for t in result.titles):
                    f.write("Note:             review copies (*.mp4.unverified) are not deliverables\n")
            for warning in result.warnings:
                f.write(f"Warning:          {warning}\n")
            f.write("\n")

            if result.cleanup_incomplete and result.encoder_cleanup:
                cleanup = result.encoder_cleanup
                f.write("ENCODER CLEANUP INCOMPLETE\n")
                f.write("-" * 20 + "\n")
                f.write("ffmpeg could not be confirmed stopped. The batch stopped and the source\n")
                f.write("stays attached; resolve the process and the mount before retrying or\n")
                f.write("detaching. The output below may still be changing.\n")
                f.write(f"PID:              {cleanup.get('pid')}\n")
                f.write(f"Source ISO:       {cleanup.get('source_iso')}\n")
                f.write(f"Possible output:  {cleanup.get('possible_output')}\n")
                f.write(f"Mount points:     {', '.join(cleanup.get('mount_points') or []) or 'none'}\n")
                f.write(f"Devices:          {', '.join(cleanup.get('devices') or []) or 'none'}\n")
                f.write(f"Reason:           {cleanup.get('reason')}\n\n")

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
                        label = "Location: " if t.location_unverified else "Not moved:"
                        f.write(f"  {label} {t.retention_error}\n")
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
            
            # As found at the start of the run (no native tool runs here)
            f.write(f"FFmpeg Version:   {details.get('ffmpeg_version', self._ffmpeg_version)}\n")
            f.write(f"Tool Version:     v1.0\n\n")
            
            f.write("=" * 70 + "\n")
            f.write(f"Log generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 70 + "\n")
    
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
                    "location_unverified": t.location_unverified,
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
        """Create comprehensive JSON manifest, from what was gathered before
        it is written (see _gather_record_details), to a temporary file
        renamed into place once complete (see _record_file)"""
        now = datetime.datetime.now()
        details = self._record_details or {}
        ffmpeg_version = details.get("ffmpeg_version", self._ffmpeg_version)
        video_info = details.get("video_info") or {}
        
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
                "processor": details.get("processor"),
                "python_version": platform.python_version(),
                "ffmpeg_version": ffmpeg_version
            },
            
            "output_files": {
                "mp4_file": str(result.mp4_path) if result.mp4_path else None,
                "log_file": str(self.config.log_path),
                "manifest_file": str(self.config.manifest_path),
                # Left by an earlier skipped or dry run under the old fixed
                # names; they do not describe this conversion
                "earlier_skip_records": self._earlier_skip_records()
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
        
        with self._record_file(self.config.manifest_path) as f:
            json.dump(manifest, f, indent=2, sort_keys=False)
    
    def _create_skip_manifest(self, result: ConversionResult, token: str):
        """Create manifest for skipped files (and dry runs), under the run's
        token and created new, never over a file"""
        now = datetime.datetime.now()
        log_path, manifest_path = self._token_records(token)

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

            "output_files": {
                "log_file": str(log_path),
                "manifest_file": str(manifest_path),
                "scope": ("this attempt only; any final MP4, log and access manifest "
                          "beside it are from another run")
            },

            "archival_notes": {
                "note": result.skip_reason if result.skip_reason else "Skipped",
                "project": "Johnson Publishing Company Archive (JPCA)"
            }
        }

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        with self._record_file(manifest_path, "x") as f:
            json.dump(manifest, f, indent=2, sort_keys=False)
    
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
                "canonical_files_changed": [str(p) for p in result.canonical_files_changed],
                # ffmpeg not confirmed stopped: recovery details (pid, source
                # ISO, possible output, mount points, devices, reason)
                "cleanup_incomplete": result.cleanup_incomplete,
                "encoder_cleanup": result.encoder_cleanup
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
                "note": (("No answer to the overwrite prompt (end of input), so nothing was "
                          "encoded or overwritten" if result.error_message == NO_ANSWER_ABORT
                          else "Conversion aborted by operator before any encoding")
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

        with self._record_file(manifest_path) as f:
            json.dump(manifest, f, indent=2, sort_keys=False)
    
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
        elif analysis.disc_type == "dvd_audio" and analysis.has_video_ts:
            # DVD-Video discs often carry an empty AUDIO_TS, so this may be an
            # incomplete DVD-Video image as much as a DVD-Audio disc
            return ("DVD-Audio disc, or an incomplete DVD-Video image: its VIDEO_TS has no VOB "
                    "files - verify the ISO and check the source disc; a DVD-Audio disc "
                    "requires specialized audio extraction tools")
        elif analysis.disc_type == "dvd_audio":
            return "DVD-Audio disc - requires specialized audio extraction tools"
        elif analysis.disc_type == "video_ts_without_vobs":
            return ("VIDEO_TS has no VOB files - the image may be incomplete or unfinalized: "
                    "verify the ISO and check the source disc")
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
# makeiso.py's in-progress (or abandoned) copy of a disc, never a verified
# master: <filename>_attempt-<YYYYmmddTHHMMSS>-<6 hex>.iso
ATTEMPT_ISO_RE = re.compile(r".+_attempt-\d{8}T\d{6}-[0-9a-f]{6}")
# A symlink target that is not there: dangling, or a path component is a file
TARGET_MISSING = (errno.ENOENT, errno.ENOTDIR)

@dataclass
class BatchDiscovery:
    """What a batch search found (see find_batch_isos). Paths are as
    reached from the batch folder, never resolved."""
    isos: List[Path] = field(default_factory=list)       # masters to process, in order
    archived: List[Path] = field(default_factory=list)   # superseded masters in archive folders
    not_masters: List[Tuple[Path, str]] = field(default_factory=list)  # listed, never processed
    notes: List[Tuple[Path, str]] = field(default_factory=list)        # not searched; exit unaffected
    problems: List[Tuple[Path, str]] = field(default_factory=list)     # incomplete search or refused ISO

def _reason(e: OSError) -> str:
    """An OSError as "[Errno N] text", without the path the listing names"""
    return f"[Errno {e.errno}] {e.strerror}" if e.errno and e.strerror else str(e)

def _not_a_master(name: str) -> Optional[str]:
    """Why a batch never converts an .iso of this name, or None. By name
    only, so nothing is opened"""
    if name.startswith("._"):
        return "macOS AppleDouble metadata file (._*), not a master"
    if ATTEMPT_ISO_RE.fullmatch(Path(name).stem):
        return "makeiso.py attempt ISO (*_attempt-<stamp>-<random>.iso), not a verified master"
    return None

def find_batch_isos(iso_dir: Path) -> BatchDiscovery:
    """Find the ISOs a batch converts, listing every folder one at a time
    (os.scandir) and recording, instead of skipping, anything that leaves
    the search incomplete: a batch folder that cannot be checked or
    listed, a folder that cannot be listed or whose listing stops partway
    (the entries already read are still used), an entry that cannot be
    inspected or disappears, a folder loop, an .iso symlink whose target
    is missing or cannot be checked, and an .iso that is not a regular file
    (FIFO, device or socket: never opened). A given symlinked batch folder
    is followed; symlinked folders inside it never are (they are notes),
    while an .iso symlink to a regular file is converted. A folder named
    *.iso is searched, never taken as an ISO.

    Superseded masters in makeiso.py archive folders (*_overwritten-*/) are
    listed, not converted: converting them would write access copies inside
    the archive folder, or, with -o, collide with the current master's
    access folder. Nothing there is converted, so a part of one that
    cannot be searched is a note, not a problem. AppleDouble (._*) and
    makeiso.py attempt ISOs are listed as not masters (-i still converts
    one). No folder is exempt by name."""
    found = BatchDiscovery()
    try:
        root_st = os.stat(iso_dir)
    except FileNotFoundError:
        found.problems.append((iso_dir, "batch folder not found"))
        return found
    except OSError as e:
        found.problems.append((iso_dir, f"could not check the batch folder: {_reason(e)}"))
        return found
    if not stat.S_ISDIR(root_st.st_mode):
        found.problems.append((iso_dir, "not a folder; give the folder that holds the ISOs"))
        return found
    # (folder, inside an archive folder, its ancestors' identities, their paths)
    stack = [(iso_dir, False, ((root_st.st_dev, root_st.st_ino),), (iso_dir,))]
    while stack:
        folder, archived, chain, chain_paths = stack.pop()
        report = found.notes if archived else found.problems
        where = " (inside a makeiso.py archive folder; nothing there is converted)" if archived else ""
        try:
            listing = os.scandir(folder)
        except FileNotFoundError:
            report.append((folder, "folder disappeared during the search" + where))
            continue
        except OSError as e:
            report.append((folder, f"could not list folder: {_reason(e)}" + where))
            continue
        entries = []
        with listing:
            while True:
                try:
                    entry = next(listing)
                except StopIteration:
                    break
                except OSError as e:
                    report.append((folder, f"listing stopped partway: {_reason(e)}" + where))
                    break
                entries.append(entry)  # used even if the listing fails later
        for entry in entries:
            path = folder / entry.name
            is_iso = path.suffix.lower() == ".iso"
            if not is_iso:
                try:
                    if entry.is_file(follow_symlinks=False):
                        continue  # an ordinary file: nothing to find in it
                except OSError:
                    pass  # the lstat below says what it is, or why it cannot be inspected
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                report.append((path, "entry disappeared during the search" + where))
                continue
            except OSError as e:
                report.append((path, f"could not inspect entry: {_reason(e)}" + where))
                continue
            if stat.S_ISDIR(st.st_mode):  # every real folder, whatever its name
                identity = (st.st_dev, st.st_ino)
                if identity in chain:
                    report.append((path, f"folder loop: the same folder as "
                                         f"{chain_paths[chain.index(identity)]}" + where))
                else:
                    stack.append((path, archived or bool(ARCHIVE_FOLDER_RE.fullmatch(entry.name)),
                                  chain + (identity,), chain_paths + (path,)))
                continue
            if stat.S_ISLNK(st.st_mode) and not is_iso:
                # Never followed; one to a folder (or to what cannot be
                # checked) is listed, since what it holds is not searched
                try:
                    target_mode = os.stat(path).st_mode
                except OSError as e:
                    detail = ("its target is missing" if e.errno in TARGET_MISSING
                              else f"its target could not be checked: {_reason(e)}")
                    found.notes.append((path, f"symlink not followed -> {_link_text(path)}; {detail}"))
                    continue
                if stat.S_ISDIR(target_mode):
                    found.notes.append((path, f"symlinked folder not followed -> {_link_text(path)}"))
                continue
            if not is_iso:
                continue  # nothing to find in it
            if archived:
                found.archived.append(path)
                continue
            why = _not_a_master(entry.name)
            if why:
                found.not_masters.append((path, why))
                continue
            mode = st.st_mode
            if stat.S_ISLNK(mode):
                try:
                    mode = os.stat(path).st_mode
                except OSError as e:
                    # Unclassified, so never handed on: it could be a FIFO or device
                    if e.errno in TARGET_MISSING:
                        detail = f"symlink target missing -> {_link_text(path)}; not processed"
                    else:
                        detail = (f"could not inspect the symlink's target -> {_link_text(path)}: "
                                  f"{_reason(e)}; not processed")
                    found.problems.append((path, detail))
                    continue
                if stat.S_ISDIR(mode):
                    found.notes.append((path, f"symlinked folder not followed -> {_link_text(path)}"))
                    continue
            if stat.S_ISREG(mode):
                found.isos.append(path)
            else:
                found.problems.append((path, "not a regular file (FIFO, device or socket); "
                                             "not processed"))
    found.isos.sort()
    found.archived.sort()
    for listed in (found.not_masters, found.notes, found.problems):
        listed.sort()
    return found

def _link_text(path: Path) -> str:
    """What a symlink says it points to, for a listing"""
    try:
        return os.readlink(path)
    except OSError:
        return "?"

def _batch_output_dir(iso_path: Path, iso_dir: Path, output_dir: Path) -> Path:
    """An ISO's access folder: beside the ISO by default, or under
    output_dir if one was given"""
    base = output_dir if output_dir != iso_dir else iso_path.parent
    return base / _access_dir_name(iso_path)

def _log_discovery(iso_dir: Path, found: BatchDiscovery):
    """What the search left out, and why, before anything is processed"""
    def shown(path: Path) -> str:
        return str(path) if path == iso_dir else str(path.relative_to(iso_dir))

    if found.archived:
        log(colorize("yellow", f"Skipping {len(found.archived)} superseded ISO(s) in makeiso.py "
                               f"archive folders (*_overwritten-*/; convert one with -i if needed):"))
        for p in found.archived:
            log(colorize("yellow", f"  {shown(p)}"))
    if found.not_masters:
        log(colorize("yellow", f"Skipping {len(found.not_masters)} file(s) that are not masters "
                               f"(convert one with -i if needed):"))
        for p, why in found.not_masters:
            log(colorize("yellow", f"  {shown(p)}: {why}"))
    if found.notes:
        log(colorize("yellow", f"Not searched ({len(found.notes)}): symlinked folders inside the "
                               f"batch folder are not followed (give one itself with --batch), and "
                               f"nothing in a makeiso.py archive folder is converted:"))
        for p, detail in found.notes:
            log(colorize("yellow", f"  {shown(p)}: {detail}"))
    if found.problems:
        log(colorize("red", f"Search incomplete or ISO refused ({len(found.problems)}); the ISOs "
                            f"found are still processed, and the batch exits 1 unless a Ctrl-C "
                            f"stops it:"))
        for p, detail in found.problems:
            log(colorize("red", f"  {shown(p)}: {detail}"))

def _recorded_cleanup_incomplete(converter) -> bool:
    """Whether a converter's run recorded an ffmpeg that could not be
    confirmed stopped (all that may be known after a further Ctrl-C)"""
    result = getattr(converter, "_result", None)
    return bool(result is not None and result.cleanup_incomplete)

def _count_batch_result(results: Dict[str, Any], iso_path: Path,
                        result: Optional[ConversionResult], cleanup_incomplete: bool = False):
    """Add one processed ISO to the batch results. result None: a further
    Ctrl-C left its run without a result here (counted as failed)."""
    results["processed"] += 1
    if result is None:
        results["failed"] += 1
        results["details"].append({
            "iso": iso_path.name, "success": False, "skipped": False, "aborted": False,
            "unverified": False, "interrupted": True, "cleanup_incomplete": cleanup_incomplete,
            "error": "Interrupted by operator (Ctrl-C); its run records may be missing "
                     "or incomplete",
            "skip_reason": None
        })
        return

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
        "interrupted": result.interrupted,
        "cleanup_incomplete": result.cleanup_incomplete,
        "error": result.error_message,
        "skip_reason": result.skip_reason
    })

def process_batch(iso_dir: Path, output_dir: Path, operator: str, 
                  dry_run: bool = False, progress: Optional[Dict[str, Any]] = None,
                  **encoding_kwargs) -> Dict[str, Any]:
    """Process every ISO the search of iso_dir finds (find_batch_isos), in
    order. Each access folder belongs to the first ISO that maps to it; a
    later one is refused, not processed. Anything the search could not
    cover, and every refused ISO, is a discovery error ("discovery_errors"
    in the results; main() exits 1 unless a Ctrl-C stopped the batch),
    listed before processing and again in the summary when any ISO is
    processed: the ISOs that were found are still processed. progress,
    if given, is kept up to date with the results and the current
    converter, so main() can still tell what was recorded if a further
    Ctrl-C leaves here."""
    found = find_batch_isos(iso_dir)
    iso_files, claimed = [], {}
    for p in found.isos:
        # Names that differ only in case count as one folder (as on macOS)
        folder = _batch_output_dir(p, iso_dir, output_dir)
        first = claimed.setdefault(str(folder).casefold(), p)
        if first == p:
            iso_files.append(p)
        else:
            found.problems.append((p, f"would share {folder} with {first}; not processed"))
    found.problems.sort()
    discovery_errors = [{"path": str(p), "detail": detail} for p, detail in found.problems]
    _log_discovery(iso_dir, found)
    
    if not iso_files:
        log(colorize("yellow", f"No ISO files found in: {iso_dir}" if not discovery_errors else
                               f"No ISO file to process in: {iso_dir} (see the "
                               f"{len(discovery_errors)} problem(s) listed above)"))
        return {"processed": 0, "success": 0, "skipped": 0, "unverified": 0, "failed": 0,
                "discovery_errors": discovery_errors}
    
    log_divider("Batch Processing")
    log(colorize("cyan", f"Found {len(iso_files)} ISO files"))
    
    results = {
        "processed": 0,
        "success": 0,
        "would_convert": 0,  # Dry-run VIDEO_TS discs
        "skipped": 0,        # Non-VIDEO_TS discs
        "unverified": 0,     # Nothing failed, but nothing promoted either
        "failed": 0,
        "interrupted": None, # the ISO a Ctrl-C stopped the batch at
        "not_started": 0,    # ISOs a Ctrl-C left unstarted
        "discovery_errors": discovery_errors,  # not searched, or refused: exit 1
        "details": []
    }
    if progress is not None:
        progress["results"] = results

    stopped_before = False  # the Ctrl-C came between ISOs: the one named never started
    try:
        for i, iso_path in enumerate(iso_files, 1):
            # A Ctrl-C stops the batch: no further ISO is started. One that
            # reaches an ISO's run is recorded by it (convert() never lets a
            # first one through). One that lands between ISOs leaves the next
            # one not started; a further one while an ISO's interruption is
            # being recorded leaves that ISO counted as failed.
            converter = result = None
            counted = interrupted = False
            try:
                log_divider(f"Processing {i}/{len(iso_files)}: {iso_path.name}")

                # Create config for this ISO — access folder next to source ISO by default,
                # or under output_dir if explicitly provided
                config = ConversionConfig(
                    iso_path=iso_path,
                    output_dir=_batch_output_dir(iso_path, iso_dir, output_dir),
                    operator=operator,
                    dry_run=dry_run,
                    **encoding_kwargs
                )

                # Run conversion
                converter = ISOToMP4Converter(config)
                if progress is not None:
                    progress["converter"] = converter
                result = converter.convert()
                # Counted with Ctrl-C held (bookkeeping only): one held here
                # stops the batch after this ISO
                held = DeferredInterrupt()
                with held:
                    _count_batch_result(results, iso_path, result)
                    counted = True
                interrupted = held.received or result.interrupted
            except KeyboardInterrupt:
                interrupted = True
                with DeferredInterrupt():
                    if counted:
                        pass
                    elif result is not None:
                        _count_batch_result(results, iso_path, result)
                        counted = True
                    elif converter is not None and getattr(converter, "_entered", False):
                        _count_batch_result(results, iso_path, None,
                                            _recorded_cleanup_incomplete(converter))
                        counted = True
            cleanup = (result.cleanup_incomplete if result is not None
                       else _recorded_cleanup_incomplete(converter))
            if interrupted:
                results["interrupted"] = iso_path.name
                results["not_started"] = len(iso_files) - i + (0 if counted else 1)
                stopped_before = not counted
            if interrupted or cleanup:
                try:
                    if interrupted:
                        log(colorize("red", "Batch interrupted by operator — stopping."))
                    if cleanup:
                        log(colorize("red", "Batch stopped: an ffmpeg could not be confirmed "
                                            "stopped and its source stays attached (see the "
                                            "warning above); no further ISO is started."))
                except KeyboardInterrupt:
                    if not results["interrupted"]:
                        results["interrupted"] = iso_path.name
                        results["not_started"] = len(iso_files) - i
                break
    except KeyboardInterrupt:
        # Only in the few steps between those above, once an ISO has been
        # counted and before the next one starts: the batch stops there,
        # and every ISO not counted yet is not started
        if not results["interrupted"]:
            done = results["processed"]
            results["not_started"] = len(iso_files) - done
            if done < len(iso_files):
                results["interrupted"], stopped_before = iso_files[done].name, True
            else:
                results["interrupted"] = "the end of the batch"

    # Summary (a Ctrl-C here only cuts it short)
    try:
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
        if results["interrupted"]:
            where = (f"before {results['interrupted']} started ({results['processed']} of "
                     f"{len(iso_files)} processed)" if stopped_before else
                     f"at {results['interrupted']} ({results['processed']} of {len(iso_files)})")
            log(colorize("red", f"Interrupted by operator (Ctrl-C) {where}; "
                                f"{results['not_started']} ISO(s) not started"))
        if discovery_errors:
            log(colorize("red", f"Search incomplete or ISO refused: {len(discovery_errors)} "
                                f"(not searched or not processed):"))
            for error in discovery_errors:
                log(colorize("red", f"  {error['path']}: {error['detail']}"))
    except KeyboardInterrupt:
        results["interrupted"] = results["interrupted"] or "the batch summary"

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
  fails once promotion has begun (a rename fails partway, or the records
  cannot be written afterwards) keeps the MP4s it promoted: no rollback.
  The log and manifest are written to temporary files and renamed into
  place, log first: an error, a Ctrl-C or a killed process never leaves
  either half-written under its final name. They are not flushed to the
  storage, so a power loss, or a volume that does not rename atomically,
  is not covered. If they cannot be written, the run writes
  *_failed-<token> records instead, saying which final log and manifest
  no longer match its MP4s (or, if those cannot be written either, it
  says so on screen).
  At the overwrite prompt, no answer (end of input) counts as "n".
  If the ISO, a VIDEO_TS file or an output cannot be checked (any error
  other than "no such file"), the run fails instead of treating it as
  missing.
  Exit status: 0 converted, skipped or successful dry run; 1 any failure,
  unverified result or abort, a batch search that was incomplete or
  refused an ISO, or arguments without -i or --batch; 2 an option or
  value rejected by the argument parser; 130 a Ctrl-C stopped the run or
  batch, which takes precedence over 0 and 1 except that an ffmpeg not
  confirmed stopped keeps 1 (see below).

{colorize('yellow', 'CTRL-C:')}
  Ctrl-C ends the current ISO with records of what it did, and a batch
  that has started converting starts no further ISO and shows its
  summary; during the search for ISOs it stops the batch with no
  summary, and at the operator-name prompt nothing starts. An outcome
  already decided (a skip, dry run, unverified result or failure)
  stands, with the interruption as a warning. Before that (checking the
  tools, attaching, analysing or encoding) it fails the run as an
  interrupted encode does; at the overwrite prompt the ISO is recorded
  as aborted. Ctrl-C during the final renames, or while the records are
  written, is held until they finish; once every MP4 is promoted the
  outcome stands, with the interruption as a warning. A Ctrl-C while the
  ISO is being attached or detached is recorded: it may still be
  attached (check hdiutil info); an attach cut short is not detached.

{colorize('yellow', 'IF FFMPEG CANNOT BE CONFIRMED STOPPED:')}
  After a failure or Ctrl-C, ffmpeg is killed and given up to 10 seconds to
  exit. Only once it has exited is its output kept as a .partial (neither
  complete nor playable). If its exit cannot be confirmed, the run fails,
  the batch stops (no further ISO is started), the output keeps its
  temporary name and the ISO is left attached. The warning and the run's
  log and manifest give the process ID, the possible output path, and the
  mount points and devices. Until you have dealt with that process and
  mount, they and the output may still be active: resolve them before
  retrying the ISO or detaching it.

{colorize('yellow', 'DISC TYPES HANDLED:')}
  • {colorize('green', 'DVD-Video (VIDEO_TS):')} Converted to MP4
  • {colorize('yellow', 'VIDEO_TS without VOB files:')} Skipped (IFO/BUP only, or empty: verify the image)
  • {colorize('yellow', 'DVD-Audio (AUDIO_TS):')} Skipped (requires different tools)
  • {colorize('yellow', 'Data discs:')} Skipped (use file copy instead)
  • {colorize('red', 'Analysis errors:')} Failed, nothing encoded (VIDEO_TS unreadable or only partly listed,
    or a VOB, IFO or BUP file that cannot be checked)
  • {colorize('yellow', 'Batch mode:')} skips superseded masters in makeiso.py *_overwritten-* folders (use -i, with -o, for one)
    and lists AppleDouble ._*.iso and makeiso.py *_attempt-*.iso files without converting them (use -i
    for one). Symlinked folders inside the batch folder are not followed (they are listed); an .iso
    symlink to a regular file is converted. A folder that cannot be listed, an entry that cannot be
    checked, an .iso that is not a regular file, or a second ISO for an access folder already used
    is listed, the ISOs found are still converted, and the batch exits 1 unless a Ctrl-C stops it
    (see the exit status above).

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
        # A batch folder that is missing or cannot be checked is reported
        # by the search (find_batch_isos), and the batch exits 1
        batch_dir = Path(args.batch).expanduser()
        
        # Default output is same directory as batch input
        output_base = Path(args.output).expanduser() if args.output else batch_dir

        progress: Dict[str, Any] = {}
        try:
            results = process_batch(
                iso_dir=batch_dir,
                output_dir=output_base,
                operator=operator,
                dry_run=args.dry_run,
                progress=progress,
                crf=args.crf,
                preset=args.preset,
                audio_bitrate=args.audio_bitrate
            )
        except KeyboardInterrupt:
            # A further Ctrl-C: the same exit rule, from what was recorded
            cleanup = (any(d.get("cleanup_incomplete")
                           for d in progress.get("results", {}).get("details", []))
                       or _recorded_cleanup_incomplete(progress.get("converter")))
            log(colorize("red", "\nInterrupted by operator (Ctrl-C): the batch stopped; each "
                                "processed ISO's records give its outcome."
                                + (" An ffmpeg could not be confirmed stopped (see above): "
                                   "exit status 1." if cleanup else "")))
            sys.exit(1 if cleanup else 130)

        # A Ctrl-C that stopped the batch exits 130, unless an ffmpeg could
        # not be confirmed stopped (that keeps 1). Otherwise a failure, an
        # unverified ISO, or a search that was incomplete or refused an ISO
        # exits 1, even when every ISO it found succeeded
        if results.get("interrupted") and not any(
                d.get("cleanup_incomplete") for d in results.get("details", [])):
            sys.exit(130)
        if results["failed"] > 0 or results.get("unverified", 0) > 0 \
                or results.get("discovery_errors"):
            sys.exit(1)
        sys.exit(0)
    
    # Single file mode
    iso_path = Path(args.iso).expanduser()

    # Only "no such file" means not found: a path that cannot be checked
    # stops here, naming the error, before anything is started or recorded
    given, problem = inspect_path(iso_path, follow_symlinks=True)
    if problem:
        log(colorize("red", f"Error: could not check {iso_path}: {problem}"))
        sys.exit(1)
    if given is None:
        log(colorize("red", f"Error: ISO file not found: {iso_path}"))
        sys.exit(1)

    # If a directory was passed, look for a single ISO inside it (macOS
    # AppleDouble ._* companions are never masters, so they do not count)
    if stat.S_ISDIR(given.st_mode):
        try:
            names = sorted(p for p in iso_path.iterdir() if not p.name.startswith("._")
                           and p.suffix.lower() == ".iso")
        except OSError as e:
            log(colorize("red", f"Error: could not list {iso_path}: {e}"))
            sys.exit(1)
        iso_files = []
        for p in names:
            found, problem = inspect_path(p, follow_symlinks=True)
            if problem:
                log(colorize("red", f"Error: could not check {p}: {problem}"))
                sys.exit(1)
            if found is not None and stat.S_ISREG(found.st_mode):
                iso_files.append(p)
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
    try:
        result = converter.convert()
    except KeyboardInterrupt:
        # A further Ctrl-C: the same exit rule, from what the run recorded
        cleanup = _recorded_cleanup_incomplete(converter)
        log(colorize("red", "\nInterrupted by operator (Ctrl-C) while the interruption was being "
                            "recorded: this ISO's records may be missing or incomplete."
                            + (" An ffmpeg could not be confirmed stopped (see above): exit "
                               "status 1." if cleanup else "")))
        sys.exit(1 if cleanup else 130)

    # Exit with appropriate code: 130 when a Ctrl-C stopped the run (its
    # records give the outcome), unless an ffmpeg could not be confirmed
    # stopped (that keeps 1)
    code = 0 if result.success else 1
    try:
        if result.success:
            if result.skipped:
                log(colorize("yellow", f"Skipped: {result.skip_reason}"))
            else:
                log(colorize("green", "Conversion completed successfully!"))
        elif result.unverified:
            log(colorize("yellow", "Unverified: nothing promoted; review copies kept "
                                   f"(not deliverables): {result.unverified_reason}"))
        elif result.aborted:
            log(colorize("yellow", f"Aborted: {result.error_message}"))
        else:
            log(colorize("red", f"Conversion failed: {result.error_message}"))
        if result.interrupted and not result.cleanup_incomplete:
            log(colorize("yellow", "Stopped by Ctrl-C: exit status 130 (the run's records give "
                                   "its outcome)."))
    except KeyboardInterrupt:
        result.interrupted = True
    if result.interrupted and not result.cleanup_incomplete:
        code = 130
    sys.exit(code)

### === SCRIPT ENTRYPOINT ===

if __name__ == "__main__":
    main()