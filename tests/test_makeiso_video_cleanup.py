#!/usr/bin/env python3
"""Synthetic tests for makeiso-video.py's batch pruning of makeiso.py archive
folders (T-124), its analysis-error gate (T-127), and a batch search that
reports every part it could not complete, lists what it does not convert,
and refuses special files and shared access folders (T-151).

Standard library only. ffmpeg/ffprobe answers come from a fake run_cmd, the
encoder is a fake process writing a placeholder file, and the ISO "mount" is
a plain directory. The guards in synthetic_support.py block child processes,
sockets, devices and writes outside TMPDIR. Search faults are injected
through os.scandir, os.lstat and os.stat fakes that delegate everything
else; symlinks and FIFOs are real files in the private directory, and a
tripwire (installed before the product is loaded) stops any open of a FIFO
or device before it can block. These tests check decisions, files and
records only - not encoding, DVD structure or real media, and not T-071's
duration checks (this file must not depend on them).

Run:  python3 tests/test_makeiso_video_cleanup.py
OPTICAL_UNDER_TEST=/dir runs the same tests against another copy.
"""

import contextlib
import errno
import importlib.util
import io
import json
import os
import shutil
import stat
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

# Special-file tripwire, installed before the product is loaded: an open of a
# FIFO, device or socket fails at once with SpecialFileTripwire instead of
# blocking the runner (an open is audited before the open call itself).
# Every BatchDiscoveryTests case asserts that no such open was attempted. It
# uses the os.stat saved here, never a test's patched one, so a fault
# injected into os.stat cannot hide a FIFO from it. (A copy of
# test_verifyiso.py's: the test modules are standalone.)
class SpecialFileTripwire(RuntimeError):
    """The product (or a negative control) tried to open a special file"""


TRIPWIRE_OPENS = []
_REAL_STAT = os.stat


def _special_file_tripwire(event, args):
    if event != "open" or not args or isinstance(args[0], int):
        return
    try:
        mode = _REAL_STAT(args[0]).st_mode
    except (OSError, TypeError, ValueError):
        return
    name = os.fsdecode(args[0])
    if stat.S_ISREG(mode) or stat.S_ISDIR(mode) or os.path.realpath(name).startswith("/dev/"):
        return
    TRIPWIRE_OPENS.append(name)
    raise SpecialFileTripwire(f"blocked an open of special file {name}")


sys.addaudithook(_special_file_tripwire)

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
        self.mounted = []   # every ISO path a mount was made for
        self.prompts = []
        self.lines = []
        FakeEncoder.outputs = []
        mounts, mounted = self.mounts, self.mounted

        class FakeMount:
            def __init__(self, iso_path):
                self.iso_path = Path(iso_path)
                self.detach_errors = []
                mounted.append(self.iso_path)

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


# --- batch discovery (T-151) ---------------------------------------------------

class FakeListing:
    """os.scandir's iterator for one folder, in name order, that fails after
    a number of entries (the shape of test_verifyiso.py's T-134 fake)"""

    def __init__(self, real, folder, calls, fail_after):
        with real:
            self.entries = sorted(real, key=lambda e: e.name)
        self.folder, self.calls, self.fail_after = folder, calls, fail_after
        self.index = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        if ("closed", self.folder) not in self.calls:
            self.calls.append(("closed", self.folder))

    def __iter__(self):
        return self

    def __next__(self):
        if self.index >= self.fail_after[0]:
            raise self.fail_after[1]
        if self.index >= len(self.entries):
            raise StopIteration
        entry = self.entries[self.index]
        self.index += 1
        return entry


@contextlib.contextmanager
def scandir_faults(open_errors=None, fail_after=None):
    """os.scandir with faults for chosen folders, delegating everything else.
    open_errors {folder: exception}; fail_after {folder: (n, OSError)}.
    Yields the calls it intercepted, as (kind, folder)"""
    real = os.scandir
    open_errors, fail_after = open_errors or {}, fail_after or {}
    calls = []

    def scandir(path=".", *args, **kwargs):
        folder = Path(os.fsdecode(path)) if not isinstance(path, int) else None
        if folder in open_errors:
            calls.append(("open", folder))
            raise open_errors[folder]
        listing = real(path, *args, **kwargs)
        if folder in fail_after:
            calls.append(("listed", folder))
            return FakeListing(listing, folder, calls, fail_after[folder])
        return listing

    with mock.patch.object(os, "scandir", scandir):
        yield calls


@contextlib.contextmanager
def stat_faults(name, errors):
    """os.stat (following links) or os.lstat raises errors[path] for those
    paths, delegating everything else. Yields the paths it failed"""
    real, fired = getattr(os, name), []

    def call(path, *args, **kwargs):
        target = None if isinstance(path, int) else Path(os.fsdecode(path))
        if target in errors and (name == "lstat" or kwargs.get("follow_symlinks", True)):
            fired.append(target)
            raise errors[target]
        return real(path, *args, **kwargs)

    with mock.patch.object(os, name, call):
        yield fired


def denied(code=errno.EACCES):
    return PermissionError(code, os.strerror(code))


def io_error():
    return OSError(errno.EIO, os.strerror(errno.EIO))


APPLEDOUBLE = b"\x00\x05\x16\x07" + bytes(4092)
ATTEMPT = "JPC_AV_00002_attempt-20261024T101500-7b01de.iso"


class BatchDiscoveryTests(Base):
    """T-151: the batch search lists every folder, reports each part it could
    not complete before and after processing, still converts what it found,
    and exits 1; AppleDouble and makeiso.py attempt ISOs are listed, not
    converted; special files and unfollowable .iso links are never handed
    on; a second ISO for an access folder already used is refused; symlinked
    folders are listed, not followed. Every injected fault is asserted to
    have fired, and no case may open a FIFO (the tripwire stops one first)."""

    def setUp(self):
        super().setUp()
        self.isos = self.dir / "isos"
        self._tripwire_before = len(TRIPWIRE_OPENS)
        self.addCleanup(self._no_special_file_opens)

    def _no_special_file_opens(self):
        self.assertEqual(TRIPWIRE_OPENS[self._tripwire_before:], [],
                         "the product tried to open a FIFO or device (the tripwire stopped it)")

    def batch(self):
        """process_batch's results, then main()'s exit status, for one batch"""
        results = video.process_batch(self.isos, self.isos, "T151")
        return results, self.main_exit("--batch", str(self.isos))

    def interrupted_exit(self, *args):
        """main()'s exit status; a Ctrl-C that escapes it fails the test,
        never the runner"""
        try:
            return self.main_exit(*args)
        except KeyboardInterrupt:
            self.fail("Ctrl-C escaped main()")

    def errors(self, results):
        return {e["path"]: e["detail"] for e in results.get("discovery_errors", [])}

    def assert_listed(self, path, phrase, summary=True):
        """Named before any ISO is processed, and again in the batch summary
        (which is printed when any ISO is processed; summary=False for a
        batch that processes none)"""
        start = next((i for i, line in enumerate(self.lines) if "Batch Processing" in line),
                     len(self.lines))
        shown = str(path) if path == self.isos else str(path.relative_to(self.isos))
        self.assertTrue(any(shown in line and phrase in line for line in self.lines[:start]),
                        self.lines[:start])
        found = next((i for i, line in enumerate(self.lines) if "Batch Summary" in line), None)
        if summary:
            self.assertIsNotNone(found, "no batch summary was printed")
            self.assertTrue(any(str(path) in line and phrase in line
                                for line in self.lines[found:]), self.lines[found:])
        else:
            self.assertIsNone(found, self.lines)

    def assert_never_handed_on(self, results, *isos):
        """Never processed: not counted, attached or given an access folder"""
        processed = {d["iso"] for d in results["details"]} if "details" in results else set()
        for iso in isos:
            self.assertNotIn(iso.name, processed)
            self.assertNotIn(iso, self.mounted)
            self.assertFalse((iso.parent / video._access_dir_name(iso)).exists(),
                             f"{iso.name} was handed to the converter")

    # --- N10: an incomplete search is reported and exits 1 ---

    def test_a_folder_that_cannot_be_listed_is_reported_and_the_rest_converted(self):
        healthy = self.disc(self.isos / "a" / "JPC_AV_00001.iso")
        self.disc(self.isos / "b" / "JPC_AV_00002.iso")
        with scandir_faults(open_errors={self.isos / "b": denied(errno.EPERM)}) as calls:
            results, code = self.batch()
        self.assertIn(("open", self.isos / "b"), calls, "the fault never reached the search")
        self.assertEqual((results["processed"], results["success"]), (1, 1))
        self.assertEqual(set(self.mounted), {healthy})
        self.assertEqual(self.errors(results),
                         {str(self.isos / "b"): "could not list folder: [Errno 1] Operation not permitted"})
        self.assert_listed(self.isos / "b", "could not list folder: [Errno 1]")
        self.assertEqual(code, 1)

    def test_a_real_unreadable_folder_is_reported_and_the_rest_converted(self):
        if os.geteuid() == 0:
            self.skipTest("root can list a folder of mode 000")
        healthy = self.disc(self.isos / "a" / "JPC_AV_00001.iso")
        self.disc(self.isos / "b" / "JPC_AV_00002.iso")
        locked = self.isos / "b"
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        results, code = self.batch()
        locked.chmod(0o700)
        self.assertEqual((results["processed"], results["success"]), (1, 1))
        self.assertEqual(set(self.mounted), {healthy})
        self.assertEqual(self.errors(results),
                         {str(locked): "could not list folder: [Errno 13] Permission denied"})
        self.assertEqual(code, 1)

    def test_an_unlistable_batch_folder_is_reported_not_taken_as_empty(self):
        self.disc(self.isos / "JPC_AV_00001.iso")
        for label in ("fake", "mode 000"):
            with self.subTest(label):
                if label == "mode 000" and os.geteuid() == 0:
                    self.skipTest("root can list a folder of mode 000")
                self.lines.clear()
                self.mounted.clear()
                if label == "fake":
                    faults = scandir_faults(open_errors={self.isos: denied()})
                else:
                    self.isos.chmod(0)
                    self.addCleanup(self.isos.chmod, 0o700)
                    faults = contextlib.nullcontext([("open", self.isos)])
                try:
                    with faults as calls:
                        results, code = self.batch()
                finally:
                    self.isos.chmod(0o700)
                self.assertIn(("open", self.isos), calls, "the fault never reached the search")
                self.assertEqual(results["processed"], 0)
                self.assertEqual(self.mounted, [])
                self.assertEqual(self.errors(results),
                                 {str(self.isos): "could not list folder: [Errno 13] Permission denied"})
                self.assert_listed(self.isos, "could not list folder", summary=False)
                self.assertEqual(code, 1)

    def test_a_listing_that_stops_partway_keeps_what_it_read(self):
        self.disc(self.isos / "a" / "JPC_AV_00001.iso")
        self.disc(self.isos / "b" / "JPC_AV_00002.iso")
        self.disc(self.isos / "b" / "JPC_AV_00003.iso")
        with scandir_faults(fail_after={self.isos / "b": (1, io_error())}) as calls:
            results, code = self.batch()
        self.assertIn(("listed", self.isos / "b"), calls, "the fault never reached the search")
        self.assertIn(("closed", self.isos / "b"), calls, "the listing was not closed")
        self.assertEqual([d["iso"] for d in results["details"]], ["JPC_AV_00001.iso", "JPC_AV_00002.iso"])
        self.assertEqual(self.errors(results),
                         {str(self.isos / "b"): "listing stopped partway: [Errno 5] Input/output error"})
        self.assert_listed(self.isos / "b", "listing stopped partway")
        self.assertEqual(code, 1)

    def test_entries_that_cannot_be_inspected_or_disappear_are_reported(self):
        self.disc(self.isos / "JPC_AV_00001.iso")
        for n in (2, 3):
            self.disc(self.isos / f"JPC_AV_0000{n}.iso")
        errors = {self.isos / "JPC_AV_00002.iso": denied(),
                  self.isos / "JPC_AV_00003.iso": FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))}
        with stat_faults("lstat", errors) as fired:
            results, code = self.batch()
        self.assertEqual(set(fired), set(errors), "a fault never reached the search")
        self.assertEqual([d["iso"] for d in results["details"]], ["JPC_AV_00001.iso"])
        self.assertEqual(self.errors(results), {
            str(self.isos / "JPC_AV_00002.iso"): "could not inspect entry: [Errno 13] Permission denied",
            str(self.isos / "JPC_AV_00003.iso"): "entry disappeared during the search"})
        self.assertEqual(code, 1)

    def test_ctrl_c_still_exits_130_and_the_summary_lists_the_search_problems(self):
        # G2-D2 with G6-D1: the Ctrl-C (at the first ISO's overwrite prompt)
        # stops the batch and decides the exit status; the summary still
        # lists what the search could not cover
        first = self.disc(self.isos / "a" / "JPC_AV_00001.iso")
        self.disc(self.isos / "a" / "JPC_AV_00002.iso")
        self.disc(self.isos / "b" / "JPC_AV_00003.iso")
        earlier = self.isos / "a" / "access_JPC_AV_00001" / "JPC_AV_00001.mp4"
        earlier.parent.mkdir(parents=True)
        earlier.write_bytes(b"EARLIER ACCESS COPY")
        asked = []

        def interrupt(prompt=""):
            asked.append(prompt)
            raise KeyboardInterrupt

        with scandir_faults(open_errors={self.isos / "b": denied()}) as calls, \
                mock.patch("builtins.input", interrupt):
            code = self.interrupted_exit("--batch", str(self.isos))
        self.assertIn(("open", self.isos / "b"), calls, "the fault never reached the search")
        self.assertEqual(len(asked), 1, "the Ctrl-C was never delivered")
        self.assertEqual(code, 130)
        self.assertEqual(self.mounted, [first])
        self.assertEqual(earlier.read_bytes(), b"EARLIER ACCESS COPY")
        self.assert_listed(self.isos / "b", "could not list folder")

    def test_ctrl_c_during_the_search_stops_the_batch(self):
        self.disc(self.isos / "JPC_AV_00001.iso")
        real, fired = os.scandir, []

        def scandir(path=".", *args, **kwargs):
            if not isinstance(path, int) and Path(os.fsdecode(path)) == self.isos:
                fired.append(path)
                raise KeyboardInterrupt
            return real(path, *args, **kwargs)

        with mock.patch.object(os, "scandir", scandir):
            code = self.interrupted_exit("--batch", str(self.isos))
        self.assertTrue(fired, "the Ctrl-C never reached the search")
        self.assertEqual(code, 130)
        self.assertEqual(self.mounted, [])

    # --- special files, and .iso links that cannot be followed ---

    def test_special_files_named_iso_are_never_handed_on(self):
        healthy = self.disc(self.isos / "JPC_AV_00001.iso")
        fifo, link = self.isos / "JPC_AV_00009.iso", self.isos / "JPC_AV_00010.iso"
        os.mkfifo(fifo)
        os.symlink(fifo, link)
        results, code = self.batch()
        self.assertEqual(self.mounted, [healthy, healthy], "a special file was handed to the converter")
        self.assertEqual(results["processed"], 1)
        refused = "not a regular file (FIFO, device or socket); not processed"
        self.assertEqual(self.errors(results), {str(fifo): refused, str(link): refused})
        self.assert_never_handed_on(results, fifo, link)
        self.assert_listed(fifo, "not a regular file")
        self.assertEqual(code, 1)

    def test_iso_links_that_cannot_be_followed_are_reported(self):
        healthy = self.disc(self.isos / "JPC_AV_00001.iso")
        target = self.disc(self.dir / "elsewhere" / "JPC_AV_00012.iso")
        dangling, unreadable = self.isos / "JPC_AV_00011.iso", self.isos / "JPC_AV_00012.iso"
        os.symlink(self.dir / "unmounted" / "JPC_AV_00011.iso", dangling)
        os.symlink(target, unreadable)
        with stat_faults("stat", {unreadable: io_error()}) as fired:
            results, code = self.batch()
        self.assertIn(unreadable, fired, "the fault never reached the search")
        self.assertEqual(set(self.mounted), {healthy})
        self.assertEqual(self.errors(results), {
            str(dangling): f"symlink target missing -> {self.dir / 'unmounted' / 'JPC_AV_00011.iso'}; "
                           f"not processed",
            str(unreadable): f"could not inspect the symlink's target -> {target}: "
                             f"[Errno 5] Input/output error; not processed"})
        self.assert_never_handed_on(results, dangling, unreadable)
        self.assertEqual(code, 1)

    def test_a_special_file_given_with_i_is_never_attached(self):
        fifo = self.dir / "JPC_AV_00009.iso"
        os.mkfifo(fifo)
        self.assertEqual(self.main_exit("-i", str(fifo)), 1)
        self.assertEqual(self.mounted, [], "the FIFO was handed to hdiutil")
        manifest = json.loads(next((self.dir / "access_JPC_AV_00009").glob("*_failed-*_manifest.json"))
                              .read_text())
        self.assertTrue(manifest["conversion_status"]["error_message"].startswith(
            "Not a regular file"), manifest["conversion_status"]["error_message"])

    # --- G6-D2: symlinked folders; a folder named *.iso ---

    def test_symlinked_folders_are_listed_not_followed(self):
        healthy = self.disc(self.isos / "JPC_AV_00001.iso")
        outside = self.dir / "outside"
        self.disc(outside / "JPC_AV_00011.iso")
        link = self.isos / "linked"
        os.symlink(outside, link)
        results, code = self.batch()
        self.assertEqual(set(self.mounted), {healthy})
        self.assertEqual(results["processed"], 1)
        self.assertEqual(self.errors(results), {})
        self.assertTrue(any(line.strip() == f"linked: symlinked folder not followed -> {outside}"
                            for line in self.lines), self.lines)
        self.assertEqual(code, 0)

    def test_a_folder_named_like_an_iso_is_searched_not_converted(self):
        inner = self.disc(self.isos / "odd.iso" / "JPC_AV_00001.iso")
        results, code = self.batch()
        self.assertEqual(set(self.mounted), {inner})
        self.assertEqual((results["processed"], results["success"], results["failed"]), (1, 1, 0))
        self.assertEqual(code, 0)

    # --- N11 (G6-D4): AppleDouble and makeiso.py attempt ISOs ---

    def test_appledouble_and_attempt_isos_are_listed_not_converted(self):
        master = self.disc(self.isos / "JPC_AV_00001.iso")
        (self.isos / "._JPC_AV_00001.iso").write_bytes(APPLEDOUBLE)
        self.disc(self.isos / ATTEMPT)
        results = video.process_batch(self.isos, self.isos, "T151")
        self.assertEqual((results["processed"], results["success"], results["failed"]), (1, 1, 0))
        self.assertEqual(self.mounted, [master])
        self.assertEqual(sorted(p.name for p in self.isos.iterdir() if p.is_dir()), ["access_JPC_AV_00001"])
        self.assertEqual(results["discovery_errors"], [])
        self.assertTrue(any("Skipping 2 file(s) that are not masters" in line for line in self.lines))
        for name, why in (("._JPC_AV_00001.iso", "AppleDouble"), (ATTEMPT, "attempt ISO")):
            self.assertTrue(any(line.strip().startswith(f"{name}: ") and why in line
                                for line in self.lines), (name, self.lines))
        self.assertEqual(self.main_exit("--batch", str(self.isos)), 0)

    def test_an_appledouble_companion_does_not_hide_the_master_given_as_a_folder(self):
        folder = self.dir / "JPC_AV_00001"
        master = self.disc(folder / "JPC_AV_00001.iso")
        (folder / "._JPC_AV_00001.iso").write_bytes(APPLEDOUBLE)
        self.assertEqual(self.main_exit("-i", str(folder)), 0)
        self.assertEqual(self.mounted, [master])
        self.assertEqual(len(FakeEncoder.outputs), 1)

    # --- MV-15 (G6-D6): one access folder per batch ISO ---

    def test_a_second_iso_for_the_same_access_folder_is_refused(self):
        out = self.dir / "out"
        for label, names in (("same name", ("disc.iso", "disc.iso")),
                             ("names that differ only in case", ("JPC_AV_00005.iso", "jpc_av_00005.iso"))):
            with self.subTest(label):
                self.isos = self.dir / label.replace(" ", "-")
                first = self.disc(self.isos / "a" / names[0])
                second = self.disc(self.isos / "b" / names[1])
                self.mounted.clear()
                self.prompts.clear()
                results = video.process_batch(self.isos, out, "T151")
                self.assertEqual(self.prompts, [], "an overwrite prompt was shown")
                self.assertEqual(results["processed"], 1)
                self.assertEqual(self.mounted, [first])
                folder = out / video._access_dir_name(second)  # its own name for the folder
                self.assertEqual(self.errors(results),
                                 {str(second): f"would share {folder} with {first}; not processed"})
                self.assertEqual(self.main_exit("--batch", str(self.isos), "-o", str(out)), 1)
                self.assertNotIn(second, self.mounted)

    def test_a_shared_access_folder_is_refused_without_o_too(self):
        # The ruling: one access folder per batch ISO, with or without -o. By
        # default each access folder sits beside its ISO, so two ISOs share
        # one only when their names map to the same folder name there
        first = self.disc(self.isos / "00001.iso")          # sorts first
        second = self.disc(self.isos / "JPC_AV_00001.iso")
        results = video.process_batch(self.isos, self.isos, "T151")
        self.assertEqual(self.prompts, [], "an overwrite prompt was shown")
        self.assertEqual(self.mounted, [first])
        folder = self.isos / "access_JPC_AV_00001"
        self.assertEqual(self.errors(results),
                         {str(second): f"would share {folder} with {first}; not processed"})
        self.assertEqual(self.main_exit("--batch", str(self.isos)), 1)
        self.assertNotIn(second, self.mounted)

    def test_control_distinct_access_folders_are_never_refused(self):
        cases = (("same name, different folders, default output", ("a/JPC_AV_00001.iso",
                                                                   "b/JPC_AV_00001.iso"), False),
                 ("different names, one folder, default output", ("JPC_AV_00002.iso",
                                                                  "JPC_AV_00003.iso"), False),
                 ("different names, different folders, -o", ("a/JPC_AV_00004.iso",
                                                             "b/JPC_AV_00005.iso"), True))
        for n, (label, names, with_output) in enumerate(cases):
            with self.subTest(label):
                self.isos = self.dir / f"distinct-{n}"
                isos = [self.disc(self.isos / name) for name in names]
                out = self.dir / f"out-{n}" if with_output else self.isos
                self.mounted.clear()
                self.prompts.clear()
                results = video.process_batch(self.isos, out, "T151")
                self.assertEqual((results["processed"], results["success"]), (2, 2))
                self.assertEqual(results.get("discovery_errors", []), [])
                self.assertEqual(sorted(self.mounted), sorted(isos))
                self.assertEqual(self.prompts, [], "an overwrite prompt was shown")

    # --- the batch folder itself (G6-D1) ---

    def test_a_batch_folder_that_is_a_file_is_reported(self):
        iso = self.disc(self.dir / "JPC_AV_00001.iso")
        self.assertEqual(self.main_exit("--batch", str(iso)), 1)
        self.assertEqual(self.mounted, [])
        self.assertTrue(any(str(iso) in line and "not a folder" in line for line in self.lines),
                        self.lines)

    def test_a_batch_folder_that_cannot_be_checked_is_reported(self):
        self.disc(self.isos / "JPC_AV_00001.iso")
        real_path_stat, path_fired = Path.stat, []

        def path_stat(path, *args, **kwargs):
            # pathlib's own stat, which released 3.9's exists() calls
            if path == self.isos:
                path_fired.append(path)
                raise io_error()
            return real_path_stat(path, *args, **kwargs)

        with stat_faults("stat", {self.isos: io_error()}) as fired, \
                mock.patch.object(Path, "stat", path_stat):
            try:
                code = self.main_exit("--batch", str(self.isos))
            except OSError as e:
                self.fail(f"the batch folder's check escaped main(): {e!r}")
        self.assertTrue(fired or path_fired, "the fault never fired")
        self.assertEqual(code, 1)
        self.assertEqual(self.mounted, [])
        self.assertTrue(any(str(self.isos) in line and
                            "could not check the batch folder: [Errno 5] Input/output error" in line
                            for line in self.lines), self.lines)

    # --- healthy controls ---

    def test_control_a_symlinked_batch_folder_is_followed(self):
        iso = self.disc(self.isos / "JPC_AV_00001.iso")
        link = self.dir / "batch-link"
        os.symlink(self.isos, link)
        self.mounts[link / iso.name] = self.mounts[iso]
        self.assertEqual(self.main_exit("--batch", str(link)), 0)
        self.assertEqual(self.mounted, [link / iso.name])
        self.assertEqual(len(FakeEncoder.outputs), 1)

    def test_control_an_iso_link_to_a_master_is_converted(self):
        target = self.disc(self.dir / "masters" / "JPC_AV_00001.iso")
        link = self.isos / "JPC_AV_00001.iso"
        link.parent.mkdir(parents=True)
        os.symlink(target, link)
        self.mounts[link] = self.mounts[target]
        results, code = self.batch()
        self.assertEqual((results["processed"], results["success"]), (1, 1))
        self.assertEqual(self.mounted[0], link)
        self.assertEqual(code, 0)

    def test_an_unlistable_folder_inside_an_archive_folder_is_only_a_note(self):
        # Nothing in a makeiso.py archive folder is converted, so a part of
        # one that cannot be searched hides no master: listed, exit 0
        current = self.disc(self.isos / "JPC_AV_00001" / "JPC_AV_00001.iso")
        archive = self.isos / "JPC_AV_00001" / ARCHIVE
        self.disc(archive / "JPC_AV_00001.iso")
        (archive / "extra").mkdir()
        with scandir_faults(open_errors={archive / "extra": denied()}) as calls:
            results, code = self.batch()
        self.assertIn(("open", archive / "extra"), calls, "the fault never reached the search")
        self.assertEqual(set(self.mounted), {current})
        self.assertEqual(results.get("discovery_errors"), [])
        self.assertTrue(any("extra: could not list folder" in line and "archive folder" in line
                            for line in self.lines), self.lines)
        self.assertTrue(any("Skipping 1 superseded ISO" in line for line in self.lines))
        self.assertEqual(code, 0)

    def test_control_names_that_only_look_like_attempts_are_masters(self):
        for name in ("JPC_AV_00003_attempt.iso", "JPC_AV_00004_attempt-2026.iso"):
            self.disc(self.isos / name)
        results, code = self.batch()
        self.assertEqual((results["processed"], results["success"]), (2, 2))
        self.assertEqual(code, 0)

    def test_control_explicit_single_isos_are_unchanged(self):
        attempt = self.disc(self.dir / "single" / ATTEMPT)
        self.assertEqual(self.main_exit("-i", str(attempt)), 0)
        self.assertEqual(self.mounted, [attempt])
        folder = self.dir / "both"
        self.disc(folder / "JPC_AV_00002.iso")
        self.disc(folder / ATTEMPT)
        self.assertEqual(self.main_exit("-i", str(folder)), 1)
        self.assertTrue(any("Multiple .iso files found" in line for line in self.lines))
        self.assertEqual(self.mounted, [attempt])

    def test_control_a_missing_batch_folder_exits_1(self):
        missing = self.dir / "not-there"
        self.assertEqual(self.main_exit("--batch", str(missing)), 1)
        self.assertEqual(self.mounted, [])
        self.assertTrue(any(str(missing) in line for line in self.lines), self.lines)


if __name__ == "__main__":
    unittest.main()
