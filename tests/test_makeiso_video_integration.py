#!/usr/bin/env python3
"""Synthetic tests for how makeiso-video.py's duration check, review copies
and deferred promotion (T-071) work together with the released cleanup: the
analysis-error gate (T-127), batch pruning of makeiso.py archive folders
(T-124) and verifyiso.py's handling of conversion records (T-125).

Standard library only. Every ffprobe answer is chosen per file and every call
is recorded, so a test can show what was probed or encoded and what was not;
an unknown target fails the test. The encoder is a fake process writing
placeholder bytes, and the ISO "mount" is a plain directory. The guards in
synthetic_support.py block child processes, sockets, devices and writes
outside TMPDIR. These tests check decisions, files and records only - not
encoding, DVD structure or real media.

Run:  python3 tests/test_makeiso_video_integration.py
OPTICAL_UNDER_TEST=/dir runs the same tests against another copy.
"""

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "synthetic_support", Path(__file__).resolve().parent / "synthetic_support.py")
support = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(support)
support.install()

video = support.load("makeiso_video_under_test", "makeiso-video.py")
verify = support.load("verifyiso_under_test", "verifyiso.py")

ARCHIVE = "JPC_AV_00001_overwritten-20261024T101500-7b01de"
EARLIER = b"EARLIER ACCESS COPY"


class FakeTools:
    """ffprobe answers per file and a fake ffmpeg process; records every call.
    VOB durations are keyed by path, MP4 durations by the ISO stem the MP4 is
    named after (temporary .encoding-* files included)"""

    def __init__(self):
        self.vob_seconds = {}
        self.mp4_seconds = {}
        self.probes = []    # (ffprobe kind, target) for every non-version call
        self.encodes = []   # output path of every encoder start

    def run_cmd(self, cmd, capture_output=True, check=True):
        cmd = [str(c) for c in cmd]
        if cmd[1:] == ["-version"] and cmd[0] in ("ffmpeg", "ffprobe"):
            return subprocess.CompletedProcess(cmd, 0, f"{cmd[0]} version 0.0-synthetic\n", "")
        if cmd[0] != "ffprobe" or not ("-show_entries" in cmd or "-show_streams" in cmd):
            raise AssertionError(f"command not allowed in a synthetic test: {cmd!r}")
        target = Path(cmd[-1])
        kind = "duration" if "-show_entries" in cmd else "streams"
        self.probes.append((kind, target))
        seconds = self.seconds(target)
        if kind == "duration":
            return subprocess.CompletedProcess(cmd, 0, f"{seconds}\n", "")
        payload = {"format": {}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    def seconds(self, target: Path) -> float:
        if target in self.vob_seconds:
            return self.vob_seconds[target]
        stem = target.name.split(".")[0]
        if target.suffix == ".mp4" and stem in self.mp4_seconds:
            return self.mp4_seconds[stem]
        raise AssertionError(f"no synthetic ffprobe answer for {target}")

    def popen(self, cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        if cmd[0] != "ffmpeg":
            raise AssertionError(f"process not allowed in a synthetic test: {cmd!r}")
        out = Path(cmd[-1])
        out.write_bytes(b"SYNTHETIC ACCESS COPY " + out.parent.name.encode())
        self.encodes.append(out)
        return types.SimpleNamespace(stderr=io.StringIO(""), returncode=0,
                                     poll=lambda: 0, kill=lambda: None, wait=lambda: 0)

    def probed_under(self, folder: Path) -> list:
        return [t for _, t in self.probes if folder in t.parents]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="integration-case-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.tools = FakeTools()
        self.mounts = {}
        self.mounted = []
        self.partial = set()
        self.prompts = []
        self.lines = []
        mounts, mounted = self.mounts, self.mounted

        class FakeMount:
            def __init__(self, iso_path):
                self.iso_path = Path(iso_path)
                self.detach_errors = []

            def __enter__(self):
                mounted.append(self.iso_path)
                return mounts.get(self.iso_path)

            def __exit__(self, *exc):
                return False

        def answer(prompt=""):
            self.prompts.append(prompt)
            return "y"

        real_iterdir = Path.iterdir
        partial = self.partial

        def listing(path):
            # A VIDEO_TS listing that fails after its first entry
            if path in partial:
                yield sorted(real_iterdir(path))[0]
                raise PermissionError("synthetic: VIDEO_TS listing failed partway")
            yield from real_iterdir(path)

        for patcher in (mock.patch.object(video, "run_cmd", self.tools.run_cmd),
                        mock.patch.object(video.subprocess, "Popen", self.tools.popen),
                        mock.patch.object(video, "ISOMount", FakeMount),
                        mock.patch.object(video, "log", self.lines.append),
                        mock.patch.object(video, "ANSI_ENABLED", False),
                        mock.patch.object(Path, "iterdir", listing),
                        mock.patch("builtins.input", answer)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self._violations_before = len(support.VIOLATIONS)
        self.addCleanup(self._no_escapes)

    def _no_escapes(self):
        self.assertEqual(support.VIOLATIONS[self._violations_before:], [],
                         "a guarded operation was attempted")
        self.assertEqual(support.tripwire_calls(), "", "a native tool was executed")

    def disc(self, iso: Path, vob_seconds=600.0, mp4_seconds=None, partial=False):
        """A synthetic ISO file and the plain directory its fake mount shows:
        VIDEO_TS with two content VOBs and no IFO, so the expected duration
        comes from the VOB probes. mp4_seconds is what its MP4s measure."""
        iso.parent.mkdir(parents=True, exist_ok=True)
        iso.write_bytes(b"SYNTHETIC ISO PLACEHOLDER")
        mount = self.dir / "mounts" / str(len(self.mounts))
        vts = mount / "VIDEO_TS"
        vts.mkdir(parents=True)
        for name in ("VTS_01_1.VOB", "VTS_01_2.VOB"):
            (vts / name).write_bytes(b"SYNTHETIC VOB")
            self.tools.vob_seconds[vts / name] = vob_seconds / 2
        self.tools.mp4_seconds[iso.stem] = vob_seconds if mp4_seconds is None else mp4_seconds
        if partial:
            self.partial.add(vts)
        self.mounts[iso] = mount
        return iso

    def canonical(self, out: Path, stem="JPC_AV_00001") -> dict:
        """An earlier run's final MP4, log and manifest; returns their bytes"""
        out.mkdir(parents=True, exist_ok=True)
        earlier = {
            out / f"{stem}.mp4": EARLIER,
            out / f"{stem}.mp4.log.txt": b"EARLIER LOG\n",
            out / f"{stem}_access_manifest.json": json.dumps(
                {"conversion_metadata": {"run_id": "earlier"},
                 "conversion_status": {"overall_status": "success"}}).encode(),
        }
        for path, data in earlier.items():
            path.write_bytes(data)
        return earlier

    def assert_unchanged(self, files: dict):
        for path, data in files.items():
            self.assertEqual(path.read_bytes(), data, f"{path.name} changed")

    def convert(self, iso: Path, out: Path, dry_run=False):
        config = video.ConversionConfig(iso_path=iso, output_dir=out, operator="T071",
                                        dry_run=dry_run)
        return video.ISOToMP4Converter(config).convert()

    def main_exit(self, *args):
        with mock.patch.object(sys, "argv", ["makeiso-video.py", *args, "--operator", "T071"]):
            with self.assertRaises(SystemExit) as caught:
                video.main()
        return caught.exception.code

    @staticmethod
    def names(folder: Path, pattern="*") -> list:
        return sorted(p.name for p in folder.glob(pattern))

    @staticmethod
    def record(folder: Path, pattern: str) -> dict:
        found = sorted(folder.glob(pattern))
        if len(found) != 1:
            raise AssertionError(f"expected one {pattern} in {folder}, found {found}")
        return json.loads(found[0].read_text())

    def assert_no_attempt_files(self, folder: Path):
        for pattern in ("*.encoding-*", "*.mp4.partial", "*.mp4.unverified", "*_unverified-*"):
            self.assertEqual(self.names(folder, pattern), [], pattern)


class AnalysisRefusalTests(Base):
    """An analysis error stops the ISO before any duration work, even with
    final files from an earlier run in place"""

    def setUp(self):
        super().setUp()
        self.iso = self.disc(self.dir / "JPC_AV_00001.iso", partial=True)
        self.out = self.dir / "access_JPC_AV_00001"
        self.earlier = self.canonical(self.out)
        self.planning = mock.patch.object(video, "plan_titlesets", wraps=video.plan_titlesets)
        self.expecting = mock.patch.object(video, "expected_titleset_duration",
                                           wraps=video.expected_titleset_duration)

    def assert_refused(self, result, dry_run):
        self.assertFalse(result.success)
        self.assertFalse(result.skipped or result.unverified or result.aborted)
        self.assertTrue(result.error_message.startswith("Analysis error: "), result.error_message)
        self.assertEqual(self.prompts, [], "an overwrite prompt was shown")
        self.assertEqual(self.tools.probes, [], "something was probed")
        self.assertEqual(self.tools.encodes, [], "something was encoded")
        self.assert_unchanged(self.earlier)
        self.assert_no_attempt_files(self.out)
        manifest = self.record(self.out, "JPC_AV_00001_failed-*_manifest.json")
        self.assertEqual(manifest["conversion_status"]["overall_status"], "error")
        self.assertEqual(manifest["conversion_status"]["canonical_files_changed"], [])
        self.assertIsNone(manifest["conversion_status"]["unverified_reason"])
        self.assertEqual(manifest["titlesets"]["count"], 0)
        self.assertIs(manifest["conversion_metadata"]["dry_run"], dry_run)
        self.assertEqual(len(self.names(self.out, "JPC_AV_00001_failed-*.log.txt")), 1)

    def test_refused_with_earlier_final_files_kept(self):
        with self.planning as planning, self.expecting as expecting:
            result = self.convert(self.iso, self.out)
        self.assert_refused(result, dry_run=False)
        planning.assert_not_called()
        expecting.assert_not_called()

    def test_dry_run_refused_with_earlier_final_files_kept(self):
        with self.planning as planning, self.expecting as expecting:
            result = self.convert(self.iso, self.out, dry_run=True)
        self.assert_refused(result, dry_run=True)
        self.assertNotEqual(result.skip_reason, "Dry run mode")
        self.assertEqual(self.names(self.out, "*dryrun*") + self.names(self.out, "*_skip_manifest.json"), [])
        planning.assert_not_called()
        expecting.assert_not_called()

    def test_single_iso_exits_1_normally_and_as_dry_run(self):
        self.assertEqual(self.main_exit("-i", str(self.iso)), 1)
        self.assertEqual(self.main_exit("-i", str(self.iso), "-n"), 1)
        self.assertEqual((self.tools.probes, self.tools.encodes, self.prompts), ([], [], []))
        self.assert_unchanged(self.earlier)
        self.assertEqual(len(self.names(self.out, "JPC_AV_00001_failed-*_manifest.json")), 2)

    def test_control_same_disc_without_the_listing_error_is_checked_and_promoted(self):
        self.partial.clear()
        result = self.convert(self.iso, self.out)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.tools.encodes[0].parent, self.out)
        self.assertNotEqual((self.out / "JPC_AV_00001.mp4").read_bytes(), EARLIER)
        check = self.record(self.out, "JPC_AV_00001_access_manifest.json")["titlesets"]["outputs"][0]
        self.assertEqual((check["duration_check"]["result"], check["duration_check"]["expected_source"]),
                         ("passed", "vob_probe"))


class MixedBatchTests(Base):
    """One batch: a healthy master with an archived predecessor, an unverified
    ISO with earlier final files, and an ISO whose analysis fails"""

    def setUp(self):
        super().setUp()
        self.isos = self.dir / "isos"
        self.current = self.disc(self.isos / "JPC_AV_00001" / "JPC_AV_00001.iso")
        self.archived = self.disc(self.isos / "JPC_AV_00001" / ARCHIVE / "JPC_AV_00001.iso")
        self.unverified = self.disc(self.isos / "JPC_AV_00002.iso", mp4_seconds=700.0)
        self.failing = self.disc(self.isos / "JPC_AV_00003.iso", partial=True)
        self.unverified_out = self.isos / "access_JPC_AV_00002"
        self.earlier = self.canonical(self.unverified_out, stem="JPC_AV_00002")

    def test_counts_review_copy_and_untouched_archive(self):
        results = video.process_batch(self.isos, self.isos, "T071")
        self.assertEqual({k: results[k] for k in ("processed", "success", "skipped",
                                                   "unverified", "failed")},
                         {"processed": 3, "success": 1, "skipped": 0, "unverified": 1, "failed": 1})
        self.assertEqual(sorted(self.mounted), sorted([self.current, self.unverified, self.failing]))
        self.assertTrue(any("Skipping 1 superseded ISO" in line for line in self.lines))
        archive = self.archived.parent
        self.assertEqual(sorted(p.name for p in archive.rglob("*")), ["JPC_AV_00001.iso"])
        self.assertEqual(self.tools.probed_under(self.mounts[self.archived]), [])
        self.assertEqual(self.tools.probed_under(self.mounts[self.failing]), [])
        self.assertEqual(sorted(p.parent for p in self.tools.encodes),
                         sorted([self.current.parent / "access_JPC_AV_00001", self.unverified_out]))

        healthy = self.record(self.current.parent / "access_JPC_AV_00001",
                              "JPC_AV_00001_access_manifest.json")
        self.assertEqual(healthy["conversion_status"]["overall_status"], "success")
        self.assertEqual(healthy["titlesets"]["outputs"][0]["duration_check"]["result"], "passed")

        self.assertEqual(len(self.prompts), 1, "only the unverified ISO had final files")
        self.assert_unchanged(self.earlier)
        self.assertEqual(len(self.names(self.unverified_out, "JPC_AV_00002_unverified-*.mp4.unverified")), 1)
        held = self.record(self.unverified_out, "JPC_AV_00002_unverified-*_manifest.json")
        self.assertEqual(held["conversion_status"]["overall_status"], "unverified")
        check = held["titlesets"]["outputs"][0]["duration_check"]
        self.assertEqual((check["result"], check["expected_seconds"], check["output_seconds"]),
                         ("unverified", 600.0, 700.0))
        self.assertEqual(held["conversion_status"]["canonical_files_changed"], [])

        failed_out = self.isos / "access_JPC_AV_00003"
        failed = self.record(failed_out, "JPC_AV_00003_failed-*_manifest.json")
        self.assertEqual(failed["conversion_status"]["overall_status"], "error")
        self.assertTrue(failed["conversion_status"]["error_message"].startswith("Analysis error: "))
        self.assert_no_attempt_files(failed_out)

    def test_batch_exits_1(self):
        self.assertEqual(self.main_exit("--batch", str(self.isos)), 1)
        self.assertNotIn(self.archived, self.mounted)

    def test_verifier_skips_every_conversion_record(self):
        video.process_batch(self.isos, self.isos, "T071")
        manifests = verify.find_preservation_manifests([self.isos])
        names = sorted(p.name for p in manifests)
        for pattern in ("JPC_AV_00001_access_manifest.json", "JPC_AV_00002_access_manifest.json",
                        "JPC_AV_00002_unverified-*_manifest.json",
                        "JPC_AV_00003_failed-*_manifest.json"):
            self.assertEqual(len([p for p in manifests if p.match(pattern)]), 1, (pattern, names))
        for path in manifests:
            result = verify.check_manifest(path)
            self.assertEqual(result.status, "SKIPPED", (path.name, result.detail))
            self.assertIn("makeiso-video access-copy record", result.detail)


class ArchiveAndSelectionTests(Base):
    """Batch pruning leaves an archive-only folder alone; an explicit -i still
    converts an archived master, under the same duration check"""

    def setUp(self):
        super().setUp()
        self.isos = self.dir / "isos"
        self.archived = self.disc(self.isos / "JPC_AV_00001" / ARCHIVE / "JPC_AV_00001.iso")

    def test_archive_only_batch_processes_nothing_and_exits_0(self):
        results = video.process_batch(self.isos, self.isos, "T071")
        self.assertEqual(results, {"processed": 0, "success": 0, "skipped": 0,
                                   "unverified": 0, "failed": 0, "discovery_errors": []})
        self.assertEqual(self.main_exit("--batch", str(self.isos)), 0)
        self.assertEqual((self.mounted, self.tools.probes, self.tools.encodes), ([], [], []))
        self.assertEqual(sorted(p.name for p in self.archived.parent.rglob("*")),
                         ["JPC_AV_00001.iso"])

    def test_explicit_iso_converts_an_archived_master_and_leaves_the_current_copy(self):
        self.disc(self.isos / "JPC_AV_00001" / "JPC_AV_00001.iso")
        current = self.canonical(self.isos / "JPC_AV_00001" / "access_JPC_AV_00001")
        self.assertEqual(self.main_exit("-i", str(self.archived)), 0)
        self.assertEqual(self.mounted, [self.archived])
        out = self.archived.parent / "access_JPC_AV_00001"
        self.assertEqual([p.parent for p in self.tools.encodes], [out])
        vobs = self.tools.probed_under(self.mounts[self.archived])
        self.assertTrue(vobs and all(p.suffix == ".VOB" for p in vobs), vobs)
        self.assertTrue(self.tools.probed_under(out), "the output was never measured")
        check = self.record(out, "JPC_AV_00001_access_manifest.json")["titlesets"]["outputs"][0]
        self.assertEqual(check["duration_check"]["result"], "passed")
        self.assert_unchanged(current)
        self.assertEqual(self.prompts, [])


class RecordFailureAfterPromotionTests(Base):
    """Records are written after promotion, the canonical log and manifest
    to temporary files renamed into place (T-144). If they cannot be
    written, the run fails and the MP4s it promoted stay (no rollback); the
    earlier canonical records stay whole, and this run's failure records
    say they no longer match. If those cannot be written either, the
    screen says so"""

    def setUp(self):
        super().setUp()
        self.iso = self.disc(self.dir / "JPC_AV_00001.iso")
        self.out = self.dir / "access_JPC_AV_00001"
        self.earlier = self.canonical(self.out)
        self.mp4 = self.out / "JPC_AV_00001.mp4"
        self.log = self.out / "JPC_AV_00001.mp4.log.txt"
        self.manifest = self.out / "JPC_AV_00001_access_manifest.json"

    def assert_promoted_beside_earlier_records(self):
        self.assertEqual(self.mp4.read_bytes(), b"SYNTHETIC ACCESS COPY access_JPC_AV_00001")
        self.assertTrue(any("Could not write run records" in line for line in self.lines))
        self.assertEqual(self.log.read_bytes(), self.earlier[self.log])
        self.assertEqual(self.manifest.read_bytes(), self.earlier[self.manifest])
        self.assertEqual(self.names(self.out, "*.writing-*"), [], "a temporary record was left")
        self.assertEqual(self.names(self.out, "*_aborted-*"), [])
        self.assert_no_attempt_files(self.out)

    def test_incomplete_manifest_after_complete_promotion(self):
        # The canonical manifest's write fails partway (the stub writes part
        # of it, then fails): it goes to a temporary file, never in place
        real_dump, fired = json.dump, []

        def broken_dump(obj, fp, *args, **kwargs):
            if not fired:
                fired.append(fp.name)
                fp.write('{"conversion_metadata": {')
                raise OSError(28, "synthetic: no space left on device")
            return real_dump(obj, fp, *args, **kwargs)

        with mock.patch.object(video.json, "dump", broken_dump):
            self.assertEqual(self.main_exit("-i", str(self.iso)), 1)
        self.assertTrue(fired)
        self.assertTrue(any(line.startswith("Conversion failed: Run records could not be written")
                            for line in self.lines))
        self.assert_promoted_beside_earlier_records()
        self.assertEqual(verify.check_manifest(self.manifest).status, "SKIPPED")
        failed = self.record(self.out, "JPC_AV_00001_failed-*_manifest.json")
        status = failed["conversion_status"]
        self.assertEqual(status["overall_status"], "error")
        self.assertEqual(status["canonical_files_changed"], [str(self.mp4)])
        self.assertTrue(failed["titlesets"]["outputs"][0]["replaced_existing_canonical"])
        self.assertTrue(any("no longer match" in w and self.manifest.name in w and self.log.name in w
                            for w in status["warnings"]), status["warnings"])
        self.assertEqual(len(self.names(self.out, "JPC_AV_00001_failed-*.log.txt")), 1)
        failed_path = next(self.out.glob("JPC_AV_00001_failed-*_manifest.json"))
        self.assertEqual(verify.check_manifest(failed_path).status, "SKIPPED")

    def test_stale_records_after_complete_promotion_exit_1(self):
        # Every log write fails, the failure records' too: nothing is
        # recorded, and the screen says what the records would have
        calls = []

        def broken_log(converter, result, **kwargs):
            calls.append(kwargs.get("token"))
            raise OSError(28, "synthetic: no space left on device")

        with mock.patch.object(video.ISOToMP4Converter, "_create_log_file", broken_log):
            self.assertEqual(self.main_exit("-i", str(self.iso)), 1)
        self.assertTrue(any(line.startswith("Conversion failed: Run records could not be written")
                            for line in self.lines))
        self.assertTrue(any(line.startswith("Could not write failure records either")
                            for line in self.lines), self.lines)
        self.assertTrue(any(line.startswith("Not recorded: this run replaced or created JPC_AV_00001.mp4")
                            and "no longer match" in line for line in self.lines), self.lines)
        self.assert_promoted_beside_earlier_records()
        self.assertEqual(self.names(self.out, "*_failed-*"), [])
        self.assertEqual(len(calls), 2, "the log write was not attempted for both kinds of records")


if __name__ == "__main__":
    unittest.main()
