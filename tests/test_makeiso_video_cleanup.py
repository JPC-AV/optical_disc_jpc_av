#!/usr/bin/env python3
"""Synthetic tests for makeiso-video.py's batch pruning of makeiso.py archive
folders (T-124) and its analysis-error gate (T-127).

Standard library only. ffmpeg/ffprobe answers come from a fake run_cmd, the
encoder is a fake process writing a placeholder file, and the ISO "mount" is
a plain directory. The guards in synthetic_support.py block child processes,
sockets, devices and writes outside TMPDIR. These tests check decisions,
files and records only - not encoding, DVD structure or real media, and not
T-071's duration checks (this file must not depend on them).

Run:  python3 tests/test_makeiso_video_cleanup.py
OPTICAL_UNDER_TEST=/dir runs the same tests against another copy.
"""

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "synthetic_support", Path(__file__).resolve().parent / "synthetic_support.py")
support = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(support)
support.install()

video = support.load("makeiso_video_under_test", "makeiso-video.py")

ARCHIVE = "JPC_AV_00001_overwritten-20261024T101500-7b01de"


class FakeEncoder:
    """Stands in for ffmpeg: writes a placeholder to the output path"""
    outputs = []

    def __init__(self, cmd, **kwargs):
        assert cmd[0] == "ffmpeg", cmd
        out = Path(cmd[-1])
        out.write_bytes(b"SYNTHETIC ACCESS COPY")
        FakeEncoder.outputs.append(out)
        self.stderr = io.StringIO("")
        self.returncode = 0

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self):
        return 0


def fake_run_cmd(cmd, capture_output=True, check=True):
    cmd = [str(c) for c in cmd]
    if cmd[1:] == ["-version"] and cmd[0] in ("ffmpeg", "ffprobe"):
        return subprocess.CompletedProcess(cmd, 0, f"{cmd[0]} version 0.0-synthetic\n", "")
    if cmd[0] == "ffprobe" and "-show_entries" in cmd:
        return subprocess.CompletedProcess(cmd, 0, "600.0\n", "")
    if cmd[0] == "ffprobe" and "-show_streams" in cmd:
        payload = {"format": {}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
    raise AssertionError(f"command not allowed in a synthetic test: {cmd!r}")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="video-case-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.mounts = {}
        self.prompts = []
        self.lines = []
        FakeEncoder.outputs = []
        mounts = self.mounts

        class FakeMount:
            def __init__(self, iso_path):
                self.iso_path = Path(iso_path)
                self.detach_errors = []

            def __enter__(self):
                return mounts.get(self.iso_path)

            def __exit__(self, *exc):
                return False

        def answer(prompt=""):
            self.prompts.append(prompt)
            return "y"

        for patcher in (mock.patch.object(video, "run_cmd", fake_run_cmd),
                        mock.patch.object(video.subprocess, "Popen", FakeEncoder),
                        mock.patch.object(video, "ISOMount", FakeMount),
                        mock.patch.object(video, "log", self.lines.append),
                        mock.patch.object(video, "ANSI_ENABLED", False),
                        mock.patch("builtins.input", answer)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self._violations_before = len(support.VIOLATIONS)
        self.addCleanup(self._no_escapes)

    def _no_escapes(self):
        self.assertEqual(support.VIOLATIONS[self._violations_before:], [],
                         "a guarded operation was attempted")
        self.assertEqual(support.tripwire_calls(), "", "a native tool was executed")

    def disc(self, iso: Path, video_ts=True):
        """A synthetic ISO file and the plain directory its fake mount shows"""
        iso.parent.mkdir(parents=True, exist_ok=True)
        iso.write_bytes(b"SYNTHETIC ISO PLACEHOLDER")
        mount = self.dir / "mounts" / str(len(self.mounts))
        if video_ts:
            (mount / "VIDEO_TS").mkdir(parents=True)
            (mount / "VIDEO_TS" / "VTS_01_1.VOB").write_bytes(b"SYNTHETIC VOB")
        else:
            mount.mkdir(parents=True)
            (mount / "photo.jpg").write_bytes(b"SYNTHETIC DATA FILE")
        self.mounts[iso] = mount
        return iso

    def convert(self, iso: Path, out: Path, dry_run=False):
        config = video.ConversionConfig(iso_path=iso, output_dir=out, operator="T127",
                                        dry_run=dry_run)
        return video.ISOToMP4Converter(config).convert()

    def main_exit(self, *args):
        with mock.patch.object(sys, "argv", ["makeiso-video.py", *args, "--operator", "T127"]):
            with self.assertRaises(SystemExit) as caught:
                video.main()
        return caught.exception.code


class BatchPruningTests(Base):
    """T-124: batch mode skips superseded masters in makeiso.py archive folders"""

    def layout(self):
        current = self.disc(self.dir / "isos" / "JPC_AV_00001" / "JPC_AV_00001.iso")
        archived = self.disc(self.dir / "isos" / "JPC_AV_00001" / ARCHIVE / "JPC_AV_00001.iso")
        return current, archived

    def test_batch_skips_archive_folders(self):
        current, archived = self.layout()
        results = video.process_batch(self.dir / "isos", self.dir / "isos", "T127")
        self.assertEqual((results["processed"], results["success"]), (1, 1))
        self.assertEqual(FakeEncoder.outputs[0].parent, current.parent / "access_JPC_AV_00001")
        self.assertEqual(len(FakeEncoder.outputs), 1)
        self.assertEqual(sorted(p.name for p in archived.parent.iterdir()), ["JPC_AV_00001.iso"])
        self.assertTrue(any("Skipping 1 superseded ISO" in line for line in self.lines), self.lines)
        self.assertTrue(any(f"{ARCHIVE}/JPC_AV_00001.iso" in line for line in self.lines))

    def test_batch_with_output_dir_never_overwrites_the_current_access_copy(self):
        self.layout()
        out = self.dir / "access"
        results = video.process_batch(self.dir / "isos", out, "T127")
        self.assertEqual((results["processed"], results["success"]), (1, 1))
        self.assertEqual(len(FakeEncoder.outputs), 1)
        self.assertEqual(self.prompts, [], "an overwrite prompt was shown")

    def test_discovery_outside_archive_folders_is_unchanged(self):
        self.disc(self.dir / "isos" / "a" / "b" / "JPC_AV_00002.iso")
        self.disc(self.dir / "isos" / "notes_overwritten-later" / "JPC_AV_00003.iso")
        self.disc(self.dir / "isos" / "JPC_AV_00004_overwritten-20261024T101500-7b01de.iso")
        results = video.process_batch(self.dir / "isos", self.dir / "isos", "T127")
        self.assertEqual((results["processed"], results["success"]), (3, 3))

    def test_explicit_single_iso_still_converts_an_archived_master(self):
        current, archived = self.layout()
        self.assertEqual(self.main_exit("-i", str(archived)), 0)
        self.assertEqual(len(FakeEncoder.outputs), 1)
        self.assertEqual(FakeEncoder.outputs[0].parent.parent, archived.parent)


class AnalysisGateTests(Base):
    """T-127: an analysis error stops the ISO before inventory, planning or encoding"""

    @contextlib.contextmanager
    def partial_listing(self, iso, message="synthetic: VIDEO_TS listing failed after one VOB"):
        vts = self.mounts[iso] / "VIDEO_TS"
        first = vts / "VTS_01_1.VOB"
        (vts / "VTS_01_2.VOB").write_bytes(b"SYNTHETIC VOB, NEVER LISTED")
        real_iterdir = Path.iterdir

        def listing(path):
            if path == vts:
                yield first
                raise PermissionError(message) if message else PermissionError()
            yield from real_iterdir(path)

        with mock.patch.object(Path, "iterdir", listing):
            yield

    def assert_refused(self, result, out):
        self.assertFalse(result.success)
        self.assertFalse(result.skipped)
        self.assertTrue((result.error_message or "").startswith("Analysis error"), result.error_message)
        self.assertEqual(FakeEncoder.outputs, [], "something was encoded")
        records = list(out.glob("*_failed-*_manifest.json"))
        self.assertEqual(len(records), 1, sorted(p.name for p in out.iterdir()) if out.exists() else "no folder")
        self.assertEqual(json.loads(records[0].read_text())["conversion_status"]["overall_status"], "error")

    def test_partial_enumeration_fails_before_planning(self):
        iso = self.disc(self.dir / "JPC_AV_00001.iso")
        out = self.dir / "access"
        logged = []
        real_log_analysis = video.ISOToMP4Converter._log_analysis
        with self.partial_listing(iso), \
                mock.patch.object(video.ISOToMP4Converter, "_log_analysis",
                                  lambda conv, a: (logged.append(a), real_log_analysis(conv, a))):
            result = self.convert(iso, out)
        self.assert_refused(result, out)
        self.assertEqual(logged, [], "the incomplete inventory was logged as if complete")

    def test_partial_enumeration_with_an_empty_error_message_is_refused(self):
        iso = self.disc(self.dir / "JPC_AV_00001.iso")
        out = self.dir / "access"
        with self.partial_listing(iso, message=""):
            result = self.convert(iso, out)
        self.assert_refused(result, out)

    def test_partial_enumeration_dry_run_is_refused_too(self):
        iso = self.disc(self.dir / "JPC_AV_00001.iso")
        out = self.dir / "access"
        with self.partial_listing(iso):
            result = self.convert(iso, out, dry_run=True)
        self.assertNotEqual(result.skip_reason, "Dry run mode")
        self.assert_refused(result, out)

    def test_partial_enumeration_fails_the_batch_and_exits_1(self):
        iso = self.disc(self.dir / "isos" / "JPC_AV_00001.iso")
        with self.partial_listing(iso):
            results = video.process_batch(self.dir / "isos", self.dir / "isos", "T127")
            code = self.main_exit("-i", str(iso))
        self.assertEqual((results["success"], results["failed"]), (0, 1))
        self.assertEqual(code, 1)
        self.assertEqual(FakeEncoder.outputs, [])

    def test_control_healthy_analysis_converts(self):
        iso = self.disc(self.dir / "JPC_AV_00001.iso")
        result = self.convert(iso, self.dir / "access")
        self.assertTrue(result.success)
        self.assertEqual(len(FakeEncoder.outputs), 1)

    def test_control_data_disc_is_skipped(self):
        iso = self.disc(self.dir / "JPC_AV_00001.iso", video_ts=False)
        result = self.convert(iso, self.dir / "access")
        self.assertTrue(result.success and result.skipped)
        self.assertEqual(FakeEncoder.outputs, [])

    def test_control_unlistable_root_with_reachable_video_ts_converts(self):
        iso = self.disc(self.dir / "JPC_AV_00001.iso")
        root = self.mounts[iso]
        real_iterdir = Path.iterdir

        def listing(path):
            if path == root:
                raise PermissionError("synthetic: volume root is not listable")
            yield from real_iterdir(path)

        with mock.patch.object(Path, "iterdir", listing):
            result = self.convert(iso, self.dir / "access")
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(len(FakeEncoder.outputs), 1)


if __name__ == "__main__":
    unittest.main()
