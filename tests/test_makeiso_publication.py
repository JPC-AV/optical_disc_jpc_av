#!/usr/bin/env python3
"""Synthetic tests for makeiso.py's bundle replacement: the per-replacement
archive folder, rollback, Ctrl-C handling and truthful records (T-123,
T-124), plus overwrite-then-verify with verifyiso.py, and dry runs that never
change an existing file, a symlink's target or ownership (T-128). Also failed
runs whose records name where each of their files actually is after failed or
interrupted renames (T-131), unreadable metadata that is recorded as unknown
rather than absent, and diskutil or chown that cannot be started (T-133).

Standard library only. The drive is fake: disk info, tree listing, unmount,
copy, isolyzer and eject are synthetic stand-ins writing small byte strings
into a private directory (dry-run tests keep makeiso's own tree-listing
method and fake only the tree command, chown and ownership calls; native-launch
tests keep makeiso's own unmount, eject and ownership fix and fake only
subprocess.run). The guards in synthetic_support.py block child
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
import errno
import hashlib
import importlib.util
import io
import json
import os
import plistlib
import re
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
                    create_fails=False, real_tree=False, confirm=None, sudo_user=None,
                    native_disk=False, copy=None):
    """Replace makeiso's native boundaries with synthetic ones. real_tree keeps
    makeiso's own tree-listing method (the caller then fakes subprocess.run
    for the tree command); confirm replaces the overwrite prompt; sudo_user
    sets SUDO_USER inside the drive; native_disk keeps makeiso's own unmount
    and eject (the caller fakes subprocess.run for diskutil); copy(self) runs
    as the copy starts."""
    def disk_info(self):
        return {"TotalSize": len(payload), "OpticalMediaType": "DVD-ROM",
                "MountPoint": "/Volumes/SYNTHETIC"}

    def tree_listing(self):
        if tree:
            tree(self)
        self.config.tree_path.write_text("Synthetic tree: 1 directories, 1 files\n")
        return True

    def create(self, size):
        if copy:
            copy(self)
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
                            ("_confirm_overwrite", confirm or (lambda self, paths: True))):
            if attr == "_generate_tree_listing" and real_tree:
                continue
            if attr in ("_unmount_disk", "_finalize_disk") and native_disk:
                continue
            stack.enter_context(mock.patch.object(imaging.OpticalDiscBackup, attr, value))
        stack.enter_context(mock.patch.object(imaging.os, "geteuid", lambda: 0))
        stack.enter_context(mock.patch.dict(os.environ, {}, clear=False))
        os.environ.pop("SUDO_USER", None)
        if sudo_user:
            os.environ["SUDO_USER"] = sudo_user
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


# --- dry runs (T-128) -------------------------------------------------------

TREE, LOG, MANIFEST = f"{NAME}_tree.txt", f"{NAME}.iso.log.txt", f"{NAME}_manifest.json"
DRY_RUN_FILES = (TREE, LOG, MANIFEST)  # in the order a dry run creates them
LISTING = "Synthetic tree: 1 directories, 1 files\n"
OPERATOR_UID = 4242
REAL_OPEN, REAL_FSTAT = open, os.fstat


def state(folder: Path) -> dict:
    """Every entry under folder, never following a link: relative path ->
    (kind, bytes or link target, mode, size, mtime_ns, uid, gid, inode)"""
    out = {}
    for root, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            p = Path(root) / name
            st = os.lstat(p)
            if os.path.islink(p):
                kind, data = "link", os.readlink(p)
            elif p.is_dir():
                kind, data = "dir", None
            else:
                kind, data = "file", p.read_bytes()
            out[str(p.relative_to(folder))] = (kind, data, st.st_mode, st.st_size,
                                               st.st_mtime_ns, st.st_uid, st.st_gid, st.st_ino)
    return out


def ident(path: Path):
    st = os.lstat(path)
    return st.st_dev, st.st_ino


class FakeTools:
    """subprocess.run for makeiso's own tree-listing method and its chown:
    every call is recorded; the tree command writes LISTING into the file it
    is given, or fails as asked. With disk=True it also answers diskutil
    unmountDisk, eject and info (verbs recorded in .disk). launch_error names
    one verb (unmountDisk, eject, info or chown) that cannot be started:
    FileNotFoundError, as when the program is missing. exit_status maps a
    verb to a nonzero status."""

    def __init__(self, status=0, missing=False, disk=False, launch_error=None, exit_status=None):
        self.status, self.missing = status, missing
        self.trees, self.chowned = [], []
        self.disk_enabled, self.launch_error = disk, launch_error
        self.exit_status, self.disk = exit_status or {}, []

    def run(self, cmd, *args, **kwargs):
        cmd = [str(c) for c in cmd]
        if cmd[0] == "chown" or (cmd[0] == "diskutil" and self.disk_enabled):
            verb = cmd[1] if cmd[0] == "diskutil" else "chown"
            if cmd[0] == "diskutil":
                self.disk.append(verb)
            if verb == self.launch_error:
                raise FileNotFoundError(2, "No such file or directory (synthetic)", cmd[0])
            if cmd[0] == "chown":
                self.chowned.append(cmd[2:])
            code = self.exit_status.get(verb, 0)
            if code and kwargs.get("check"):
                raise subprocess.CalledProcessError(code, cmd, "", "synthetic failure")
            if verb == "info":
                return subprocess.CompletedProcess(cmd, 0, plistlib.dumps({"MountPoint": "/Volumes/SYNTHETIC"}), b"")
            return subprocess.CompletedProcess(cmd, code, "", "synthetic failure" if code else "")
        if cmd[0] != "tree":
            raise AssertionError(f"unexpected command {cmd!r}")
        self.trees.append(cmd)
        if self.missing:
            raise FileNotFoundError(2, "No such file or directory", "tree")
        kwargs["stdout"].write(LISTING)
        return subprocess.CompletedProcess(cmd, self.status)


class FailingFile:
    """A file whose writes or close fail (disk full), as asked"""

    def __init__(self, f, fail_on):
        self.f, self.fail_on = f, fail_on

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()
        if self.fail_on == "close" and exc[0] is None:
            raise OSError(28, "synthetic: no space left on device")
        return False

    def fileno(self):
        return self.f.fileno()

    def write(self, data):
        if self.fail_on == "write":
            raise OSError(28, "synthetic: no space left on device")
        return self.f.write(data)

    def flush(self):
        return self.f.flush()

    def close(self):
        self.f.close()


@contextlib.contextmanager
def at_creation(path: Path, action):
    """Call action(file, mode, args, kwargs) just before makeiso opens path for
    writing: the dry-run creation boundary. A non-None return replaces the file."""
    def hooked_open(file, mode="r", *args, **kwargs):
        if Path(file) == path and mode in ("x", "w"):
            replacement = action(file, mode, args, kwargs)
            if replacement is not None:
                return replacement
        return REAL_OPEN(file, mode, *args, **kwargs)
    with mock.patch.object(imaging, "open", hooked_open, create=True):
        yield


@contextlib.contextmanager
def fake_ownership(before=None, foreign=()):
    """Ownership as if run as root under sudo: the operator's uid comes from a
    fake pwd lookup; fchown is recorded with the identity of the object behind
    the descriptor (before(fd) runs first); fstat reports this process's
    objects as root's, except the identities in foreign. Ownership must never
    change by path."""
    calls = []

    def fchown(fd, uid, gid):
        if before:
            before(fd)
        st = REAL_FSTAT(fd)
        calls.append(((st.st_dev, st.st_ino), uid, gid))

    def fstat(fd):
        st = REAL_FSTAT(fd)
        values = list(st[:10])
        values[4] = 501 if (st.st_dev, st.st_ino) in foreign else 0
        return os.stat_result(values)

    def by_path(*args, **kwargs):
        raise AssertionError(f"ownership changed by path: {args!r}")

    with mock.patch("pwd.getpwnam", lambda name: types.SimpleNamespace(pw_uid=OPERATOR_UID)), \
            mock.patch.object(imaging.os, "fchown", fchown), \
            mock.patch.object(imaging.os, "fstat", fstat), \
            mock.patch.object(imaging.os, "chown", by_path), \
            mock.patch.object(imaging.os, "lchown", by_path):
        yield calls


class DryRunTests(Base):
    """T-128: a dry run never changes an existing file, a symlink's target or
    anything's ownership; it creates its own tree, log and manifest exclusively"""

    def setUp(self):
        super().setUp()
        self.tools = FakeTools()
        self.prompts = []

        def confirm(obj, paths):
            self.prompts.append(sorted(p.name for p in paths))
            return True
        self.confirm = confirm

    def dry_main(self, folder: Path, sudo_user=None):
        """makeiso's main() as a dry run into folder: (exit status, log lines)"""
        lines = []
        with mock.patch.object(imaging.subprocess, "run", self.tools.run), \
                mock.patch.object(imaging, "log", lines.append):
            code = run_main(folder, b"", dry_run=True, real_tree=True,
                            confirm=self.confirm, sudo_user=sudo_user)
        return code, lines

    def stop_message(self, lines) -> str:
        found = [l for l in lines if l.startswith(("Dry run refused:", "Dry run stopped:"))]
        self.assertEqual(len(found), 1, lines)
        return found[0]

    def case(self, label: str) -> Path:
        folder = self.dir / label
        folder.mkdir()
        return folder

    def assert_refused(self, before, code, lines):
        self.assertEqual(code, 1, lines)
        self.assertEqual(state(self.dir), before, "something that existed changed")
        self.assertEqual((self.tools.trees, self.prompts), ([], []), "tree or a prompt ran")
        self.assertTrue(self.stop_message(lines).startswith("Dry run refused:"))

    def assert_lists_own_files(self, message: str, own):
        if own:
            self.assertIn("Files this dry run created", message)
            self.assertTrue(message.endswith(": " + ", ".join(str(p) for p in own)), message)
        else:
            self.assertTrue(message.endswith("It created no files."), message)

    # refusal before anything is written

    def test_each_canonical_name_refuses_the_dry_run(self):
        for name in CANONICAL:
            for kind in ("file", "empty", "folder"):
                with self.subTest(name=name, kind=kind):
                    folder = self.case(f"{kind}-{name}")
                    if kind == "folder":
                        (folder / name).mkdir()
                    else:
                        (folder / name).write_bytes(b"" if kind == "empty" else b"existing " + name.encode())
                    before = state(self.dir)
                    code, lines = self.dry_main(folder)
                    self.assert_refused(before, code, lines)
                    self.assertIn(name, self.stop_message(lines))

    def test_a_whole_bundle_refuses_the_dry_run(self):
        self.old_bundle()
        before = state(self.dir)
        code, lines = self.dry_main(self.dir)
        self.assert_refused(before, code, lines)
        self.assertEqual(verify_cli(self.dir)[0], 0, "the existing master no longer verifies")

    def test_live_and_broken_symlinks_refuse_and_their_targets_stay(self):
        targets = self.case("targets")
        for i, name in enumerate(CANONICAL):
            for kind in ("live", "broken"):
                with self.subTest(name=name, kind=kind):
                    folder = self.case(f"{kind}-link-{i}")
                    target = targets / f"{kind}-{i}"
                    if kind == "live":
                        target.write_bytes(b"symlink target " + name.encode())
                    (folder / name).symlink_to(target)
                    before = state(self.dir)
                    code, lines = self.dry_main(folder)
                    self.assert_refused(before, code, lines)

    # successful dry runs

    def test_fresh_missing_and_empty_folders_succeed(self):
        for folder in (self.dir / "missing" / "deeper", self.case("empty")):
            with self.subTest(folder=folder.name):
                code, lines = self.dry_main(folder)
                self.assertEqual(code, 0, lines)
                self.assertEqual(sorted(os.listdir(folder)), sorted(DRY_RUN_FILES))
                self.assertEqual((folder / TREE).read_text(), LISTING)
                manifest = json.loads((folder / MANIFEST).read_text())
                self.assertEqual(manifest["backup_status"]["overall_status"], "dry_run")
                checked = verify.check_manifest(folder / MANIFEST)
                self.assertEqual((checked.status, checked.detail),
                                 ("SKIPPED", "dry-run record — no master was made"))
                self.assertEqual(verify_cli(folder)[0], 0)
                self.assertEqual(self.prompts, [])

    def test_unrelated_contents_are_allowed_and_untouched(self):
        folder = self.case("shared")
        (folder / "notes.txt").write_text("operator notes\n")
        (folder / "other_disc.iso").write_bytes(b"another master")
        (folder / f"{NAME}_failed-20261024T120000-abcdef_manifest.json").write_text("{}")
        archive = folder / f"{NAME}_overwritten-20261024T101500-7b01de"
        archive.mkdir()
        (archive / f"{NAME}.iso").write_bytes(b"archived master")
        before = state(folder)
        code, lines = self.dry_main(folder)
        self.assertEqual(code, 0, lines)
        after = state(folder)
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        self.assertEqual(sorted(set(after) - set(before)), sorted(DRY_RUN_FILES))

    # names taken during the run, and failures

    def test_a_name_taken_just_before_its_creation_stops_the_run(self):
        targets = self.case("targets")
        for i, name in enumerate(DRY_RUN_FILES):
            for kind in ("file", "symlink"):
                with self.subTest(name=name, kind=kind):
                    folder = self.case(f"taken-{kind}-{i}")
                    target = targets / f"target-{kind}-{i}"
                    target.write_bytes(b"someone else's file")

                    def take(file, mode, args, kwargs, path=folder / name, target=target, kind=kind):
                        if kind == "file":
                            path.write_bytes(b"appeared meanwhile")
                        else:
                            path.symlink_to(target)

                    with at_creation(folder / name, take):
                        code, lines = self.dry_main(folder)
                    self.assertEqual(code, 1, lines)
                    message = self.stop_message(lines)
                    self.assertIn(f"{name} appeared", message)
                    if kind == "file":
                        self.assertEqual((folder / name).read_bytes(), b"appeared meanwhile")
                    else:
                        self.assertEqual(os.readlink(folder / name), str(target))
                    self.assertEqual(target.read_bytes(), b"someone else's file")
                    own = [folder / n for n in DRY_RUN_FILES[:i]]
                    self.assertEqual(sorted(os.listdir(folder)), sorted([name] + [p.name for p in own]))
                    self.assert_lists_own_files(message, own)

    def test_creation_write_and_close_failures_stop_with_exit_1(self):
        def fail_create(file, mode, args, kwargs):
            raise PermissionError(13, "synthetic: permission denied", str(file))

        def failing(fail_on):
            return lambda file, mode, args, kwargs: FailingFile(REAL_OPEN(file, mode, *args, **kwargs),
                                                                fail_on)

        cases = (("create", TREE, fail_create, ()), ("close", TREE, failing("close"), (TREE,)),
                 ("write", LOG, failing("write"), (TREE, LOG)),
                 ("close", MANIFEST, failing("close"), (TREE, LOG, MANIFEST)))
        for failure, name, action, own in cases:
            with self.subTest(failure=failure, name=name):
                folder = self.case(f"{failure}-{name}")
                with at_creation(folder / name, action):
                    code, lines = self.dry_main(folder)
                self.assertEqual(code, 1, lines)
                message = self.stop_message(lines)
                self.assertIn("synthetic:", message)
                self.assertEqual(sorted(os.listdir(folder)), sorted(own))
                self.assert_lists_own_files(message, [folder / n for n in own])

    def test_a_failing_or_missing_tree_command_stops_with_exit_1(self):
        for label, tools, words in (("status", FakeTools(status=1), "tree exited with status 1"),
                                    ("missing", FakeTools(missing=True), "No such file or directory")):
            with self.subTest(label):
                self.tools = tools
                folder = self.case(f"tree-{label}")
                code, lines = self.dry_main(folder)
                self.assertEqual(code, 1, lines)
                message = self.stop_message(lines)
                self.assertIn(words, message)
                self.assertEqual(os.listdir(folder), [TREE])
                self.assert_lists_own_files(message, [folder / TREE])

    # ownership: only objects this run created, through their descriptors

    def test_ownership_changes_only_through_descriptors_of_created_files(self):
        # A folder the dry run creates keeps its owner, with a warning
        for label in ("created", "existing"):
            with self.subTest(folder=label):
                folder = self.dir / f"{label}-folder"
                if label == "existing":
                    folder.mkdir()
                with fake_ownership() as calls:
                    code, lines = self.dry_main(folder, sudo_user="synthetic-operator")
                self.assertEqual(code, 0, lines)
                self.assertEqual([c[0] for c in calls], [ident(folder / n) for n in DRY_RUN_FILES])
                self.assertTrue(all(c[1:] == (OPERATOR_UID, -1) for c in calls), calls)
                self.assertEqual(self.tools.chowned, [], "ownership changed by name")
                warned = [l for l in lines if "keeps its owner (root)" in l]
                self.assertEqual(len(warned), 1 if label == "created" else 0, lines)

    def test_a_created_file_replaced_before_its_owner_change_is_left_alone(self):
        folder = self.case("swapped")
        elsewhere = self.case("elsewhere")
        target = elsewhere / "target"
        target.write_bytes(b"not this run's file")
        before = state(elsewhere)
        tree = folder / TREE
        created = {}

        def swap(fd):
            st = REAL_FSTAT(fd)
            if not created and os.path.lexists(tree) and ident(tree) == (st.st_dev, st.st_ino):
                created["tree"] = (st.st_dev, st.st_ino)
                tree.unlink()
                tree.symlink_to(target)

        with fake_ownership(before=swap) as calls:
            code, lines = self.dry_main(folder, sudo_user="synthetic-operator")
        self.assertIn("tree", created, "no owner change ran on the created tree listing")
        self.assertEqual(code, 0, lines)
        self.assertEqual(os.readlink(tree), str(target))
        self.assertEqual(state(elsewhere), before)
        owned = [c[0] for c in calls]
        self.assertIn(created["tree"], owned)
        self.assertNotIn(ident(target), owned)
        self.assertEqual(self.tools.chowned, [])

    def test_a_folder_substituted_after_mkdir_never_changes_owner(self):
        # mkdir gives no descriptor: whatever is then at the path - a
        # preexisting folder with the same (root) owner or a different one, or
        # a symlink to one - keeps its owner, and so does the folder made
        real_mkdir = Path.mkdir
        for kind in ("same owner", "different owner", "symlink"):
            with self.subTest(kind=kind):
                label = kind.replace(" ", "-")
                folder = self.dir / f"requested-{label}"
                preexisting = self.case(f"preexisting-{label}")
                (preexisting / "keep.txt").write_text("preexisting unrelated material\n")
                replacement = ident(preexisting)
                foreign = {replacement} if kind == "different owner" else set()
                made = []

                def mkdir(path, *args, folder=folder, preexisting=preexisting, kind=kind,
                          label=label, made=made, **kwargs):
                    real_mkdir(path, *args, **kwargs)
                    if path == folder:
                        made.append(ident(folder))
                        folder.rename(self.dir / f"actually-created-{label}")
                        if kind == "symlink":
                            folder.symlink_to(preexisting, target_is_directory=True)
                        else:
                            preexisting.rename(folder)

                with mock.patch.object(Path, "mkdir", mkdir), \
                        fake_ownership(foreign=foreign) as calls:
                    code, lines = self.dry_main(folder, sudo_user="synthetic-operator")
                owned = [c[0] for c in calls]
                self.assertTrue(made, "the substitution point was never reached")
                self.assertNotIn(replacement, owned, "the substituted folder changed owner")
                self.assertNotIn(made[0], owned, "the folder this run made changed owner")
                self.assertTrue(any("keeps its owner (root)" in l for l in lines), lines)
                now = preexisting if kind == "symlink" else folder
                self.assertEqual(ident(now), replacement)
                self.assertEqual((now / "keep.txt").read_text(), "preexisting unrelated material\n")
                if kind == "symlink":
                    self.assertEqual(code, 1, lines)  # no longer a real folder: refused
                    self.assertEqual(calls, [])
                else:
                    self.assertEqual(code, 0, lines)
                    self.assertEqual(owned, [ident(folder / n) for n in DRY_RUN_FILES])
                self.assertEqual(self.tools.chowned, [])

    # normal runs keep their behavior

    def test_normal_replacement_still_asks_and_archives(self):
        self.old_bundle()
        with synthetic_drive(b"new verified synthetic master", confirm=self.confirm):
            result = imaging.OpticalDiscBackup(config(self.dir)).create_backup()
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(self.prompts, [sorted(CANONICAL)])
        self.assertEqual(len(archives(self.dir)), 1)
        self.assertEqual(canonical(self.dir)[f"{NAME}.iso"], b"new verified synthetic master")

    def test_normal_tree_failure_is_still_only_a_warning(self):
        self.tools = FakeTools(status=1)
        with synthetic_drive(b"synthetic master", real_tree=True), \
                mock.patch.object(imaging.subprocess, "run", self.tools.run):
            result = imaging.OpticalDiscBackup(config(self.dir)).create_backup()
        self.assertTrue(result.success, result.error_message)
        self.assertIn("tree listing failed or incomplete", result.warnings)
        self.assertEqual(sorted(canonical(self.dir)), sorted(CANONICAL))
        manifest = json.loads((self.dir / MANIFEST).read_text())
        self.assertEqual(manifest["backup_status"]["overall_status"], "success_with_warnings")


# --- truthful receipts after failed renames (T-131) --------------------------

OLD = b"old verified synthetic master"           # 29 bytes
NEW = b"new verified synthetic master, longer"   # 37 bytes: sizes tell them apart
ISO, LOGNAME, TREENAME, ISOLYZER = (f"{NAME}.iso", f"{NAME}.iso.log.txt", f"{NAME}_tree.txt",
                                    f"{NAME}_isolyzer.xml")


def one(folder: Path, pattern: str) -> Path:
    found = sorted(folder.glob(pattern))
    assert len(found) == 1, (pattern, sorted(p.name for p in folder.iterdir()))
    return found[0]


def chowned_paths(tools) -> list:
    return [path for call in tools.chowned for path in call]


class ReceiptTests(Base):
    """A failed run's records name where each of this run's files actually
    is, whatever renames failed or were interrupted; a staged success record
    never survives unremarked; failure marking resumes under one token"""

    fault = PublicationTests.fault

    def subfolder(self, label: str, seed=True) -> Path:
        folder = self.dir / label
        folder.mkdir()
        if seed:
            obj, result = backup(folder, OLD)
            self.assertTrue(result.success)
        return folder

    def assert_receipt_locates(self, folder, iso, tree, isolyzer, payload):
        record_path = one(folder, f"{NAME}_failed-*_manifest.json")
        log_path = one(folder, f"{NAME}_failed-*.iso.log.txt")
        record = json.loads(record_path.read_text())
        files = record["output_files"]
        self.assertEqual((files["iso_filename"], files["tree_filename"], files["isolyzer_filename"],
                          files["log_filename"], files["manifest_filename"]),
                         (iso.name, tree.name, isolyzer.name, log_path.name, record_path.name))
        for path in (iso, tree, isolyzer):
            self.assertTrue(path.is_file(), f"{path.name} is named but absent")
        self.assertEqual(iso.read_bytes(), payload)
        self.assertEqual(files["iso_file_size_bytes"], len(payload))
        self.assertIs(record["backup_status"]["iso_created"], True)
        self.assertEqual(record["integrity_verification"]["sha256_iso_hash"], hashes(payload)[1])
        self.assertEqual(record["technical_details"]["backup_parameters"]["final_destination"], str(iso))
        tree_op = record["operations_performed"]["tree_listing"]
        self.assertEqual((tree_op["output_file"], tree_op["summary"]),
                         (tree.name, {"total_directories": 1, "total_files": 1}))
        log_text = log_path.read_text()
        for label, path in (("ISO File:", iso), ("Tree File:", tree), ("Isolyzer File:", isolyzer)):
            self.assertIn(f"{label:<18}{path.name}\n", log_text)
        self.assertIn("ISO File Size:", log_text)
        self.assertIn(f"  Output:  {isolyzer.name}\n", log_text)
        self.assertEqual(manifests_claiming_success(folder, "*_failed-*_manifest.json"), [])
        return record, log_text

    def assert_prior_bundle_archived_whole(self, folder):
        folders = archives(folder)
        self.assertEqual(len(folders), 1)
        self.assertEqual(sorted(p.name for p in folders[0].iterdir()), sorted(CANONICAL))
        self.assertEqual((folders[0] / ISO).read_bytes(), OLD)
        self.assertEqual(verify.check_manifest(folders[0] / f"{NAME}_manifest.json").status, "PASS")
        return folders[0]

    def publish_failing_with(self, folder, stuck, unlink_error=None, sudo_user=None):
        """Publication fails at the manifest; moving `stuck` back off its
        canonical name fails; unlink_error is raised for a canonical stuck log"""
        real_rename, real_unlink = Path.rename, Path.unlink
        state = {}

        def rename(path, target):
            if Path(path) == folder / stuck and "attempt-" in Path(target).name:
                state["stuck"] = Path(path).read_bytes()
                raise OSError("synthetic: cannot move it back")
            return real_rename(path, target)

        def unlink(path, *args, **kwargs):
            if unlink_error is not None and Path(path) == folder / stuck:
                raise unlink_error
            return real_unlink(path, *args, **kwargs)

        tools = FakeTools()
        with self.fault(lambda src, dst: "attempt-" in src.name and src.name.endswith("_manifest.json")), \
                mock.patch.object(Path, "rename", rename), mock.patch.object(Path, "unlink", unlink), \
                mock.patch.object(imaging.subprocess, "run", tools.run):
            obj, result = backup(folder, NEW, sudo_user=sudo_user)
        self.assertIn("stuck", state, "the move-back fault never fired")
        return obj, result, tools, state

    def test_rollback_leaving_a_file_at_its_canonical_name_records_it_there(self):
        for stuck in (ISO, TREENAME, ISOLYZER):
            with self.subTest(stuck):
                folder = self.subfolder(stuck.replace(".", "-"))
                obj, result, tools, _ = self.publish_failing_with(folder, stuck,
                                                                  sudo_user="synthetic-operator")
                self.assertFalse(result.success)
                archive = self.assert_prior_bundle_archived_whole(folder)
                self.assertEqual(sorted(canonical(folder)), [stuck])
                self.assertEqual(sorted(folder.glob("*attempt-*")), [])
                located = {name: folder / name if name == stuck else
                           one(folder, f"{NAME}_failed-*{'.iso.partial' if name == ISO else name[len(NAME):]}")
                           for name in (ISO, TREENAME, ISOLYZER)}
                record, _ = self.assert_receipt_locates(folder, located[ISO], located[TREENAME],
                                                        located[ISOLYZER], NEW)
                if stuck == ISO:
                    self.assertEqual(result.iso_path, folder / ISO)
                    self.assertIn("remains at the canonical filename",
                                  record["operations_performed"]["iso_creation"]["publish_method"])
                    self.assertTrue(any(f"could not move {ISO} back" in w
                                        for w in record["backup_status"]["warnings"]))
                chowned = chowned_paths(tools)
                self.assertIn(str(folder / stuck), chowned)
                self.assertFalse(any(c.startswith(str(archive) + os.sep) for c in chowned), chowned)
                failed_record = one(folder, f"{NAME}_failed-*_manifest.json")
                self.assertEqual(verify.check_manifest(failed_record).status, "SKIPPED")

    def test_staged_success_log_left_at_canonical_name_is_removed(self):
        folder = self.subfolder("log")
        obj, result, tools, _ = self.publish_failing_with(folder, LOGNAME)
        self.assertFalse(result.success)
        self.assertEqual(canonical(folder), {})
        self.assert_prior_bundle_archived_whole(folder)
        for log_file in folder.glob("*.iso.log.txt"):
            self.assertNotIn("Status:           SUCCESS", log_file.read_text(), log_file.name)
        self.assertEqual(manifests_claiming_success(folder, "*_attempt-*_manifest.json"), [])
        record, _ = self.assert_receipt_locates(folder, one(folder, f"{NAME}_failed-*.iso.partial"),
                                                one(folder, f"{NAME}_failed-*_tree.txt"),
                                                one(folder, f"{NAME}_failed-*_isolyzer.xml"), NEW)
        self.assertTrue(any(w.startswith(f"removed {LOGNAME}") for w in record["backup_status"]["warnings"]),
                        record["backup_status"]["warnings"])

    def test_unremovable_staged_success_log_is_contradicted_in_the_records(self):
        folder = self.subfolder("log")
        obj, result, tools, state = self.publish_failing_with(
            folder, LOGNAME, unlink_error=PermissionError(13, "synthetic: Permission denied"))
        self.assertFalse(result.success)
        self.assertEqual((folder / LOGNAME).read_bytes(), state["stuck"], "the staged log was rewritten")
        self.assert_prior_bundle_archived_whole(folder)
        record = json.loads(one(folder, f"{NAME}_failed-*_manifest.json").read_text())
        log_text = one(folder, f"{NAME}_failed-*.iso.log.txt").read_text()
        contradictions = [w for w in record["backup_status"]["warnings"]
                          if LOGNAME in w and "does not describe a successful run" in w]
        self.assertEqual(len(contradictions), 1, record["backup_status"]["warnings"])
        self.assertIn(contradictions[0], log_text)

    def test_failed_rename_to_failed_name_records_the_file_where_it_stayed(self):
        for suffix in ("_tree.txt", "_isolyzer.xml", ".iso"):
            with self.subTest(suffix):
                folder = self.subfolder(suffix.strip("._"))
                old = canonical(folder)
                real_rename = Path.rename

                def rename(path, target, suffix=suffix):
                    p, t = Path(path), Path(target)
                    if "attempt-" in p.name and p.name.endswith(suffix) and "_failed-" in t.name:
                        raise OSError("synthetic: cannot rename to the failed name")
                    return real_rename(path, target)

                tools = FakeTools()
                with mock.patch.object(Path, "rename", rename), \
                        mock.patch.object(imaging.subprocess, "run", tools.run):
                    code = run_main(folder, NEW, mismatch=True, sudo_user="synthetic-operator")
                self.assertEqual(code, 2)
                self.assertEqual(canonical(folder), old)
                stuck = one(folder, f"{NAME}_attempt-*{suffix}")
                located = {s: stuck if s == suffix else
                           one(folder, f"{NAME}_failed-*{'.iso.mismatch' if s == '.iso' else s}")
                           for s in (".iso", "_tree.txt", "_isolyzer.xml")}
                record, _ = self.assert_receipt_locates(folder, located[".iso"], located["_tree.txt"],
                                                        located["_isolyzer.xml"], NEW)
                failed_name = (f"{NAME}_{record['output_files']['manifest_filename'].split('_')[1]}"
                               f"{'.iso.mismatch' if suffix == '.iso' else suffix}")
                notes = [w for w in record["backup_status"]["warnings"]
                         if stuck.name in w and failed_name in w]
                self.assertEqual(len(notes), 1, record["backup_status"]["warnings"])
                self.assertIn(str(stuck), chowned_paths(tools))

    def resume_hook(self):
        """The attempt ISO's failed-* rename commits, then KeyboardInterrupt
        is raised directly (not a held SIGINT): create_backup must resume"""
        real_rename = Path.rename
        state = {}

        def rename(path, target):
            p, t = Path(path), Path(target)
            outcome = real_rename(path, target)
            if not state and "attempt-" in p.name and p.name.endswith(".iso") and "_failed-" in t.name:
                state["fired"] = True
                raise KeyboardInterrupt
            return outcome

        return state, mock.patch.object(Path, "rename", rename)

    def test_interrupted_failure_marking_resumes_under_one_token(self):
        folder = self.subfolder("main")
        old = canonical(folder)
        state, patcher = self.resume_hook()
        with patcher:
            code = run_main(folder, NEW, mismatch=True)
        self.assertTrue(state, "the interrupt never fired")
        self.assertEqual(code, 2)
        self.assertEqual(canonical(folder), old)
        self.assertEqual(sorted(folder.glob("*attempt-*")), [])
        tokens = {re.search(r"_failed-(\d{8}T\d{6}-[0-9a-f]{6})", p.name).group(1)
                  for p in folder.iterdir() if "_failed-" in p.name}
        self.assertEqual(len(tokens), 1, sorted(p.name for p in folder.iterdir()))
        self.assert_receipt_locates(folder, one(folder, f"{NAME}_failed-*.iso.mismatch"),
                                    one(folder, f"{NAME}_failed-*_tree.txt"),
                                    one(folder, f"{NAME}_failed-*_isolyzer.xml"), NEW)
        folder = self.subfolder("object")
        state, patcher = self.resume_hook()
        with patcher:
            obj, result = backup(folder, NEW, mismatch=True)
        self.assertTrue(obj.interrupted)
        self.assertEqual(result.iso_path, one(folder, f"{NAME}_failed-*.iso.mismatch"))

    def test_a_check_that_fails_while_marking_is_not_taken_as_absence(self):
        folder = self.subfolder("main")
        real_lstat = os.lstat
        error = OSError(5, "synthetic: Input/output error")

        def lstat(path, *args, **kwargs):
            name = os.fsdecode(path)
            if "attempt-" in os.path.basename(name) and name.endswith("_tree.txt"):
                raise error
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(imaging.os, "lstat", lstat):
            code = run_main(folder, NEW, mismatch=True)
        self.assertEqual(code, 2)
        tree = one(folder, f"{NAME}_attempt-*_tree.txt")
        self.assertEqual(sorted(folder.glob(f"{NAME}_failed-*_tree.txt")), [])
        record = json.loads(one(folder, f"{NAME}_failed-*_manifest.json").read_text())
        self.assertEqual(record["output_files"]["tree_filename"], tree.name)
        notes = [w for w in record["backup_status"]["warnings"]
                 if "could not verify" in w and str(error) in w and tree.name in w]
        self.assertEqual(len(notes), 1, record["backup_status"]["warnings"])

    # --- controls: green on released code too ---

    def test_control_healthy_replacement_records_and_chowns_the_canonical_bundle(self):
        folder = self.subfolder("main")
        tools = FakeTools()
        with mock.patch.object(imaging.subprocess, "run", tools.run):
            obj, result = backup(folder, NEW, sudo_user="synthetic-operator")
        self.assertTrue(result.success, result.error_message)
        record = json.loads((folder / f"{NAME}_manifest.json").read_text())
        self.assertEqual(record["output_files"]["iso_filename"], ISO)
        self.assertEqual(record["output_files"]["iso_file_size_bytes"], len(NEW))
        self.assertEqual(record["technical_details"]["backup_parameters"]["final_destination"],
                         str(folder / ISO))
        self.assertEqual(record["operations_performed"]["tree_listing"]["summary"],
                         {"total_directories": 1, "total_files": 1})
        self.assertEqual(sorted(chowned_paths(tools)),
                         sorted([str(folder / n) for n in CANONICAL] + [str(archives(folder)[0])]))

    def test_control_mismatch_without_faults_records_its_failed_files(self):
        folder = self.subfolder("main")
        self.assertEqual(run_main(folder, NEW, mismatch=True), 2)
        record, _ = self.assert_receipt_locates(folder, one(folder, f"{NAME}_failed-*.iso.mismatch"),
                                                one(folder, f"{NAME}_failed-*_tree.txt"),
                                                one(folder, f"{NAME}_failed-*_isolyzer.xml"), NEW)
        self.assertFalse(any("could not rename" in w for w in record["backup_status"]["warnings"]))

    def test_control_late_publish_error_never_records_the_prior_master(self):
        folder = self.subfolder("main")
        old = canonical(folder)
        tools = FakeTools()
        with self.fault(lambda src, dst: "attempt-" in src.name and src.name.endswith("_manifest.json")), \
                mock.patch.object(imaging.subprocess, "run", tools.run):
            obj, result = backup(folder, NEW, sudo_user="synthetic-operator")
        self.assertFalse(result.success)
        self.assertEqual(canonical(folder), old)
        partial = one(folder, f"{NAME}_failed-*.iso.partial")
        record = json.loads(one(folder, f"{NAME}_failed-*_manifest.json").read_text())
        self.assertEqual(record["output_files"]["iso_filename"], partial.name)
        self.assertEqual(record["output_files"]["iso_file_size_bytes"], len(NEW))
        self.assertEqual(record["integrity_verification"]["sha256_iso_hash"], hashes(NEW)[1])
        chowned = chowned_paths(tools)
        self.assertFalse(any(str(folder / n) in chowned for n in CANONICAL), chowned)

# --- unreadable metadata (C2 follow-up) ---------------------------------------

@contextlib.contextmanager
def unreadable(armed: set):
    """os.stat, os.lstat, Path.stat and Path.lstat raise EIO for each path the
    caller puts in `armed`, from the moment it does (pathlib on 3.9/3.10 keeps
    its own reference to os.stat, so both levels are patched)"""
    def fail(path):
        raise OSError(errno.EIO, "synthetic: Input/output error", os.fsdecode(path))

    def os_level(real):
        def call(path, *args, **kwargs):
            if not isinstance(path, int) and Path(os.fsdecode(path)) in armed:
                fail(path)
            return real(path, *args, **kwargs)
        return call

    def path_level(real):
        def call(path, *args, **kwargs):
            if path in armed:
                fail(path)
            return real(path, *args, **kwargs)
        return call

    with mock.patch.object(os, "stat", os_level(os.stat)), \
            mock.patch.object(os, "lstat", os_level(os.lstat)), \
            mock.patch.object(Path, "stat", path_level(Path.stat)), \
            mock.patch.object(Path, "lstat", path_level(Path.lstat)):
        yield armed


class UnreadableMetadataTests(Base):
    """A check that fails with an error other than "no such file" (EIO here)
    means unknown, not absent: failure records stay complete and say so,
    staged success records fail closed before publication, and the ownership
    fix stays best effort"""

    subfolder = ReceiptTests.subfolder

    def arm_at_marking(self, armed, role):
        real = imaging.OpticalDiscBackup._mark_failed_attempt

        def mark(obj, result, attempt):
            armed.add(getattr(attempt, role))
            return real(obj, result, attempt)

        return mock.patch.object(imaging.OpticalDiscBackup, "_mark_failed_attempt", mark)

    def run_with(self, folder, patches, payload=NEW, **drive):
        tools, lines = FakeTools(), []
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(imaging.subprocess, "run", tools.run))
            stack.enter_context(mock.patch.object(imaging, "log", lines.append))
            code = run_main(folder, payload, **drive)
        return code, tools, [str(line) for line in lines]

    def failure_records(self, folder):
        log_text = one(folder, f"{NAME}_failed-*.iso.log.txt").read_text()
        self.assertIn("Log generated:", log_text, "the failure log is incomplete")
        record = json.loads(one(folder, f"{NAME}_failed-*_manifest.json").read_text())
        for pattern in ("*_failed-*_manifest.json", "*_attempt-*_manifest.json"):
            self.assertEqual(manifests_claiming_success(folder, pattern), [])
        return record, log_text

    def test_an_unreadable_iso_still_gets_complete_failure_records(self):
        folder = self.subfolder("iso")
        old = canonical(folder)
        armed = set()
        with unreadable(armed):
            code, tools, lines = self.run_with(folder, [self.arm_at_marking(armed, "iso")], mismatch=True)
        self.assertTrue(armed, "failure marking never started")
        self.assertEqual(code, 2, lines)
        self.assertEqual(canonical(folder), old)
        iso = one(folder, f"{NAME}_attempt-*.iso")  # never renamed: it could not be checked
        self.assertEqual(iso.read_bytes(), NEW)
        record, log_text = self.failure_records(folder)
        files, status = record["output_files"], record["backup_status"]
        self.assertEqual(status["overall_status"], "verification_failed")
        self.assertEqual(files["iso_filename"], iso.name)
        self.assertEqual((files["iso_file_size_bytes"], files["iso_file_size_formatted"],
                          status["iso_created"]), (None, None, None))
        notes = [w for w in status["warnings"] if iso.name in w and "Input/output error" in w]
        self.assertEqual(len(notes), 1, status["warnings"])
        self.assertIn(notes[0], log_text)
        self.assertRegex(log_text, rf"ISO File Size:\s+unavailable \(could not check {re.escape(iso.name)}: ")
        physical = [line for line in log_text.splitlines() if line.startswith("Physical Size")]
        self.assertEqual(len(physical), 1, log_text)
        self.assertIn("unavailable", physical[0])  # never the disc's TotalSize
        used = [line for line in log_text.splitlines() if line.startswith("Used Space")]
        self.assertFalse(any("%" in line for line in used), used)

    def test_an_unreadable_isolyzer_output_keeps_its_analysis_section(self):
        folder = self.subfolder("isolyzer")
        old = canonical(folder)
        armed = set()
        with unreadable(armed):
            code, tools, lines = self.run_with(folder, [self.arm_at_marking(armed, "isolyzer")],
                                               mismatch=True)
        self.assertEqual(code, 2, lines)
        self.assertEqual(canonical(folder), old)
        isolyzer = one(folder, f"{NAME}_attempt-*_isolyzer.xml")
        record, log_text = self.failure_records(folder)
        self.assertEqual(record["output_files"]["isolyzer_filename"], isolyzer.name)
        self.assertIn("ISO Structure Analysis", log_text)
        self.assertIn(f"  Output:  {isolyzer.name} (could not check it: ", log_text)
        self.assertIn("Valid ISO 9660:", log_text)  # the analysis itself is kept
        self.assertTrue(any(isolyzer.name in w and "Input/output error" in w
                            for w in record["backup_status"]["warnings"]))

    def test_ownership_skips_an_unreadable_file_and_hands_over_the_rest(self):
        folder = self.subfolder("sudo")
        old = canonical(folder)
        armed = set()
        with unreadable(armed):
            code, tools, lines = self.run_with(folder, [self.arm_at_marking(armed, "iso")],
                                               mismatch=True, sudo_user="synthetic-operator")
        self.assertEqual(code, 2, lines)
        self.assertEqual(canonical(folder), old)
        self.failure_records(folder)
        iso = one(folder, f"{NAME}_attempt-*.iso")
        chowned = chowned_paths(tools)
        self.assertNotIn(str(iso), chowned)
        for pattern in ("_failed-*.iso.log.txt", "_failed-*_manifest.json",
                        "_failed-*_tree.txt", "_failed-*_isolyzer.xml"):
            self.assertIn(str(one(folder, NAME + pattern)), chowned)
        self.assertTrue(any(str(iso) in line and "owner was not changed" in line for line in lines),
                        lines)

    def test_an_unreadable_archive_folder_is_only_an_ownership_warning(self):
        folder = self.subfolder("archive")
        armed = set()
        real = imaging.OpticalDiscBackup._fix_ownership

        def fix(obj, result, created_output_dir):
            if obj._archive_dir is not None:
                armed.add(Path(obj._archive_dir))
            return real(obj, result, created_output_dir)

        with unreadable(armed):
            code, tools, lines = self.run_with(
                folder, [mock.patch.object(imaging.OpticalDiscBackup, "_fix_ownership", fix)],
                sudo_user="synthetic-operator")
        self.assertTrue(armed, "no archive folder reached the ownership fix")
        archive = next(iter(armed))
        self.assertEqual(code, 0, lines)
        self.assertEqual(canonical(folder)[ISO], NEW)
        record = json.loads((folder / f"{NAME}_manifest.json").read_text())
        self.assertEqual(record["backup_status"]["overall_status"], "success")
        chowned = chowned_paths(tools)
        self.assertNotIn(str(archive), chowned)
        for name in CANONICAL:
            self.assertIn(str(folder / name), chowned)
        self.assertTrue(any(str(archive) in line and "owner was not changed" in line for line in lines),
                        lines)

    def test_an_unreadable_iso_while_staging_success_fails_closed(self):
        for mode in ("persistent", "transient"):
            with self.subTest(mode):
                folder = self.subfolder(mode)
                old = canonical(folder)
                armed, writes = set(), []
                real = imaging.OpticalDiscBackup._write_run_records

                def write(obj, result, *args, mode=mode, **kwargs):
                    first = not writes
                    writes.append(bool(result.success))
                    if first and result.success:
                        armed.add(Path(obj.config.output_path))
                    try:
                        return real(obj, result, *args, **kwargs)
                    finally:
                        if first and mode == "transient":
                            armed.clear()

                with unreadable(armed):
                    code, tools, lines = self.run_with(
                        folder, [mock.patch.object(imaging.OpticalDiscBackup, "_write_run_records", write)])
                self.assertEqual(writes[:1], [True], "the staged success records were never written")
                self.assertEqual(code, 1, lines)
                self.assertEqual(canonical(folder), old)
                self.assertEqual(archives(folder), [], "publication started")
                record, log_text = self.failure_records(folder)
                files, status = record["output_files"], record["backup_status"]
                self.assertEqual(status["overall_status"], "failed")
                self.assertIn("Input/output error", status["errors"])
                if mode == "persistent":
                    iso = one(folder, f"{NAME}_attempt-*.iso")
                    expected = (iso.name, None, None)
                else:
                    iso = one(folder, f"{NAME}_failed-*.iso.partial")
                    expected = (iso.name, True, len(NEW))
                self.assertEqual((files["iso_filename"], status["iso_created"],
                                  files["iso_file_size_bytes"]), expected)
                self.assertEqual(iso.read_bytes(), NEW)


# --- native launch errors (T-133) ---------------------------------------------

class NativeLaunchTests(Base):
    """diskutil and chown that cannot even be started (OSError at launch) go
    through the established outcome policies: makeiso's own unmount, eject
    and ownership fix run, with only subprocess.run faked"""

    def native_main(self, folder, payload=NEW, tools=None, **drive):
        tools = tools or FakeTools(disk=True)
        lines = []
        with mock.patch.object(imaging.subprocess, "run", tools.run), \
                mock.patch.object(imaging, "log", lines.append):
            code = run_main(folder, payload, native_disk=True, **drive)
        return code, tools, lines

    def subfolder(self, label: str) -> Path:
        folder = self.dir / label
        folder.mkdir()
        obj, result = backup(folder, OLD)
        self.assertTrue(result.success)
        return folder

    def published(self, folder):
        record = json.loads((folder / f"{NAME}_manifest.json").read_text())
        self.assertEqual(canonical(folder)[ISO], NEW)
        self.assertEqual(sorted(canonical(folder)), sorted(CANONICAL))
        self.assertEqual(len(archives(folder)), 1)
        self.assertEqual((archives(folder)[0] / ISO).read_bytes(), OLD)
        self.assertEqual(verify_cli(folder)[0], 0)
        self.assertEqual(sorted(folder.glob("*attempt-*")) + sorted(folder.glob("*_failed-*")), [])
        return record

    def failed_record(self, folder):
        return json.loads(one(folder, f"{NAME}_failed-*_manifest.json").read_text())

    def test_native_unmount_and_eject_control(self):
        folder = self.subfolder("main")
        code, tools, lines = self.native_main(folder)
        self.assertEqual(code, 0, lines)
        self.assertEqual(tools.disk, ["unmountDisk", "eject"])
        record = self.published(folder)
        self.assertEqual(record["backup_status"]["overall_status"], "success")
        ops = record["operations_performed"]
        self.assertEqual((ops["disk_unmount"]["status"], ops["finalization"]["status"]),
                         ("success", "completed"))

    def test_nonzero_unmount_keeps_its_policy(self):
        folder = self.subfolder("main")
        old = canonical(folder)
        code, tools, lines = self.native_main(folder, tools=FakeTools(disk=True,
                                                                      exit_status={"unmountDisk": 1}))
        self.assertEqual(code, 1)
        self.assertEqual(tools.disk, ["unmountDisk"])
        self.assertEqual(canonical(folder), old)
        record = self.failed_record(folder)
        self.assertIn(f"Failed to unmount /dev/disk9", record["backup_status"]["errors"])
        ops = record["operations_performed"]
        self.assertEqual((ops["disk_unmount"]["status"], ops["finalization"]["status"]),
                         ("failed", "skipped"))

    def test_nonzero_eject_keeps_its_policy(self):
        folder = self.subfolder("main")
        code, tools, lines = self.native_main(folder, tools=FakeTools(disk=True, exit_status={"eject": 1}))
        self.assertEqual(code, 0, lines)
        record = self.published(folder)
        self.assertEqual(record["backup_status"]["overall_status"], "success_with_warnings")
        self.assertIn("disc finalization (eject) failed", record["backup_status"]["warnings"])
        self.assertEqual(record["operations_performed"]["finalization"]["status"], "failed")
        self.assertIn("FAILED (eject error — see console output)", (folder / LOGNAME).read_text())

    def test_unmount_launch_error_is_a_recorded_failure(self):
        folder = self.subfolder("main")
        old = canonical(folder)
        code, tools, lines = self.native_main(folder, tools=FakeTools(disk=True, launch_error="unmountDisk"),
                                              sudo_user="synthetic-operator")
        self.assertEqual(code, 1, lines)
        self.assertEqual(tools.disk, ["unmountDisk"])
        self.assertEqual(canonical(folder), old)
        self.assertEqual(archives(folder), [])
        self.assertEqual(sorted(folder.glob("*attempt-*")), [])
        record = self.failed_record(folder)
        self.assertEqual(record["backup_status"]["overall_status"], "failed")
        self.assertIn("Failed to unmount /dev/disk9", record["backup_status"]["errors"])
        self.assertIs(record["backup_status"]["iso_created"], False)
        ops = record["operations_performed"]
        self.assertEqual((ops["disk_unmount"]["status"], ops["iso_creation"]["performed"],
                          ops["finalization"]["status"]), ("failed", False, "skipped"))
        tree = folder / record["output_files"]["tree_filename"]
        self.assertTrue(tree.is_file() and "_failed-" in tree.name, tree.name)
        log_text = (folder / record["output_files"]["log_filename"]).read_text()
        self.assertIn("Status:  FAILED", log_text)
        self.assertIn("Skipped (disc was never unmounted)", log_text)
        self.assertIn(str(one(folder, f"{NAME}_failed-*_manifest.json")), chowned_paths(tools))
        self.assertTrue(any("could not run diskutil" in str(l) for l in lines), lines)

    def test_eject_launch_error_after_a_verified_copy_warns_and_publishes(self):
        folder = self.subfolder("main")
        code, tools, lines = self.native_main(folder, tools=FakeTools(disk=True, launch_error="eject"),
                                              sudo_user="synthetic-operator")
        self.assertEqual(code, 0, lines)
        self.assertEqual(tools.disk, ["unmountDisk", "eject"])
        record = self.published(folder)
        self.assertEqual(record["backup_status"]["overall_status"], "success_with_warnings")
        self.assertIn("disc finalization (eject) failed", record["backup_status"]["warnings"])
        self.assertEqual(record["operations_performed"]["finalization"]["status"], "failed")
        self.assertIn("FAILED (eject error — see console output)", (folder / LOGNAME).read_text())
        self.assertTrue(tools.chowned, "the ownership fix did not run")

    def test_eject_launch_error_after_a_failed_copy_keeps_the_failure(self):
        for label, drive, exit_code in (("mismatch", {"mismatch": True}, 2),
                                        ("copy failed", {"create_fails": True}, 1)):
            with self.subTest(label):
                folder = self.subfolder(label.replace(" ", "-"))
                old = canonical(folder)
                code, tools, lines = self.native_main(
                    folder, tools=FakeTools(disk=True, launch_error="eject"), **drive)
                self.assertEqual(code, exit_code, lines)
                self.assertEqual(tools.disk, ["unmountDisk", "eject"])
                self.assertEqual(canonical(folder), old)
                self.assertEqual(archives(folder), [])
                self.assertEqual(sorted(folder.glob("*attempt-*")), [])
                record = self.failed_record(folder)
                self.assertEqual(record["operations_performed"]["finalization"]["status"], "failed")
                if label == "mismatch":
                    self.assertEqual(record["backup_status"]["overall_status"], "verification_failed")
                    self.assertTrue((folder / record["output_files"]["iso_filename"]).is_file())
                    self.assertTrue(record["output_files"]["iso_filename"].endswith(".iso.mismatch"))
                else:
                    self.assertEqual(record["backup_status"]["errors"],
                                     "synthetic: the attempt ISO could not be opened")

    def test_chown_launch_error_is_only_a_warning(self):
        cases = (("success, chown cannot start", {"launch_error": "chown"}, {}, 0),
                 ("parity: chown fails", {"exit_status": {"chown": 1}}, {}, 0),
                 ("mismatch, chown cannot start", {"launch_error": "chown"}, {"mismatch": True}, 2))
        for label, tool_options, drive, exit_code in cases:
            with self.subTest(label):
                folder = self.subfolder(label.split(",")[0].split(":")[0])
                tools = FakeTools(disk=True, **tool_options)
                at_chown = {}
                real_run = tools.run

                def run(cmd, *args, **kwargs):
                    if str(cmd[0]) == "chown":
                        at_chown["files"] = snapshot(folder)
                    return real_run(cmd, *args, **kwargs)

                tools.run = run
                code, _, lines = self.native_main(folder, tools=tools, sudo_user="synthetic-operator", **drive)
                self.assertEqual(code, exit_code, lines)
                self.assertIn("files", at_chown, "chown was never attempted")
                self.assertEqual(snapshot(folder), at_chown["files"], "records changed after the chown")
                self.assertTrue(any("Could not set ownership of output files" in str(l) for l in lines))
                if exit_code == 0:
                    record = json.loads((folder / f"{NAME}_manifest.json").read_text())
                    self.assertEqual(record["backup_status"]["overall_status"], "success")
                else:
                    self.assertEqual(self.failed_record(folder)["backup_status"]["overall_status"],
                                     "verification_failed")

    def test_dry_run_still_never_unmounts_or_ejects(self):
        folder = self.dir / "dry"
        folder.mkdir()
        tools = FakeTools(disk=True)
        with mock.patch.object(imaging.subprocess, "run", tools.run), \
                mock.patch.object(imaging, "log", lambda message: None):
            code = run_main(folder, b"", dry_run=True, real_tree=True, native_disk=True)
        self.assertEqual(code, 0)
        self.assertEqual((tools.disk, tools.chowned), (["info"], []))
        record = json.loads((folder / f"{NAME}_manifest.json").read_text())
        self.assertEqual(record["backup_status"]["overall_status"], "dry_run")

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
    if scenario not in ("tree", "unmount", "eject_verified", "eject_mismatch",
                        "dry_tree", "dry_records"):
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
        if scenario == "failure_marker" and "attempt-" in p.name and p.name.endswith(".iso") \
                and "_failed-" in Path(target).name:
            outcome = real_rename(path, target)
            _send_sigint_once(state)
            return outcome
        return real_rename(path, target)

    real_log = imaging.create_formatted_log

    def formatted_log(*args, **kwargs):
        if scenario in ("records", "dry_records"):
            _send_sigint_once(state)
        return real_log(*args, **kwargs)

    def tree_command(cmd, *args, **kwargs):
        # A dry run's real tree-listing method runs; only the tree command is fake
        if cmd[0] != "tree":
            raise AssertionError(f"unexpected command {cmd!r}")
        kwargs["stdout"].write("Synthetic tree: partial\n")
        if scenario == "dry_tree":
            kwargs["stdout"].flush()
            _send_sigint_once(state)
        return subprocess.CompletedProcess(cmd, 0)

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
    if scenario == "tree":
        drive["tree"] = tree
    if scenario == "failure_marker":
        drive["mismatch"] = True
    disk_tools = None
    if scenario == "copy_sigint_eject_launch":
        drive.update(native_disk=True, copy=lambda self: _send_sigint_once(state))
        disk_tools = FakeTools(disk=True, launch_error="eject")
    dry_run = scenario.startswith("dry_")
    if dry_run:
        drive["real_tree"] = True
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "replace", replace))
        stack.enter_context(mock.patch.object(Path, "rename", rename))
        stack.enter_context(mock.patch.object(imaging, "create_formatted_log", formatted_log))
        if dry_run:
            stack.enter_context(mock.patch.object(imaging.subprocess, "run", tree_command))
        if restore_exit is not None:
            stack.enter_context(restore_exit)
        if disk_tools is not None:
            stack.enter_context(mock.patch.object(imaging.subprocess, "run", disk_tools.run))
        try:
            code = run_main(folder, new, dry_run=dry_run, **drive)
        finally:
            print("SIGINT_SENT:", bool(state.get("sent")))
            if disk_tools is not None:
                print("DISK_CALLS:", disk_tools.disk)
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
        # In a fresh location (a dry run over an existing bundle is refused
        # before its tree listing: DryRunTests). Only its own partial listing stays.
        code, output = self.child("dry_tree")
        self.assertEqual(code, -signal.SIGINT, output)
        self.assertEqual(sorted(os.listdir(self.dir)), [f"{NAME}_tree.txt"])
        self.assertEqual((self.dir / f"{NAME}_tree.txt").read_text(), "Synthetic tree: partial\n")

    def test_sigint_while_a_dry_run_writes_its_records_stops_it(self):
        code, output = self.child("dry_records")
        self.assertEqual(code, -signal.SIGINT, output)
        self.assertEqual(sorted(os.listdir(self.dir)), [f"{NAME}_tree.txt"])

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

    def test_sigint_while_marking_a_failed_attempt_is_held_until_marking_finishes(self):
        code, output = self.child("failure_marker")
        self.assertEqual(code, 2, output)
        self.assertIn("HANDLER_RESTORED: True", output)
        self.assertEqual(canonical(self.dir)[f"{NAME}.iso"], b"old verified synthetic master")
        self.assertEqual(sorted(canonical(self.dir)), sorted(CANONICAL))
        self.assertEqual(verify_cli(self.dir)[0], 0)
        self.assertEqual(sorted(self.dir.glob("*attempt-*")), [])
        record = json.loads(one(self.dir, f"{NAME}_failed-*_manifest.json").read_text())
        files = record["output_files"]
        self.assertTrue(files["iso_filename"].endswith(".iso.mismatch"), files["iso_filename"])
        self.assertTrue((self.dir / files["iso_filename"]).is_file())
        self.assertIs(record["backup_status"]["iso_created"], True)
        for key in ("tree_filename", "isolyzer_filename"):
            self.assertTrue((self.dir / files[key]).is_file(), files[key])
        self.assertEqual(record["operations_performed"]["tree_listing"]["summary"],
                         {"total_directories": 1, "total_files": 1})

    def test_sigint_during_copy_then_an_eject_launch_error_keeps_the_interrupt(self):
        code, output = self.child("copy_sigint_eject_launch")
        self.assertEqual(code, 1, output)
        self.assertNotIn("Traceback", output)
        self.assertIn("DISK_CALLS:", output)
        self.assertIn("'eject'", output.split("DISK_CALLS:")[1].splitlines()[0])
        self.assertEqual(canonical(self.dir)[f"{NAME}.iso"], b"old verified synthetic master")
        self.assertEqual(verify.check_manifest(self.dir / f"{NAME}_manifest.json").status, "PASS")
        self.assertEqual(archives(self.dir), [])
        self.assertEqual(sorted(self.dir.glob("*attempt-*")), [])
        record = json.loads(one(self.dir, f"{NAME}_failed-*_manifest.json").read_text())
        self.assertIn("Interrupted by operator (Ctrl-C)", record["backup_status"]["errors"])
        self.assertEqual(record["operations_performed"]["finalization"]["status"], "failed")

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
