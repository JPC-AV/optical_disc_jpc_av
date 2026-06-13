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
import plistlib
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
class TitleOutput:
    """Outcome of one titleset's encode"""
    vts_number: int
    output_path: Path
    status: str = "not_attempted"   # planned | completed | failed | not_attempted
    output_size: int = 0
    video_duration: Optional[float] = None
    expected_duration: Optional[float] = None
    encode_time: float = 0.0
    md5: Optional[str] = None
    sha256: Optional[str] = None
    source_audio_streams: Optional[int] = None
    source_subtitle_streams: Optional[int] = None
    mapped_video_streams: int = 0
    mapped_audio_streams: int = 0
    error: Optional[str] = None
    partial_path: Optional[Path] = None  # where a failed/interrupted output was quarantined

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

    def __enter__(self) -> Optional[Path]:
        """Mount the ISO and return the most relevant mount point"""
        try:
            # Use hdiutil to mount the ISO
            result = run_cmd([
                "hdiutil", "attach", str(self.iso_path),
                "-readonly", "-nobrowse", "-plist"
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
                        if any(item.is_dir() and item.name.upper() == "VIDEO_TS"
                               for item in candidate.iterdir()):
                            self.mount_point = candidate
                            log(colorize("cyan", f"Using volume containing VIDEO_TS: {candidate}"))
                            break
                    except OSError:
                        continue

            log(colorize("green", f"Mounted ISO at: {self.mount_point}"))
            return self.mount_point

        except subprocess.CalledProcessError as e:
            log(colorize("red", f"Failed to mount ISO: {e}"))
            return None
        except Exception as e:
            log(colorize("red", f"Unexpected error mounting ISO: {e}"))
            return None

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Unmount the ISO (all volumes for multi-session images)"""
        for mount_point in self.mount_points:
            if not mount_point.exists():
                continue  # an earlier detach already released this volume
            try:
                run_cmd(["hdiutil", "detach", str(mount_point), "-force"])
                log(colorize("green", f"Unmounted: {mount_point}"))
            except subprocess.CalledProcessError as e:
                log(colorize("yellow", f"Warning: Failed to unmount cleanly: {e}"))
                # Try with device paths
                for device in self.devices:
                    try:
                        run_cmd(["hdiutil", "detach", device, "-force"])
                    except Exception as fallback_error:
                        log(colorize("yellow", f"Warning: fallback detach of {device} failed: {fallback_error}"))

### === CONTENT ANALYSIS ===

def analyze_iso_content(mount_point: Path) -> DiscContentAnalysis:
    """Analyze the content structure of a mounted ISO"""
    analysis = DiscContentAnalysis()
    analysis.mount_point = mount_point
    
    try:
        # Check for VIDEO_TS folder (case-insensitive search)
        video_ts_path = None
        audio_ts_path = None
        
        for item in mount_point.iterdir():
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
    """Get duration in seconds via ffprobe; target may be a path or a concat: URL"""
    try:
        result = run_cmd([
            "ffprobe",
            "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(target)
        ])
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None

def probe_expected_duration(vobs: List[Path]) -> Optional[float]:
    """Best-effort expected duration of a titleset.

    ffprobe on a concat: VOB input is unreliable — it can report only the
    first segment's duration, which would blind the truncation check. So we
    take the LARGER of two estimates: the concat probe and the sum of
    per-VOB durations. Returns None when neither can be obtained — callers
    warn rather than fail."""
    estimates = []
    concat = "concat:" + "|".join(str(v) for v in vobs)
    concat_duration = get_video_duration(concat)
    if concat_duration:
        estimates.append(concat_duration)
    parts = [get_video_duration(v) for v in vobs]
    if parts and all(p is not None for p in parts):
        estimates.append(sum(parts))
    return max(estimates) if estimates else None

def probe_stream_counts(target) -> Dict[str, int]:
    """Count video/audio/subtitle streams via ffprobe"""
    counts = {"video": 0, "audio": 0, "subtitle": 0}
    for stream in get_video_info(target).get("streams", []):
        codec_type = stream.get("codec_type")
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
        self._failure_token: Optional[str] = None

    def _get_failure_token(self, kind: str = "failed") -> str:
        """One token per run names every failure artifact (quarantined
        partials, failure log, failure manifest), so a failed attempt's
        evidence groups together and retries can never overwrite it"""
        if self._failure_token is None:
            self._failure_token = (f"{kind}-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}"
                                   f"-{uuid4().hex[:6]}")
        return self._failure_token

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
                    token = self._get_failure_token("aborted" if result.aborted else "failed")
                    self._create_error_manifest(result, token)
                    self._create_log_file(result, is_error=True, failure_token=token)
            except Exception as record_error:
                log(colorize("red", f"Could not write run records: {record_error}"))
                if result.success:
                    result.success = False
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

        with ISOMount(self.config.iso_path) as mount_point:
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

                self._log_analysis(analysis)
                
                # Check if this is a VIDEO_TS disc
                if not analysis.has_video_ts or not analysis.vob_files:
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

        for plan in plans:
            title = TitleOutput(vts_number=plan.vts_number, output_path=plan.output_path)
            result.titles.append(title)

            label = "main title" if plan.is_main else f"title {plan.vts_number:02d}"
            log_divider(f"Encoding {label} → {plan.output_path.name}")

            # Source stream inventory (first content VOB is representative)
            source_counts = probe_stream_counts(plan.content_vobs[0])
            title.source_audio_streams = source_counts.get("audio")
            title.source_subtitle_streams = source_counts.get("subtitle")

            # Best-effort expected duration for the truncation check
            title.expected_duration = probe_expected_duration(plan.content_vobs)

            # A failed titleset marks the whole run failed, but remaining
            # titlesets are still attempted so the record is complete.
            # Ctrl-C stops here: the interrupted title is recorded as failed
            # and remaining titlesets stay "not_attempted".
            if self.run:
                self.run.mark(f"encode_vts_{plan.vts_number:02d}")
            try:
                self._encode_titleset(plan, title, result)
            except KeyboardInterrupt:
                result.success = False
                result.interrupted = True
                result.conversion_time += title.encode_time
                log(colorize("red", "Remaining titleset(s) not attempted."))
                break
            result.conversion_time += title.encode_time
            if title.status != "completed":
                result.success = False

        # Mirror the main title into the legacy top-level fields so existing
        # log/manifest consumers keep working
        main = result.titles[0] if result.titles else None
        if main and main.status == "completed":
            result.output_size = main.output_size
            result.video_duration = main.video_duration
            result.md5_mp4 = main.md5
            result.sha256_mp4 = main.sha256
        if not result.success:
            failed = [t for t in result.titles if t.status == "failed"]
            result.error_message = "; ".join(
                f"{t.output_path.name}: {t.error}" for t in failed)

        return result

    def _quarantine_partial(self, path: Path, display_base: Optional[Path] = None) -> Optional[Path]:
        """Rename a partial/failed output so it can't pass for a finished
        access copy. Named from the run's shared failure token (and the final
        output's stem, not the temp name) so all artifacts of one failed
        attempt group together and retries never overwrite evidence."""
        if not path.exists():
            return None
        base = display_base or path
        token = self._get_failure_token()
        marked = path.with_name(f"{base.stem}_{token}.mp4.partial")
        try:
            path.rename(marked)
        except OSError as e:
            # Same filesystem condition that failed the run can block the
            # rename too; leave the temp where it is and point records at it
            log(colorize("yellow", f"Warning: could not quarantine partial {path.name}: {e}"))
            return path
        log(colorize("yellow", f"Partial output renamed to: {marked.name}"))
        return marked

    def _encode_titleset(self, plan: TitlesetPlan, title: TitleOutput,
                         result: ConversionResult):
        """Encode one titleset's content VOBs to MP4 and verify the output"""
        for vob in plan.content_vobs:
            log(colorize("cyan", f"  + {vob.name} ({format_bytes(vob.stat().st_size)})"))

        # Encode to a run-private temp path; the final name is only ever
        # written by an atomic replace() after the output verifies. ffmpeg
        # never opens the final path, so a failed retry cannot damage a
        # pre-existing good file there.
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
            # Run ffmpeg with progress output
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
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
                title.partial_path = self._quarantine_partial(temp_path, plan.output_path)
                return

        except KeyboardInterrupt:
            log(colorize("red", "\nConversion interrupted by user."))
            process.kill()
            process.wait()
            title.encode_time = time.time() - encode_start
            title.status = "failed"
            title.error = "Interrupted by operator (Ctrl-C)"
            title.partial_path = self._quarantine_partial(temp_path, plan.output_path)
            raise
        except Exception as e:
            title.encode_time = time.time() - encode_start
            title.status = "failed"
            title.error = str(e)
            title.partial_path = self._quarantine_partial(temp_path, plan.output_path)
            return

        # Post-encode verification and promotion share the encode's failure
        # handling: any error here fails the title, quarantines the temp,
        # and flows into the normal failure records (it must never escape
        # the record machinery).
        stage = "verification"
        try:
            if not temp_path.exists():
                title.status = "failed"
                title.error = "Output file was not created"
                return

            title.output_size = temp_path.stat().st_size
            title.video_duration = get_video_duration(temp_path)

            # Record what was actually mapped (closes the loop on stream loss)
            output_counts = probe_stream_counts(temp_path)
            title.mapped_video_streams = output_counts.get("video", 0)
            title.mapped_audio_streams = output_counts.get("audio", 0)
            if title.source_audio_streams and title.mapped_audio_streams < title.source_audio_streams:
                result.warnings.append(
                    f"{plan.output_path.name}: only {title.mapped_audio_streams} of "
                    f"{title.source_audio_streams} source audio stream(s) mapped")

            # Duration sanity check (best-effort; warns, never fails the run)
            if title.expected_duration and title.video_duration:
                if title.video_duration < title.expected_duration * 0.95:
                    result.warnings.append(
                        f"{plan.output_path.name}: output duration "
                        f"{format_timecode(title.video_duration)} is >5% shorter than expected "
                        f"{format_timecode(title.expected_duration)} — possible truncation")
            elif title.expected_duration is None:
                result.warnings.append(
                    f"{plan.output_path.name}: duration comparison unavailable (source unprobeable)")

            # Calculate checksums (MD5 + SHA-256 in one read)
            stage = "checksum"
            if self.run:
                self.run.mark(f"checksum_vts_{plan.vts_number:02d}")
            log(colorize("cyan", "Calculating MD5 and SHA-256 of output file..."))
            title.md5, title.sha256 = calculate_checksums(temp_path)
            if title.md5 is None:
                result.warnings.append(
                    f"{plan.output_path.name}: checksum calculation failed — no fixity recorded")

            # Everything verified — atomically promote the temp onto the final
            # name (replaces an operator-approved overwrite target in one step)
            stage = "promotion"
            temp_path.replace(plan.output_path)

        except KeyboardInterrupt:
            title.status = "failed"
            title.error = f"Interrupted by operator (Ctrl-C) during post-encode {stage}"
            title.partial_path = self._quarantine_partial(temp_path, plan.output_path)
            raise
        except Exception as e:
            title.status = "failed"
            title.error = f"post-encode {stage} failed: {e}"
            log(colorize("red", f"{plan.output_path.name}: {title.error}"))
            title.partial_path = self._quarantine_partial(temp_path, plan.output_path)
            return

        title.status = "completed"
        log(colorize("green", f"{plan.output_path.name} complete "
                              f"({format_bytes(title.output_size)}, "
                              f"{format_timecode(title.video_duration) if title.video_duration else 'duration n/a'}, "
                              f"{title.mapped_audio_streams} audio track(s))"))
    
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
                                     f"{t.mapped_audio_streams} audio track(s)"))
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

        Failure/aborted logs are written under the run's failure token so a
        failed retry never overwrites the log a prior success manifest
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
                f.write(f"ISO TO MP4 ACCESS COPY CONVERSION LOG ({'ABORTED' if result.aborted else 'ERROR'})\n")
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
            else:
                status = 'FAILED'
            f.write(f"Status:           {status}\n")
            if result.skip_reason:
                f.write(f"Skip Reason:      {result.skip_reason}\n")
            if result.error_message:
                f.write(f"Error:            {result.error_message}\n")
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
            f.write(f"MP4 File:         {result.mp4_path.name if result.mp4_path else 'N/A (conversion did not complete)'}\n")
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
                        f.write(f" (expected ~{format_timecode(t.expected_duration)})")
                    f.write("\n")
                    f.write(f"  Audio:    {t.mapped_audio_streams} of {t.source_audio_streams if t.source_audio_streams is not None else '?'} source track(s) mapped\n")
                    f.write(f"  MD5:      {t.md5}\n")
                    f.write(f"  SHA-256:  {t.sha256}\n")
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
                     "(DVD bitmap subtitles are not MP4-compatible)."),
            "outputs": [
                {
                    "vts_number": t.vts_number,
                    "filename": t.output_path.name,
                    "is_main": i == 0,
                    "status": t.status,
                    "size_bytes": t.output_size,
                    "duration_seconds": t.video_duration,
                    "expected_duration_seconds": t.expected_duration,
                    "encode_time_seconds": t.encode_time,
                    "md5_checksum": t.md5,
                    "sha256_checksum": t.sha256,
                    "source_audio_streams": t.source_audio_streams,
                    "source_subtitle_streams": t.source_subtitle_streams,
                    "mapped_audio_streams": t.mapped_audio_streams,
                    "final_intended_path": str(t.output_path),
                    "partial_renamed_to": str(t.partial_path) if t.partial_path else None,
                    "error": t.error
                } for i, t in enumerate(result.titles)
            ]
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
                "mp4_created": result.mp4_path is not None and result.mp4_path.exists() if result.mp4_path else False,
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
        """Create manifest for failed or operator-aborted conversions.

        Carries the full per-titleset record so a mixed run (one title
        completed, another failed) loses nothing machine-readable. Written
        under the run's failure token so retries never overwrite prior
        evidence (the success manifest or earlier failures)."""
        now = datetime.datetime.now()
        status = "aborted" if result.aborted else "error"

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
                "error_message": result.error_message
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
                         "This ISO encountered an error during processing"),
                "project": "Johnson Publishing Company Archive (JPCA)"
            }
        }

        manifest_path = self.config.output_dir / f"{self.config.iso_name}_{failure_token}_manifest.json"
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=False)

        log(colorize("cyan", f"{'Aborted' if result.aborted else 'Error'} manifest saved to: {manifest_path}"))
    
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

def process_batch(iso_dir: Path, output_dir: Path, operator: str, 
                  dry_run: bool = False, **encoding_kwargs) -> Dict[str, Any]:
    """Process multiple ISOs in a directory"""
    
    # Find all ISO files (case-insensitive, avoids duplicates on macOS)
    iso_files = sorted(p for p in iso_dir.rglob("*") if p.suffix.lower() == ".iso")
    
    if not iso_files:
        log(colorize("yellow", f"No ISO files found in: {iso_dir}"))
        return {"processed": 0, "success": 0, "skipped": 0, "failed": 0}
    
    log_divider("Batch Processing")
    log(colorize("cyan", f"Found {len(iso_files)} ISO files"))
    
    results = {
        "processed": 0,
        "success": 0,
        "would_convert": 0,  # Dry-run VIDEO_TS discs
        "skipped": 0,        # Non-VIDEO_TS discs
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
        
        if result.skipped:
            # Differentiate dry-run skips (VIDEO_TS that would convert) from actual skips
            if result.skip_reason == "Dry run mode":
                results["would_convert"] += 1
            else:
                results["skipped"] += 1
        elif result.success:
            results["success"] += 1
        else:
            results["failed"] += 1
        
        results["details"].append({
            "iso": iso_path.name,
            "success": result.success,
            "skipped": result.skipped,
            "aborted": result.aborted,
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

{colorize('yellow', 'DISC TYPES HANDLED:')}
  • {colorize('green', 'DVD-Video (VIDEO_TS):')} Converted to MP4
  • {colorize('yellow', 'DVD-Audio (AUDIO_TS):')} Skipped (requires different tools)
  • {colorize('yellow', 'Data discs:')} Skipped (use file copy instead)

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
        
        if results["failed"] > 0:
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
    elif result.aborted:
        log(colorize("yellow", f"Aborted: {result.error_message}"))
        sys.exit(1)
    else:
        log(colorize("red", f"Conversion failed: {result.error_message}"))
        sys.exit(1)

### === SCRIPT ENTRYPOINT ===

if __name__ == "__main__":
    main()