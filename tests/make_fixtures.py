#!/usr/bin/env python3

"""
make_fixtures.py - Synthetic DVD ISO fixtures for testing makeiso-video.py

Generates tiny MPEG-2 VOB files with ffmpeg, lays them out as VIDEO_TS
structures, and packs each into an ISO image.

*** macOS ONLY: ISO packing uses `hdiutil makehybrid`, which exists only on
*** macOS. The rest of the toolkit is macOS-specific too, so this is by
*** design — but if this script fails on Linux/Windows, that's why.

Fixtures produced (in --out, default tests/fixtures/):
  single_title.iso   VIDEO_TS with one titleset  (VTS_01_1/2 + menus)
  multi_title.iso    VIDEO_TS with two titlesets (VTS_01_*, VTS_02_*)
  menu_only.iso      VIDEO_TS containing only menu VOBs (no content)
  data_only.iso      no VIDEO_TS at all (plain data files)
  broken_title2.iso  VTS_01 valid, VTS_02 is garbage bytes — exercises the
                     mixed-failure path (title 1 completes, title 2 fails)

Content VOBs carry one video stream and TWO audio streams (440 Hz and
880 Hz sine), so stream-mapping behavior is testable.

Usage:
  python3 tests/make_fixtures.py [--out DIR] [--duration SECONDS]

Requires: ffmpeg (Homebrew), hdiutil (macOS built-in). No sudo needed.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd):
    """Run a command, failing loudly with its stderr on error"""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"FAILED: {' '.join(str(c) for c in cmd)}\n{proc.stderr}")
    return proc


def make_vob(path: Path, duration: float, label: str):
    """Generate a small MPEG-2 program-stream VOB with 2 audio tracks"""
    run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=duration={duration}:size=320x240:rate=25",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration}",
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-c:v", "mpeg2video", "-b:v", "1000k",
        "-c:a", "mp2", "-b:a", "128k",
        "-metadata", f"title={label}",
        "-f", "vob", str(path),
    ])


def make_iso(src_dir: Path, iso_path: Path, volume_name: str):
    """Pack a directory into an ISO (macOS hdiutil)"""
    iso_path.unlink(missing_ok=True)
    # hdiutil appends .iso itself when using -iso with an extensionless -o;
    # pass the path without suffix to control the final name exactly.
    run([
        "hdiutil", "makehybrid", "-iso", "-joliet",
        "-default-volume-name", volume_name,
        "-o", str(iso_path), str(src_dir),
    ])


def build_fixtures(out_dir: Path, duration: float):
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # Shared tiny VOBs (content ~duration s, menus shorter)
        content_vob = tmp / "content.VOB"
        menu_vob = tmp / "menu.VOB"
        make_vob(content_vob, duration, "content")
        make_vob(menu_vob, max(1.0, duration / 4), "menu")

        def lay_out(name: str, vob_map: dict, extra_files: dict = None):
            """Create a disc dir; vob_map: VIDEO_TS filename -> source vob (or None for no VIDEO_TS)"""
            disc = tmp / name
            if vob_map:
                vts = disc / "VIDEO_TS"
                vts.mkdir(parents=True)
                for fname, src in vob_map.items():
                    shutil.copy(src, vts / fname)
                    # Minimal IFO/BUP companions for realism
                    if fname.endswith("_1.VOB") or fname == "VIDEO_TS.VOB":
                        stem = fname.rsplit("_", 1)[0] if fname != "VIDEO_TS.VOB" else "VIDEO_TS"
                        (vts / f"{stem}_0.IFO").write_bytes(b"DVDVIDEO-VTS fixture stub")
                        (vts / f"{stem}_0.BUP").write_bytes(b"DVDVIDEO-VTS fixture stub")
            else:
                disc.mkdir(parents=True)
            for fname, content in (extra_files or {}).items():
                (disc / fname).write_text(content)
            return disc

        fixtures = {
            "single_title": lay_out("single_title", {
                "VIDEO_TS.VOB": menu_vob,     # disc menu
                "VTS_01_0.VOB": menu_vob,     # title menu
                "VTS_01_1.VOB": content_vob,  # content part 1
                "VTS_01_2.VOB": content_vob,  # content part 2
            }),
            "multi_title": lay_out("multi_title", {
                "VIDEO_TS.VOB": menu_vob,
                "VTS_01_0.VOB": menu_vob,
                "VTS_01_1.VOB": content_vob,
                "VTS_02_0.VOB": menu_vob,
                "VTS_02_1.VOB": content_vob,
                "VTS_02_2.VOB": content_vob,
            }),
            "menu_only": lay_out("menu_only", {
                "VIDEO_TS.VOB": menu_vob,
                "VTS_01_0.VOB": menu_vob,
            }),
            "data_only": lay_out("data_only", {}, extra_files={
                "readme.txt": "fixture data disc — no VIDEO_TS\n",
                "photo_list.csv": "id,name\n1,test\n",
            }),
            "broken_title2": lay_out("broken_title2", {
                "VTS_01_1.VOB": content_vob,
            }),
        }

        # broken_title2: VTS_02 is garbage so ffmpeg fails on it after VTS_01
        # succeeds — the mixed-failure regression case
        broken_vts = tmp / "broken_title2" / "VIDEO_TS"
        (broken_vts / "VTS_02_1.VOB").write_bytes(os.urandom(512 * 1024))

        for name, disc_dir in fixtures.items():
            iso = out_dir / f"{name}.iso"
            make_iso(disc_dir, iso, name.upper())
            print(f"built {iso}  ({iso.stat().st_size // 1024} KB)")


def main():
    if sys.platform != "darwin":
        sys.exit("This fixture builder is macOS-only (uses hdiutil makehybrid).")
    parser = argparse.ArgumentParser(description="Build synthetic DVD ISO fixtures (macOS only)")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "fixtures",
                        help="Output directory (default: tests/fixtures)")
    parser.add_argument("--duration", type=float, default=4.0,
                        help="Content VOB duration in seconds (default: 4)")
    args = parser.parse_args()
    build_fixtures(args.out, args.duration)
    print("done.")


if __name__ == "__main__":
    main()
