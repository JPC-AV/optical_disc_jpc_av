#!/usr/bin/env python3
"""Synthetic tests for makeiso.py's bundle replacement: the per-replacement
archive folder, rollback, Ctrl-C handling and truthful records (T-123,
T-124), plus overwrite-then-verify with verifyiso.py.

Standard library only. The drive is fake: disk info, tree listing, unmount,
copy, isolyzer and eject are synthetic stand-ins writing small byte strings
into a private directory. The guards in synthetic_support.py block child
processes, sockets, devices and writes outside TMPDIR; the only child is this
interpreter re-running this file with --sigint-child, so that a REAL SIGINT
can be delivered mid-publication without disturbing the test runner. No
disc, drive, mount or real media is involved.

Run:  python3 tests/test_makeiso_publication.py
OPTICAL_UNDER_TEST=/dir runs the same tests against another copy.
"""

import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "synthetic_support", Path(__file__).resolve().parent / "synthetic_support.py")
support = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(support)
support.install()

imaging = support.load("makeiso_under_test", "makeiso.py")
verify = support.load("verifyiso_under_test", "verifyiso.py")
imaging.logger.disabled = True

NAME = "disc"
CANONICAL = (f"{NAME}.iso", f"{NAME}.iso.log.txt", f"{NAME}_manifest.json",
             f"{NAME}_tree.txt", f"{NAME}_isolyzer.xml")


def hashes(data: bytes):
    return hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()


@contextlib.contextmanager
def synthetic_drive(payload: bytes, mismatch=False, finalize=None, tree=None, unmount=None,
                    create_fails=False):
    """Replace makeiso's native boundaries with synthetic ones"""
    def disk_info(self):
        return {"TotalSize": len(payload), "OpticalMediaType": "DVD-ROM",
                "MountPoint": "/Volumes/SYNTHETIC"}

    def tree_listing(self):
        if tree:
            tree(self)
        self.config.tree_path.write_text("Synthetic tree: 1 directories, 1 files\n")
        return True

    def create(self, size):
        if create_fails:
            raise OSError("synthetic: the attempt ISO could not be opened")
        self.config.output_path.write_bytes(payload)
        md5, sha = hashes(payload)
        iso_md5 = hashes(payload + b"!")[0] if mismatch else md5
        return imaging.IsoCreationResult(0.01, 0.01, md5, iso_md5, sha, sha,
                                         self.config.output_path)

    def analyze(self, path):
        self.config.isolyzer_path.write_text("<synthetic/>")
        return {"status_success": True, "valid_iso9660": True, "size_as_expected": True,
                "size_actual": len(payload), "size_expected": len(payload)}

    def eject(self):
        return finalize(self) if finalize else True

    with contextlib.ExitStack() as stack:
        for attr, value in (("_get_disk_info", disk_info), ("_generate_tree_listing", tree_listing),
                            ("_unmount_disk", unmount or (lambda self: True)),
                            ("_create_and_verify_iso", create),
                            ("_analyze_iso_structure", analyze), ("_finalize_disk", eject),
                            ("_confirm_overwrite", lambda self, paths: True)):
            stack.enter_context(mock.patch.object(imaging.OpticalDiscBackup, attr, value))
        stack.enter_context(mock.patch.object(imaging.os, "geteuid", lambda: 0))
        stack.enter_context(mock.patch.dict(os.environ, {}, clear=False))
        os.environ.pop("SUDO_USER", None)
        yield


def config(folder: Path, dry_run=False):
    return imaging.BackupConfig("disk9", "SYNTHETIC", NAME, "review", folder, dry_run=dry_run)


def backup(folder: Path, payload: bytes, **drive):
    with synthetic_drive(payload, **drive):
        obj = imaging.OpticalDiscBackup(config(folder))
        return obj, obj.create_backup()


def run_main(folder: Path, payload: bytes, dry_run=False, **drive) -> int:
    """makeiso's main() on the synthetic drive; returns its exit status"""
    args = argparse.Namespace(dry_run=dry_run, force=False, filename=NAME, dir=str(folder),
                              operator="review")
    with synthetic_drive(payload, **drive), \
            mock.patch.object(imaging, "parse_args", lambda: args), \
            mock.patch.object(imaging, "list_disks", lambda: "synthetic disk list"), \
            mock.patch.object(imaging, "gather_user_inputs", lambda a: config(folder, dry_run)):
        try:
            imaging.main()
        except SystemExit as e:
            return e.code
    return 0


def snapshot(folder: Path):
    """Every file under folder: relative path -> bytes"""
    return {str(p.relative_to(folder)): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def canonical(folder: Path):
    return {n: (folder / n).read_bytes() for n in CANONICAL if (folder / n).exists()}


def archives(folder: Path):
    return sorted(p for p in folder.iterdir() if p.is_dir() and "_overwritten-" in p.name)


def manifests_claiming_success(folder: Path, pattern: str):
    claims = []
    for m in folder.rglob(pattern):
        status = json.loads(m.read_text()).get("backup_status", {}).get("overall_status")
        if status in ("success", "success_with_warnings"):
            claims.append(m.name)
    return claims


def verify_cli(*paths):
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["verifyiso.py"] + [str(p) for p in paths]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            verify.main()
        except SystemExit as e:
            return (e.code if isinstance(e.code, int) else 1), out.getvalue()
    return 0, out.getvalue()


class _FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 24, 12, 0, 0)


STAMP = "20261024T120000"


def sequential_uuids():
    counter = iter(range(1, 1 << 24))
    return lambda: uuid.UUID(int=next(counter) << 104)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="makeiso-case-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._violations_before = len(support.VIOLATIONS)
        self.addCleanup(self._no_escapes)

    def _no_escapes(self):
        self.assertEqual(support.VIOLATIONS[self._violations_before:], [],
                         "a guarded operation was attempted")
        self.assertEqual(support.tripwire_calls(), "", "a native tool was executed")

    def old_bundle(self):
        obj, result = backup(self.dir, b"old verified synthetic master")
        self.assertTrue(result.success)
        return canonical(self.dir)


class PublicationTests(Base):
    """Healthy publication, the archive layout and file-error rollback"""

    def test_first_publication_creates_no_archive_folder(self):
        obj, result = backup(self.dir, b"first synthetic master")
        self.assertTrue(result.success)
        self.assertEqual(sorted(canonical(self.dir)), sorted(CANONICAL))
        self.assertEqual(archives(self.dir), [])
        self.assertEqual(verify.check_manifest(self.dir / f"{NAME}_manifest.json").status, "PASS")

    def test_replacement_archives_the_old_bundle_in_its_own_folder(self):
        old = self.old_bundle()
        obj, result = backup(self.dir, b"new verified synthetic master")
        self.assertTrue(result.success)
        folders = archives(self.dir)
        self.assertEqual(len(folders), 1, sorted(p.name for p in self.dir.iterdir()))
        self.assertRegex(folders[0].name, rf"^{NAME}_overwritten-\d{{8}}T\d{{6}}-[0-9a-f]{{6}}$")
        self.assertEqual({p.name: p.read_bytes() for p in folders[0].iterdir()}, old)
        self.assertEqual(canonical(self.dir)[f"{NAME}.iso"], b"new verified synthetic master")
        self.assertEqual(list(self.dir.glob("*_overwritten-*.*")), [], "sibling archive files")

    def test_overwrite_twice_then_verify_every_bundle(self):
        for payload in (b"master A", b"master B", b"master C"):
            obj, result = backup(self.dir, payload)
            self.assertTrue(result.success)
        self.assertEqual(len(archives(self.dir)), 2)
        code, output = verify_cli(self.dir)
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count("PASS: verified"), 3, output)
        # Tampering with an archived master fails that item only
        oldest = next(f for f in archives(self.dir) if (f / f"{NAME}.iso").read_bytes() == b"master A")
        (oldest / f"{NAME}.iso").write_bytes(b"master A, damaged")
        code, output = verify_cli(self.dir)
        self.assertEqual(code, 1, output)
        self.assertEqual(output.count("FAIL: hash mismatch"), 1, output)
        self.assertEqual(output.count("PASS: verified"), 2, output)
        self.assertIn(f"{oldest.name}/{NAME}.iso: FAIL", output)

    def fault(self, predicate, method="replace"):
        real = getattr(Path, method)
        tripped = []

        def faulty(path, target):
            if not tripped and predicate(Path(path), Path(target)):
                tripped.append(Path(path).name)
                raise OSError("synthetic publication failure")
            return real(path, target)

        self.addCleanup(lambda: self.assertTrue(tripped, "the fault never fired"))
        return mock.patch.object(Path, method, faulty)

    def assert_prior_bundle_back(self, old, result):
        self.assertFalse(result.success)
        self.assertEqual(canonical(self.dir), old)
        self.assertEqual(archives(self.dir), [], "an emptied archive folder was left")
        self.assertTrue(list(self.dir.glob(f"{NAME}_failed-*.iso.partial")))
        self.assertEqual(manifests_claiming_success(self.dir, "*_failed-*_manifest.json"), [])

    def test_early_publication_error_restores_the_prior_bundle(self):
        old = self.old_bundle()
        with self.fault(lambda src, dst: "attempt-" in src.name and src.name.endswith(".iso")):
            obj, result = backup(self.dir, b"new verified synthetic master")
        self.assert_prior_bundle_back(old, result)

    def test_late_publication_error_restores_the_prior_bundle(self):
        old = self.old_bundle()
        with self.fault(lambda src, dst: "attempt-" in src.name and src.name.endswith("_manifest.json")):
            obj, result = backup(self.dir, b"new verified synthetic master")
        self.assert_prior_bundle_back(old, result)

    def test_archive_rename_error_restores_the_prior_bundle(self):
        old = self.old_bundle()
        with self.fault(lambda src, dst: src.name == f"{NAME}_manifest.json"
                        and "_overwritten-" in str(dst), method="rename"):
            obj, result = backup(self.dir, b"new verified synthetic master")
        self.assert_prior_bundle_back(old, result)

    def test_restore_failing_partway_keeps_the_prior_bundle_whole(self):
        self.old_bundle()
        with self.fault(lambda src, dst: "attempt-" in src.name and src.name.endswith("_manifest.json")), \
                self.fault(lambda src, dst: "_overwritten-" in str(src) and src.name == f"{NAME}.iso"):
            obj, result = backup(self.dir, b"new verified synthetic master")
        self.assertFalse(result.success)
        folders = archives(self.dir)
        self.assertEqual(len(folders), 1)
        self.assertEqual(sorted(p.name for p in folders[0].iterdir()), sorted(CANONICAL))
        self.assertEqual(verify.check_manifest(folders[0] / f"{NAME}_manifest.json").status, "PASS")
        self.assertEqual(canonical(self.dir), {}, "a split prior bundle was left at canonical names")

    def test_continuing_rename_errors_report_where_the_files_really_are(self):
        # Publication fails, the old ISO cannot be restored, and every later
        # rename fails too: the prior files end up split, and the record must
        # say so rather than claim they are all back in the archive folder
        self.old_bundle()
        real_replace, real_rename = Path.replace, Path.rename
        state = {"restore_failed": False}

        def replace(path, target):
            if "attempt-" in Path(path).name and Path(path).name.endswith("_manifest.json"):
                raise OSError("synthetic publication error")
            if "_overwritten-" in str(path) and Path(path).name == f"{NAME}.iso":
                state["restore_failed"] = True
                raise OSError("synthetic restoration error")
            return real_replace(path, target)

        def rename(path, target):
            if state["restore_failed"]:
                raise OSError("synthetic continued rename failure")
            return real_rename(path, target)

        with mock.patch.object(Path, "replace", replace), mock.patch.object(Path, "rename", rename):
            obj, result = backup(self.dir, b"new verified synthetic master")
        self.assertFalse(result.success)
        folder = archives(self.dir)[0]
        self.assertEqual((folder / f"{NAME}.iso").read_bytes(), b"old verified synthetic master")
        restored = sorted(canonical(self.dir))
        self.assertNotIn(f"{NAME}.iso", restored)
        self.assertTrue(restored, "nothing was left at canonical names in this scenario")
        record = json.loads(next(self.dir.glob(f"{NAME}_failed-*_manifest.json")).read_text())
        warnings = record["backup_status"]["warnings"]
        self.assertFalse(any("all in" in w for w in warnings), warnings)
        split = [w for w in warnings if "left the prior files split" in w]
        self.assertEqual(len(split), 1, warnings)
        self.assertIn(f"in {folder.name}: {NAME}.iso", split[0])
        for name in restored:
            self.assertIn(name, split[0].split("at canonical names:")[1].split(";")[0])

    def test_copy_that_never_started_is_recorded_as_not_started(self):
        obj, result = backup(self.dir, b"never written", create_fails=True)
        self.assertFalse(result.success)
        failed = json.loads(next(self.dir.glob(f"{NAME}_failed-*_manifest.json")).read_text())
        creation = failed["operations_performed"]["iso_creation"]
        self.assertEqual((creation["performed"], creation["command"]), (False, None))
        log_text = next(self.dir.glob(f"{NAME}_failed-*.iso.log.txt")).read_text()
        self.assertIn("Status:  Not started", log_text)
        self.assertNotIn(f"of={self.dir / NAME}.iso", log_text)

    def test_archive_folder_collision_never_reuses_a_folder(self):
        old = self.old_bundle()
        taken = []
        for n in range(1, 4):
            folder = self.dir / f"{NAME}_overwritten-{STAMP}-{n:06x}"
            folder.mkdir()
            (folder / "someone-elses.txt").write_text("keep me")
            taken.append(folder)
        frozen = types.SimpleNamespace(datetime=_FrozenDatetime, timedelta=datetime.timedelta)
        with mock.patch.object(imaging, "datetime", frozen), \
                mock.patch.object(imaging, "uuid4", sequential_uuids()):
            obj, result = backup(self.dir, b"new verified synthetic master")
        self.assertFalse(result.success)
        self.assertEqual(canonical(self.dir), old)
        for folder in taken:
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["someone-elses.txt"])
        self.assertEqual(manifests_claiming_success(self.dir, "*_failed-*_manifest.json"), [])

    def test_rollback_never_replaces_an_occupied_slot(self):
        self.old_bundle()
        real_rename = Path.rename

        def stuck_iso(path, target):
            # Moving the published ISO back out of its canonical slot fails
            if Path(path).name == f"{NAME}.iso" and "attempt-" in Path(target).name:
                raise OSError("synthetic: cannot move the published ISO back")
            return real_rename(path, target)

        with self.fault(lambda src, dst: "attempt-" in src.name and src.name.endswith("_manifest.json")), \
                mock.patch.object(Path, "rename", stuck_iso):
            obj, result = backup(self.dir, b"new verified synthetic master")
        self.assertFalse(result.success)
        # Nothing was replaced and the prior bundle was not split: the new ISO
        # kept its slot, and the whole prior bundle stays in its archive folder
        self.assertEqual((self.dir / f"{NAME}.iso").read_bytes(), b"new verified synthetic master")
        self.assertFalse((self.dir / f"{NAME}_manifest.json").exists(),
                         "the prior manifest was restored beside the new ISO")
        folders = archives(self.dir)
        self.assertEqual(len(folders), 1)
        self.assertEqual(sorted(p.name for p in folders[0].iterdir()), sorted(CANONICAL))
        self.assertEqual((folders[0] / f"{NAME}.iso").read_bytes(), b"old verified synthetic master")
        self.assertEqual(verify.check_manifest(folders[0] / f"{NAME}_manifest.json").status, "PASS")
        failed = json.loads(next(self.dir.glob(f"{NAME}_failed-*_manifest.json")).read_text())
        self.assertTrue(any("prior bundle left intact" in w and folders[0].name in w
                            for w in failed["backup_status"]["warnings"]), failed["backup_status"])

    def test_failed_staged_success_never_leaves_a_success_record(self):
        self.old_bundle()
        real_log = imaging.create_formatted_log
        calls = []

        def log_once(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                raise OSError("synthetic: failure records cannot be written")
            return real_log(*args, **kwargs)

        with self.fault(lambda src, dst: "attempt-" in src.name and src.name.endswith(".iso")), \
                mock.patch.object(imaging, "create_formatted_log", log_once):
            obj, result = backup(self.dir, b"new verified synthetic master")
        self.assertFalse(result.success)
        self.assertEqual(manifests_claiming_success(self.dir, "*_failed-*_manifest.json"), [])
        self.assertEqual(manifests_claiming_success(self.dir, "*_attempt-*_manifest.json"), [])

    def test_ownership_fix_covers_the_archive_folder_not_archived_files(self):
        self.old_bundle()
        chowned = []

        def fake_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "chown":
                chowned.extend(cmd[2:])
                return subprocess.CompletedProcess(cmd, 0)
            raise AssertionError(f"unexpected command {cmd!r}")

        with synthetic_drive(b"new verified synthetic master"), \
                mock.patch.dict(os.environ, {"SUDO_USER": "synthetic-operator"}), \
                mock.patch.object(imaging.subprocess, "run", fake_run):
            obj = imaging.OpticalDiscBackup(config(self.dir))
            result = obj.create_backup()
        self.assertTrue(result.success)
        folders = archives(self.dir)
        self.assertEqual(len(folders), 1, "no archive folder")
        folder = folders[0]
        self.assertIn(str(folder), chowned)
        self.assertFalse(any(c.startswith(str(folder) + os.sep) for c in chowned), chowned)


# --- the child that receives a real SIGINT ---------------------------------

def _send_sigint_once(state):
    if not state.get("sent"):
        state["sent"] = True
        os.kill(os.getpid(), signal.SIGINT)
        # Give a default handler every chance to raise right here
        for _ in range(1000):
            pass


def _child_main(scenario: str, folder: Path) -> int:
    old = b"old verified synthetic master"
    new = b"new verified synthetic master"
    state = {}
    if scenario not in ("tree", "unmount", "eject_verified", "eject_mismatch", "dry_tree"):
        obj, result = backup(folder, old)
        assert result.success
    restore_exit = None
    if scenario == "after_hold" and hasattr(imaging, "DeferredInterrupt"):
        # SIGINT the moment the held section has put the old handler back
        real_exit = imaging.DeferredInterrupt.__exit__

        def exit_then_interrupt(self, *exc):
            outcome = real_exit(self, *exc)
            _send_sigint_once(state)
            return outcome

        restore_exit = mock.patch.object(imaging.DeferredInterrupt, "__exit__", exit_then_interrupt)
    real_replace, real_rename = Path.replace, Path.rename

    def replace(path, target):
        p, t = Path(path), Path(target)
        if scenario == "publish_mid" and "attempt-" in p.name and p.name.endswith("_tree.txt"):
            _send_sigint_once(state)
        if scenario == "rollback_mid" and "attempt-" in p.name and p.name.endswith("_manifest.json"):
            raise OSError("synthetic publication failure")
        if scenario == "rollback_mid" and "_overwritten-" in str(p) and p.name.endswith(".iso"):
            _send_sigint_once(state)
        outcome = real_replace(path, target)
        if "attempt-" in p.name and p.name.endswith("_manifest.json") and (
                scenario == "end_of_publish"
                or (scenario == "after_hold" and restore_exit is None)):
            # end_of_publish: still inside the held section; after_hold on a
            # version without one: the same moment, just after publication
            _send_sigint_once(state)
        return outcome

    def rename(path, target):
        p = Path(path)
        if scenario == "archive_mid" and p.name == f"{NAME}.iso.log.txt" and "_overwritten-" in str(target):
            _send_sigint_once(state)
        return real_rename(path, target)

    real_log = imaging.create_formatted_log

    def formatted_log(*args, **kwargs):
        if scenario == "records":
            _send_sigint_once(state)
        return real_log(*args, **kwargs)

    def eject(self):
        _send_sigint_once(state)
        return True

    def tree(self):
        _send_sigint_once(state)

    def unmount(self):
        _send_sigint_once(state)
        return True

    drive = {}
    if scenario == "unmount":
        drive["unmount"] = unmount
    if scenario.startswith("eject"):
        drive["finalize"] = eject
        drive["mismatch"] = scenario == "eject_mismatch"
    if scenario in ("tree", "dry_tree"):
        drive["tree"] = tree
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "replace", replace))
        stack.enter_context(mock.patch.object(Path, "rename", rename))
        stack.enter_context(mock.patch.object(imaging, "create_formatted_log", formatted_log))
        if restore_exit is not None:
            stack.enter_context(restore_exit)
        try:
            code = run_main(folder, new, dry_run=scenario == "dry_tree", **drive)
        finally:
            print("SIGINT_SENT:", bool(state.get("sent")))
    print("HANDLER_RESTORED:", signal.getsignal(signal.SIGINT) is signal.default_int_handler)
    return code


def run_child(scenario: str, folder: Path):
    env = {"HOME": os.environ.get("HOME", str(folder)), "TMPDIR": tempfile.gettempdir(),
           "PATH": "/nonexistent", "OPTICAL_UNDER_TEST": str(support.ROOT)}
    proc = subprocess.run([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                           "--sigint-child", scenario, str(folder)],
                          env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


class InterruptTests(Base):
    """A real SIGINT, delivered inside a guarded child at each boundary"""

    def child(self, scenario):
        code, output = run_child(scenario, self.dir)
        self.assertNotIn("VIOLATION", output)
        self.assertIn("SIGINT_SENT: True", output, "the scenario never delivered its SIGINT")
        return code, output

    def assert_new_bundle_published(self):
        self.assertEqual(canonical(self.dir)[f"{NAME}.iso"], b"new verified synthetic master")
        self.assertEqual(sorted(canonical(self.dir)), sorted(CANONICAL))
        manifest = json.loads((self.dir / f"{NAME}_manifest.json").read_text())
        self.assertEqual(manifest["backup_status"]["overall_status"], "success")
        folders = archives(self.dir)
        self.assertEqual(len(folders), 1)
        self.assertEqual((folders[0] / f"{NAME}.iso").read_bytes(), b"old verified synthetic master")
        self.assertEqual(verify_cli(self.dir)[0], 0)

    def test_sigint_while_archiving_is_held_until_publication_finishes(self):
        code, output = self.child("archive_mid")
        self.assertEqual(code, 130, output)
        self.assert_new_bundle_published()
        self.assertIn("HANDLER_RESTORED: True", output)

    def test_sigint_while_publishing_is_held_until_publication_finishes(self):
        code, output = self.child("publish_mid")
        self.assertEqual(code, 130, output)
        self.assert_new_bundle_published()

    def test_sigint_at_the_last_rename_keeps_the_published_bundle(self):
        code, output = self.child("end_of_publish")
        self.assertEqual(code, 130, output)
        self.assert_new_bundle_published()
        self.assertEqual(list(self.dir.glob(f"{NAME}_failed-*")), [])

    def test_sigint_just_after_the_held_section_keeps_the_published_bundle(self):
        code, output = self.child("after_hold")
        self.assertEqual(code, 130, output)
        self.assert_new_bundle_published()
        self.assertEqual(list(self.dir.glob(f"{NAME}_failed-*")), [])

    def test_sigint_in_a_dry_run_stops_it_as_before(self):
        old = self.old_bundle()
        code, output = self.child("dry_tree")
        self.assertEqual(code, -signal.SIGINT, output)
        self.assertEqual({n: d for n, d in canonical(self.dir).items()
                          if n != f"{NAME}_tree.txt"},
                         {n: d for n, d in old.items() if n != f"{NAME}_tree.txt"})

    def test_sigint_during_rollback_lets_the_rollback_finish(self):
        code, output = self.child("rollback_mid")
        self.assertEqual(code, 1, output)
        self.assertEqual(canonical(self.dir)[f"{NAME}.iso"], b"old verified synthetic master")
        self.assertEqual(sorted(canonical(self.dir)), sorted(CANONICAL))
        self.assertEqual(archives(self.dir), [])
        self.assertEqual(manifests_claiming_success(self.dir, "*_failed-*_manifest.json"), [])

    def test_sigint_while_writing_records_is_a_recorded_failure(self):
        code, output = self.child("records")
        self.assertEqual(code, 1, output)
        self.assertEqual(canonical(self.dir)[f"{NAME}.iso"], b"old verified synthetic master")
        self.assertEqual(archives(self.dir), [])
        self.assertEqual(list(self.dir.glob("*attempt-*")), [], "attempt files left unmarked")
        self.assertEqual(manifests_claiming_success(self.dir, "*_failed-*_manifest.json"), [])
        failed = list(self.dir.glob(f"{NAME}_failed-*_manifest.json"))
        self.assertEqual(len(failed), 1)
        self.assertIn("Interrupted by operator (Ctrl-C)",
                      json.loads(failed[0].read_text())["backup_status"]["errors"])

    def test_sigint_during_eject_after_a_verified_copy_keeps_it_verified(self):
        code, output = self.child("eject_verified")
        self.assertEqual(code, 130, output)
        manifest = json.loads((self.dir / f"{NAME}_manifest.json").read_text())
        status = manifest["backup_status"]
        self.assertEqual(status["overall_status"], "success_with_warnings")
        self.assertIn("disc eject interrupted by operator (Ctrl-C)", status["warnings"])
        log_text = (self.dir / f"{NAME}.iso.log.txt").read_text()
        self.assertIn("disc eject interrupted by operator (Ctrl-C)", log_text)
        self.assertIn("INTERRUPTED (Ctrl-C during the eject)", log_text)
        self.assertEqual(manifest["operations_performed"]["finalization"]["status"], "interrupted")
        self.assertEqual(verify_cli(self.dir)[0], 0)

    def test_sigint_during_eject_after_a_mismatch_keeps_the_failure(self):
        code, output = self.child("eject_mismatch")
        self.assertEqual(code, 2, output)
        self.assertFalse((self.dir / f"{NAME}.iso").exists())
        failed = json.loads(next(self.dir.glob(f"{NAME}_failed-*_manifest.json")).read_text())
        self.assertEqual(failed["backup_status"]["overall_status"], "verification_failed")
        self.assertIn("disc eject interrupted by operator (Ctrl-C)", failed["backup_status"]["warnings"])

    def test_sigint_during_unmount_is_recorded_as_interrupted(self):
        code, output = self.child("unmount")
        self.assertEqual(code, 1, output)
        failed = json.loads(next(self.dir.glob(f"{NAME}_failed-*_manifest.json")).read_text())
        self.assertEqual(failed["operations_performed"]["disk_unmount"]["status"], "interrupted")
        log_text = next(self.dir.glob(f"{NAME}_failed-*.iso.log.txt")).read_text()
        self.assertIn("INTERRUPTED (Ctrl-C; the disc may or may not be unmounted)", log_text)

    def test_sigint_during_tree_listing_is_a_recorded_failure(self):
        code, output = self.child("tree")
        self.assertEqual(code, 1, output)
        self.assertEqual(list(self.dir.glob("*attempt-*")), [], "attempt files left unmarked")
        failed_path = next(self.dir.glob(f"{NAME}_failed-*_manifest.json"))
        failed = json.loads(failed_path.read_text())
        self.assertEqual(failed["backup_status"]["overall_status"], "failed")
        self.assertIn("Interrupted by operator (Ctrl-C)", failed["backup_status"]["errors"])
        steps = failed["operations_performed"]
        self.assertEqual(steps["disk_unmount"]["status"], "not_attempted")
        self.assertIsNone(steps["iso_creation"]["command"])
        self.assertEqual(steps["finalization"]["status"], "skipped")
        log_text = next(self.dir.glob(f"{NAME}_failed-*.iso.log.txt")).read_text()
        self.assertNotIn("Skipped (dry run)", log_text)
        self.assertNotIn("eject error", log_text)
        self.assertIn("Status:  Not started", log_text)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--sigint-child":
        try:
            exit_code = _child_main(sys.argv[2], Path(sys.argv[3]))
        finally:
            if support.VIOLATIONS:
                print("VIOLATIONS:", support.VIOLATIONS)
        sys.exit(exit_code)
    unittest.main()
