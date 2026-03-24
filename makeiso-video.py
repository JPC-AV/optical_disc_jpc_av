#!/usr/bin/env python3

"""
makeiso-video.py - ISO to MP4 Access Copy Utility
Extracts VIDEO_TS content from ISO files and converts to MP4 access copies.
Part of the Johnson Publishing Company Archive (JPCA) optical disc digitization workflow.

A collaboration between the Getty Research Institute and the 
Smithsonian National Museum of African American History and Culture.
"""

import subprocess
import os
import sys
import datetime
import time
import hashlib
import re
import platform
import argparse
import json
import logging
import tempfile
import shutil
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
    def access_dir_name(self) -> str:
        """Generate access directory name: access_JPC_AV_<id>"""
        # Extract ID from filename (assumes format like JPC_AV_00001 or similar)
        # If filename already starts with JPC_AV_, use as-is; otherwise prefix
        name = self.iso_name
        if name.upper().startswith("JPC_AV_"):
            return f"access_{name}"
        else:
            return f"access_JPC_AV_{name}"
    
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
    error_message: Optional[str] = None
    disc_analysis: Optional[DiscContentAnalysis] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    
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

def colorize(color: str, text: str) -> str:
    """Apply ANSI color to text"""
    return f"{COLOR[color]}{text}{COLOR['off']}"

### === LOGGING SETUP ===

class MemoryLogHandler(logging.Handler):
    """Log handler that stores messages in memory for later file output"""
    def __init__(self):
        super().__init__()
        self.buffer: List[str] = []
    
    def emit(self, record):
        now = datetime.datetime.now()
        timestamp = now.strftime('%Y-%m-%dT%H:%M:%S') + f".{now.microsecond // 1000:03d}"
        msg = self.format(record)
        self.buffer.append(f"{timestamp} - {msg}")
    
    def clear(self):
        """Clear the buffer"""
        self.buffer.clear()

def setup_logging() -> Tuple[logging.Logger, MemoryLogHandler]:
    """Setup dual logging - console and memory buffer"""
    logger = logging.getLogger("iso_to_mp4")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    
    # Console handler
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(stream_handler)
    
    # Memory handler for clean file output
    mem_handler = MemoryLogHandler()
    mem_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(mem_handler)
    
    return logger, mem_handler

# Global logger instance
logger, mem_handler = setup_logging()

def log(msg: str):
    """Log a message to console and memory buffer"""
    logger.info(msg)

def log_to_file_only(msg: str):
    """Log a message only to the log file (memory buffer), not console"""
    now = datetime.datetime.now()
    timestamp = now.strftime('%Y-%m-%dT%H:%M:%S') + f".{now.microsecond // 1000:03d}"
    mem_handler.buffer.append(f"{timestamp} - {msg}")

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

def calculate_md5(filepath: Path) -> Optional[str]:
    """Calculate MD5 hash of a file. Returns None on error."""
    try:
        hasher = hashlib.md5()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):  # 4MB chunks
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        log(colorize("yellow", f"Warning: MD5 calculation failed: {e}"))
        return None

### === ISO MOUNTING ===

class ISOMount:
    """Context manager for mounting ISO files"""
    
    def __init__(self, iso_path: Path):
        self.iso_path = iso_path
        self.mount_point: Optional[Path] = None
        self.device: Optional[str] = None
    
    def __enter__(self) -> Optional[Path]:
        """Mount the ISO and return mount point"""
        try:
            # Use hdiutil to mount the ISO
            result = run_cmd([
                "hdiutil", "attach", str(self.iso_path),
                "-readonly", "-nobrowse", "-plist"
            ])
            
            # Parse plist output to get mount point
            plist_data = plistlib.loads(result.stdout.encode())
            
            for entity in plist_data.get("system-entities", []):
                mount_point = entity.get("mount-point")
                if mount_point:
                    self.mount_point = Path(mount_point)
                    self.device = entity.get("dev-entry")
                    log(colorize("green", f"Mounted ISO at: {self.mount_point}"))
                    return self.mount_point
            
            log(colorize("red", "Failed to find mount point in hdiutil output"))
            return None
            
        except subprocess.CalledProcessError as e:
            log(colorize("red", f"Failed to mount ISO: {e}"))
            return None
        except Exception as e:
            log(colorize("red", f"Unexpected error mounting ISO: {e}"))
            return None
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Unmount the ISO"""
        if self.mount_point:
            try:
                run_cmd(["hdiutil", "detach", str(self.mount_point), "-force"])
                log(colorize("green", f"Unmounted: {self.mount_point}"))
            except subprocess.CalledProcessError as e:
                log(colorize("yellow", f"Warning: Failed to unmount cleanly: {e}"))
                # Try with device path
                if self.device:
                    try:
                        run_cmd(["hdiutil", "detach", self.device, "-force"])
                    except:
                        pass

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

def get_video_duration(video_path: Path) -> Optional[float]:
    """Get duration of a video file in seconds using ffprobe"""
    try:
        result = run_cmd([
            "ffprobe",
            "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path)
        ])
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None

def get_video_info(video_path: Path) -> Dict[str, Any]:
    """Get detailed video information using ffprobe"""
    try:
        result = run_cmd([
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(video_path)
        ])
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return {}

### === MAIN CONVERTER CLASS ===

class ISOToMP4Converter:
    """Main class for ISO to MP4 conversion operations"""
    
    def __init__(self, config: ConversionConfig):
        self.config = config
        self.start_time: Optional[datetime.datetime] = None
    
    def convert(self) -> ConversionResult:
        """Main entry point for conversion"""
        self.start_time = datetime.datetime.now()
        mem_handler.clear()
        
        # If a directory was passed, look for a single ISO inside it
        if self.config.iso_path.is_dir():
            iso_files = list(self.config.iso_path.glob("*.iso"))
            if len(iso_files) == 1:
                log(colorize("yellow", f"Directory provided — using ISO found inside: {iso_files[0].name}"))
                self.config.iso_path = iso_files[0]
            elif len(iso_files) == 0:
                return ConversionResult(
                    success=False,
                    error_message=f"Directory provided but no .iso file found inside: {self.config.iso_path}"
                )
            else:
                iso_list = ", ".join(f.name for f in iso_files)
                return ConversionResult(
                    success=False,
                    error_message=f"Directory provided but multiple .iso files found — please specify one directly: {iso_list}"
                )

        # Validate ISO exists
        if not self.config.iso_path.exists():
            return ConversionResult(
                success=False,
                error_message=f"ISO file not found: {self.config.iso_path}"
            )
        
        # Check for ffmpeg
        ffmpeg_available, ffmpeg_version = check_ffmpeg()
        if not ffmpeg_available:
            return ConversionResult(
                success=False,
                error_message="ffmpeg not found. Please install ffmpeg."
            )
        log(colorize("cyan", f"Using ffmpeg version: {ffmpeg_version}"))
        
        # Get ISO size
        iso_size = self.config.iso_path.stat().st_size
        log(colorize("cyan", f"ISO size: {format_bytes(iso_size)}"))
        
        # Mount and analyze ISO
        log_divider("Mounting ISO")
        result = None
        
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
                
                # Check for existing output (skip in dry-run mode - nothing will be written)
                elif not self.config.dry_run and self.config.mp4_path.exists() and not self._confirm_overwrite():
                    result = ConversionResult(
                        success=False,
                        iso_path=self.config.iso_path,
                        source_size=iso_size,
                        disc_analysis=analysis,
                        error_message="Aborted to avoid overwrite"
                    )
                
                # Perform conversion
                elif self.config.dry_run:
                    log(colorize("cyan", "Dry run:") + " " + 
                        colorize("yellow", "Would convert VIDEO_TS to MP4"))
                    result = ConversionResult(
                        success=True,
                        mp4_path=self.config.mp4_path,
                        iso_path=self.config.iso_path,
                        source_size=iso_size,
                        disc_analysis=analysis,
                        skipped=True,
                        skip_reason="Dry run mode"
                    )
                else:
                    # Create output directory (only for actual conversion)
                    self.config.output_dir.mkdir(parents=True, exist_ok=True)
                    result = self._perform_conversion(analysis, iso_size)
        
        # Generate outputs (now guaranteed to run for all paths)
        if result.success and not result.skipped:
            self._generate_summary(result)
            self._create_log_file(result)
            self._create_manifest(result)
        elif result.skipped and not result.error_message:
            self._create_skip_manifest(result)
            if self.config.dry_run:
                self._create_log_file(result, dry_run=True)
        elif result.error_message:
            self._create_error_manifest(result)
            self._create_log_file(result, is_error=True)
        
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
                log_to_file_only(f"  VOB: {vob.name} ({format_bytes(vob.stat().st_size)})")
        
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
    
    def _confirm_overwrite(self) -> bool:
        """Confirm file overwrite"""
        response = get_input(colorize("yellow", 
            f"{self.config.mp4_path} exists. Overwrite? (y/n): "))
        return response.lower() == 'y'
    
    def _perform_conversion(self, analysis: DiscContentAnalysis, iso_size: int) -> ConversionResult:
        """Perform the actual VIDEO_TS to MP4 conversion"""
        log_divider("Converting VIDEO_TS to MP4")
        
        video_ts_path = analysis.mount_point / "VIDEO_TS"
        
        # Build ffmpeg command
        # Use concat demuxer with VIDEO_TS folder input
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",  # Overwrite output
            "-i", f"concat:{self._get_vob_concat_string(analysis.vob_files)}",
            "-c:v", self.config.video_codec,
            "-preset", self.config.preset,
            "-crf", str(self.config.crf),
            "-c:a", self.config.audio_codec,
            "-b:a", self.config.audio_bitrate,
            "-movflags", "+faststart",  # Enable streaming
            "-pix_fmt", "yuv420p",  # Compatibility
            str(self.config.mp4_path)
        ]
        
        log(colorize("cyan", "Running ffmpeg conversion..."))
        log(colorize("cyan", "Command:") + " " + colorize("yellow", " ".join(ffmpeg_cmd)))
        log(colorize("cyan", "Encoding settings:"))
        log(colorize("cyan", f"  Video codec: {self.config.video_codec}"))
        log(colorize("cyan", f"  CRF: {self.config.crf} (lower = higher quality)"))
        log(colorize("cyan", f"  Preset: {self.config.preset}"))
        log(colorize("cyan", f"  Audio codec: {self.config.audio_codec}"))
        log(colorize("cyan", f"  Audio bitrate: {self.config.audio_bitrate}"))
        
        conversion_start = time.time()
        
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
            last_timecode = "00:00:00.00"
            
            while True:
                line = process.stderr.readline()
                if not line and process.poll() is not None:
                    break
                
                stderr_output.append(line)
                
                # Parse timecode from ffmpeg output (format: time=HH:MM:SS.ss)
                match = re.search(r'time=(\d+:\d+:\d+\.\d+)', line)
                if match:
                    last_timecode = match.group(1)
                    elapsed = time.time() - conversion_start
                    # Update progress display (overwrite same line)
                    sys.stdout.write(f"\r{colorize('cyan', 'Encoding:')} {last_timecode}  |  {colorize('cyan', 'Elapsed:')} {format_duration(elapsed)}    ")
                    sys.stdout.flush()
            
            # Clear progress line and move to new line
            sys.stdout.write("\r" + " " * 80 + "\r")
            sys.stdout.flush()
            
            stderr_text = "".join(stderr_output)
            
            if process.returncode != 0:
                log(colorize("red", f"ffmpeg error:\n{stderr_text}"))
                return ConversionResult(
                    success=False,
                    iso_path=self.config.iso_path,
                    source_size=iso_size,
                    disc_analysis=analysis,
                    error_message=f"ffmpeg failed with return code {process.returncode}"
                )
            
            conversion_time = time.time() - conversion_start
            
        except KeyboardInterrupt:
            log(colorize("red", "\nConversion interrupted by user."))
            process.kill()
            raise
        except Exception as e:
            return ConversionResult(
                success=False,
                iso_path=self.config.iso_path,
                source_size=iso_size,
                disc_analysis=analysis,
                error_message=str(e)
            )
        
        # Verify output exists
        if not self.config.mp4_path.exists():
            return ConversionResult(
                success=False,
                iso_path=self.config.iso_path,
                source_size=iso_size,
                disc_analysis=analysis,
                error_message="Output file was not created"
            )
        
        output_size = self.config.mp4_path.stat().st_size
        video_duration = get_video_duration(self.config.mp4_path)
        
        log(colorize("green", "Conversion complete!"))
        log(colorize("cyan", f"Output size: {format_bytes(output_size)}"))
        log(colorize("cyan", f"Conversion time: {format_duration(conversion_time)}"))
        
        if video_duration:
            log(colorize("cyan", f"Video duration: {format_timecode(video_duration)}"))
        
        # Calculate MD5
        log_divider("Calculating Checksum")
        log(colorize("cyan", "Calculating MD5 hash of output file..."))
        md5_hash = calculate_md5(self.config.mp4_path)
        log(colorize("cyan", f"MD5: {md5_hash}"))
        
        return ConversionResult(
            success=True,
            mp4_path=self.config.mp4_path,
            iso_path=self.config.iso_path,
            source_size=iso_size,
            output_size=output_size,
            video_duration=video_duration,
            conversion_time=conversion_time,
            md5_mp4=md5_hash,
            disc_analysis=analysis
        )
    
    def _get_vob_concat_string(self, vob_files: List[Path]) -> str:
        """Build concat string for ffmpeg from VOB files"""
        # Filter to only include main content VOBs (VTS_01_*.VOB typically)
        # Skip menu VOBs (VIDEO_TS.VOB, VTS_*_0.VOB)
        content_vobs = []
        skipped_vobs = []
        
        for vob in vob_files:
            name_upper = vob.name.upper()
            # Skip VIDEO_TS.VOB (menu) and files ending in _0.VOB (title menus)
            if name_upper == "VIDEO_TS.VOB":
                skipped_vobs.append((vob, "menu VOB"))
                continue
            if re.match(r'VTS_\d+_0\.VOB', name_upper):
                skipped_vobs.append((vob, "title menu VOB"))
                continue
            content_vobs.append(vob)
        
        # Log skipped VOBs so operators can verify
        if skipped_vobs:
            log(colorize("yellow", f"Skipping {len(skipped_vobs)} menu VOB(s):"))
            for vob, reason in skipped_vobs:
                log(colorize("yellow", f"  - {vob.name} ({reason}, {format_bytes(vob.stat().st_size)})"))
        
        log(colorize("cyan", f"Including {len(content_vobs)} content VOB(s):"))
        for vob in content_vobs:
            log(colorize("cyan", f"  + {vob.name} ({format_bytes(vob.stat().st_size)})"))
        
        if not content_vobs:
            # Fall back to all VOBs if filtering removed everything
            log(colorize("yellow", "Warning: No content VOBs found after filtering, using all VOBs"))
            content_vobs = vob_files
        
        return "|".join(str(vob) for vob in content_vobs)
    
    def _generate_summary(self, result: ConversionResult):
        """Generate timing and summary information"""
        log_divider("Summary")
        
        log(colorize("cyan", f"Source ISO: {result.iso_path.name}"))
        log(colorize("cyan", f"Output MP4: {result.mp4_path.name if result.mp4_path else 'N/A'}"))
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
        log(colorize("green", "Conversion completed successfully!"))
    
    def _create_log_file(self, result: ConversionResult, dry_run: bool = False, is_error: bool = False):
        """Create formatted log file"""
        if dry_run:
            log_path = self.config.dry_run_log_path
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
                f.write("ISO TO MP4 ACCESS COPY CONVERSION LOG (ERROR)\n")
            else:
                f.write("ISO TO MP4 ACCESS COPY CONVERSION LOG\n")
            f.write("=" * 70 + "\n\n")
            f.write("CONVERSION SUMMARY\n")
            f.write("-" * 20 + "\n")
            f.write(f"Run ID:           {datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}_{self.config.operator}\n")
            f.write(f"Operator:         {self.config.operator}\n")
            f.write(f"Start Time:       {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"End Time:         {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Total Duration:   {format_duration(total_duration)}\n")
            f.write(f"Status:           {'SUCCESS' if result.success else 'FAILED'}\n\n")
            
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
            
            f.write(f"MD5 Checksum:     {result.md5_mp4}\n\n")
            
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
    
    def _create_manifest(self, result: ConversionResult):
        """Create comprehensive JSON manifest"""
        now = datetime.datetime.now()
        
        # Get ffmpeg version
        _, ffmpeg_version = check_ffmpeg()
        
        # Get video info
        video_info = get_video_info(result.mp4_path) if result.mp4_path else {}
        
        manifest = {
            "conversion_metadata": {
                "run_id": f"{now.strftime('%Y%m%dT%H%M%S')}_{self.config.operator}",
                "uuid": str(uuid4()),
                "tool_name": "ISO to MP4 Access Copy Utility",
                "tool_version": "v1.0",
                "created": now.isoformat(timespec="seconds"),
                "operator": self.config.operator,
                "dry_run": self.config.dry_run
            },
            
            "conversion_status": {
                "overall_status": "success" if result.success else "failed",
                "mp4_created": result.mp4_path is not None and result.mp4_path.exists() if result.mp4_path else False,
                "skipped": result.skipped,
                "skip_reason": result.skip_reason,
                "errors": result.error_message
            },
            
            "source_iso": {
                "filename": result.iso_path.name if result.iso_path else None,
                "full_path": str(result.iso_path) if result.iso_path else None,
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
                "full_path": str(result.mp4_path) if result.mp4_path else None,
                "size_bytes": result.output_size,
                "size_formatted": format_bytes(result.output_size),
                "duration_seconds": result.video_duration,
                "duration_formatted": format_timecode(result.video_duration) if result.video_duration else None,
                "md5_checksum": result.md5_mp4,
                "compression_ratio": result.compression_ratio
            },
            
            "encoding_settings": {
                "video_codec": self.config.video_codec,
                "crf_quality": self.config.crf,
                "preset": self.config.preset,
                "audio_codec": self.config.audio_codec,
                "audio_bitrate": self.config.audio_bitrate,
                "pixel_format": "yuv420p",
                "movflags": "+faststart"
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
        """Create manifest for skipped files"""
        now = datetime.datetime.now()
        
        manifest = {
            "conversion_metadata": {
                "run_id": f"{now.strftime('%Y%m%dT%H%M%S')}_{self.config.operator}",
                "uuid": str(uuid4()),
                "tool_name": "ISO to MP4 Access Copy Utility",
                "tool_version": "v1.0",
                "created": now.isoformat(timespec="seconds"),
                "operator": self.config.operator
            },
            
            "conversion_status": {
                "overall_status": "skipped",
                "skipped": True,
                "skip_reason": result.skip_reason
            },
            
            "source_iso": {
                "filename": result.iso_path.name if result.iso_path else None,
                "full_path": str(result.iso_path) if result.iso_path else None,
                "size_bytes": result.source_size,
                "size_formatted": format_bytes(result.source_size)
            },
            
            "disc_analysis": {
                "disc_type": result.disc_analysis.disc_type if result.disc_analysis else None,
                "has_video_ts": result.disc_analysis.has_video_ts if result.disc_analysis else False,
                "has_audio_ts": result.disc_analysis.has_audio_ts if result.disc_analysis else False,
                "recommendation": self._get_skip_recommendation(result.disc_analysis)
            },
            
            "archival_notes": {
                "note": result.skip_reason if result.skip_reason else "Skipped",
                "project": "Johnson Publishing Company Archive (JPCA)"
            }
        }
        
        # Save to a skip manifest file
        skip_manifest_path = self.config.output_dir / f"{self.config.iso_name}_skip_manifest.json"
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        
        with open(skip_manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=False)
        
        log(colorize("cyan", f"Skip manifest saved to: {skip_manifest_path}"))
    
    def _create_error_manifest(self, result: ConversionResult):
        """Create manifest for failed conversions"""
        now = datetime.datetime.now()
        
        manifest = {
            "conversion_metadata": {
                "run_id": f"{now.strftime('%Y%m%dT%H%M%S')}_{self.config.operator}",
                "uuid": str(uuid4()),
                "tool_name": "ISO to MP4 Access Copy Utility",
                "tool_version": "v1.0",
                "created": now.isoformat(timespec="seconds"),
                "operator": self.config.operator
            },
            
            "conversion_status": {
                "overall_status": "error",
                "skipped": False,
                "error_message": result.error_message
            },
            
            "source_iso": {
                "filename": result.iso_path.name if result.iso_path else None,
                "full_path": str(result.iso_path) if result.iso_path else None,
                "size_bytes": result.source_size,
                "size_formatted": format_bytes(result.source_size)
            },
            
            "disc_analysis": {
                "disc_type": result.disc_analysis.disc_type if result.disc_analysis else None,
                "has_video_ts": result.disc_analysis.has_video_ts if result.disc_analysis else False,
                "has_audio_ts": result.disc_analysis.has_audio_ts if result.disc_analysis else False,
                "error": result.disc_analysis.error if result.disc_analysis else None
            },
            
            "archival_notes": {
                "note": "This ISO encountered an error during processing",
                "project": "Johnson Publishing Company Archive (JPCA)"
            }
        }
        
        error_manifest_path = self.config.output_dir / f"{self.config.iso_name}_error_manifest.json"
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        
        with open(error_manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=False)
        
        log(colorize("cyan", f"Error manifest saved to: {error_manifest_path}"))
    
    def _get_skip_recommendation(self, analysis: Optional[DiscContentAnalysis]) -> str:
        """Get recommendation for handling skipped discs"""
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
        
        # Build access directory name
        iso_name = iso_path.stem
        if iso_name.upper().startswith("JPC_AV_"):
            access_dir_name = f"access_{iso_name}"
        else:
            access_dir_name = f"access_JPC_AV_{iso_name}"
        
        # Create config for this ISO
        # Place access folder next to the source ISO, not at batch root
        config = ConversionConfig(
            iso_path=iso_path,
            output_dir=iso_path.parent / access_dir_name,
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
            "error": result.error_message,
            "skip_reason": result.skip_reason
        })
    
    # Summary
    log_divider("Batch Summary")
    log(colorize("cyan", f"Total processed: {results['processed']}"))
    if dry_run:
        log(colorize("green", f"Would convert: {results['would_convert']}"))
    else:
        log(colorize("green", f"Successfully converted: {results['success']}"))
    if results["skipped"] > 0:
        log(colorize("yellow", f"Skipped (non-VIDEO_TS): {results['skipped']}"))
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
        iso_files = list(iso_path.glob("*.iso"))
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
    
    # Determine output directory
    # Default: same directory as ISO, in access_JPC_AV_<id>/ subfolder
    if args.output:
        output_base = Path(args.output).expanduser()
    else:
        output_base = iso_path.parent
    
    # Build the access directory name
    iso_name = iso_path.stem
    if iso_name.upper().startswith("JPC_AV_"):
        access_dir_name = f"access_{iso_name}"
    else:
        access_dir_name = f"access_JPC_AV_{iso_name}"
    
    output_dir = output_base / access_dir_name
    
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
    else:
        log(colorize("red", f"Conversion failed: {result.error_message}"))
        sys.exit(1)

### === SCRIPT ENTRYPOINT ===

if __name__ == "__main__":
    main()