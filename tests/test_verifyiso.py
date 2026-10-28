#!/usr/bin/env python3
"""Synthetic tests for verifyiso.py: CSV report safety, manifest families and
statuses, malformed items, and the eight fixture scenarios (T-122, T-125,
T-126); discovery that reports every part of the search it could not
complete (T-134); masters and manifests classified before they are opened,
one inspection table on every Python version, spellings that never hide a
different bundle and symlink chains told apart from loops (T-147, T-148);
and report cleanup that is reported when it fails, Ctrl-C while the report
is reserved, and escaped console text (T-149).

Standard library only. Every file is synthetic and private (TMPDIR); the
guards in synthetic_support.py block child processes, sockets, devices and
writes outside the private directory; the only child is this interpreter
re-running this file with --sigint-child, so that a REAL SIGINT can be
delivered mid-run without disturbing the test runner. These tests check
verifyiso's decisions, files and exit codes - not real collection media.
Discovery faults are injected through os.scandir and os.stat fakes that
delegate everything else, with DirEntry stand-ins; symlinks, symlink chains
and FIFOs are real files in the private directory, and a tripwire (installed
before the product is loaded) stops any open of a FIFO or device before it
can block.

Run:  python3 tests/test_verifyiso.py
OPTICAL_UNDER_TEST=/dir runs the same tests against another copy.
"""

import contextlib
import copy
import csv
import errno
import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
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
# Every DiscoveryTests case asserts that no such open was attempted. It uses
# the os.stat saved here, never a test's patched one, so a fault injected
# into os.stat cannot hide a FIFO from it.
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


verify = support.load("verifyiso_under_test", "verifyiso.py")
fixtures = support.load("verify_fixtures_under_test", "tests/make_verify_fixtures.py")


def hashes(data: bytes):
    return hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()


def record(iso_name="disc.iso", data=b"healthy synthetic master", **changes):
    """A makeiso.py-shaped success record for `data` (fixture builder shape)"""
    md5, sha = hashes(data)
    m = fixtures._manifest(iso_name, md5=md5, sha256=sha)
    m.update(changes)
    return m


def status_of(manifest_path: Path) -> str:
    """check_manifest's status, or what it raised (a defect, not a status)"""
    try:
        return verify.check_manifest(manifest_path).status
    except Exception as e:
        return f"raised {type(e).__name__}"


@contextlib.contextmanager
def unlink_denied(path: Path):
    """os.unlink refuses path (EACCES), delegating everything else; yields
    the attempts it refused"""
    real, refused = os.unlink, []

    def unlink(name, *args, **kwargs):
        if not isinstance(name, int) and Path(os.fsdecode(name)) == path:
            refused.append(path)
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(name, *args, **kwargs)

    with mock.patch.object(os, "unlink", unlink):
        yield refused


@contextlib.contextmanager
def lstat_fails(path: Path, error: OSError):
    """os.lstat raises error for path, delegating everything else; yields the
    calls it failed"""
    real, fired = os.lstat, []

    def lstat(name, *args, **kwargs):
        if not isinstance(name, int) and Path(os.fsdecode(name)) == path:
            fired.append(path)
            raise error
        return real(name, *args, **kwargs)

    with mock.patch.object(os, "lstat", lstat):
        yield fired


@contextlib.contextmanager
def resolution_fails(path: Path, error: OSError, under=None):
    """os.lstat raises error for path (as spelled, or under its folder's real
    path), but only when verifyiso resolves a path's identity - its own
    _realpath, or os.path.realpath called by a verifyiso function (the
    released form) - and, if under is given, only beneath that verifyiso
    function; everything else (the harness included) is delegated. Yields
    the verifyiso function each fault fired for, so a test shows the fault
    reached the product's resolution logic"""
    real, fired = os.lstat, []
    spellings = {str(path), os.path.join(os.path.realpath(path.parent), path.name)}
    resolvers = ("_realpath", "resolve")

    def lstat(name, *args, **kwargs):
        if not isinstance(name, int) and os.fsdecode(name) in spellings:
            frame, between = sys._getframe(1), []
            while frame is not None and frame.f_globals is not vars(verify):
                between.append((frame.f_globals.get("__name__"), frame.f_code.co_name))
                frame = frame.f_back
            product = []
            while frame is not None:
                if frame.f_globals is vars(verify):
                    product.append(frame.f_code.co_name)
                frame = frame.f_back
            via_realpath = (bool(between) and ("posixpath", "realpath") in between
                            and all(m in ("posixpath", "genericpath") for m, _ in between))
            resolving = bool(product) and (product[0] in resolvers or via_realpath)
            if resolving and (under is None or under in product):
                fired.append(next(n for n in product if n not in resolvers))
                raise error
        return real(name, *args, **kwargs)

    with mock.patch.object(os, "lstat", lstat):
        yield fired


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="verify-case-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.hashed = []
        real_hash = verify.hash_file

        def spy(path, label=""):
            self.hashed.append(Path(path))
            return real_hash(path, label)

        patcher = mock.patch.object(verify, "hash_file", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._violations_before = len(support.VIOLATIONS)
        self.addCleanup(self._no_escapes)

    def _no_escapes(self):
        self.assertEqual(support.VIOLATIONS[self._violations_before:], [],
                         "a guarded operation was attempted")
        self.assertEqual(support.tripwire_calls(), "", "a native tool was executed")

    def master(self, name="disc", data=b"healthy synthetic master", folder=None, **changes):
        folder = folder or self.dir
        folder.mkdir(parents=True, exist_ok=True)
        iso = folder / f"{name}.iso"
        iso.write_bytes(data)
        manifest = folder / f"{name}_manifest.json"
        manifest.write_text(json.dumps(record(iso.name, data, **changes)))
        return iso, manifest

    def write_manifest(self, name, content, folder=None):
        path = (folder or self.dir) / name
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
        return path

    def cli(self, *paths, report=None):
        """Run verifyiso's main(); returns (exit code or 'interrupted', stdout)"""
        argv = ["verifyiso.py"] + [str(p) for p in paths]
        if report is not None:
            argv += ["--csv", str(report)]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            try:
                verify.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
            except KeyboardInterrupt:
                code = "interrupted"
            except Exception as e:
                code = f"raised {type(e).__name__}"
            else:
                code = 0
        return code, out.getvalue()


class CsvReportTests(Base):
    """T-122: the report is reserved before hashing and never replaces a file"""

    def assert_refused(self, report):
        self.master()
        code, output = self.cli(self.dir, report=report)
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [], "an ISO was hashed before the report was refused")
        return output

    def test_refuses_the_verified_iso_itself(self):
        iso, _ = self.master()
        original = iso.read_bytes()
        self.assert_refused(iso)
        self.assertEqual(iso.read_bytes(), original)

    def test_refuses_its_manifest(self):
        _, manifest = self.master()
        original = manifest.read_bytes()
        self.assert_refused(manifest)
        self.assertEqual(manifest.read_bytes(), original)

    def test_refuses_an_earlier_report(self):
        earlier = self.dir / "report.csv"
        earlier.write_text("status,iso\nPASS,old\n")
        self.assert_refused(earlier)
        self.assertEqual(earlier.read_text(), "status,iso\nPASS,old\n")

    def test_refuses_symlinks_dangling_or_not(self):
        iso, _ = self.master()
        original = iso.read_bytes()
        to_iso = self.dir / "to_iso.csv"
        os.symlink(iso, to_iso)
        dangling = self.dir / "latest.csv"
        os.symlink(self.dir / "rotated" / "missing.csv", dangling)
        for link in (to_iso, dangling):
            with self.subTest(link=link.name):
                self.hashed.clear()
                self.assert_refused(link)
                self.assertTrue(link.is_symlink())
        self.assertEqual(iso.read_bytes(), original)
        self.assertFalse((self.dir / "rotated").exists())

    def test_refuses_a_directory_and_dev_stdout(self):
        folder = self.dir / "reports"
        folder.mkdir()
        for target in (folder, Path("/dev/stdout")):
            with self.subTest(target=str(target)):
                self.hashed.clear()
                self.assert_refused(target)

    def test_refuses_iso_and_manifest_shaped_names(self):
        for name in ("REPORT.ISO", "audit.iso", "X_MANIFEST.JSON", "fixity_manifest.json"):
            with self.subTest(name=name):
                self.hashed.clear()
                self.assert_refused(self.dir / name)
                self.assertFalse((self.dir / name).exists())

    def test_refuses_a_missing_or_unwritable_folder_before_hashing(self):
        self.assert_refused(self.dir / "no-such-folder" / "report.csv")
        locked = self.dir / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        self.hashed.clear()
        self.assert_refused(locked / "report.csv")

    def test_healthy_report_is_written_and_exit_follows_verification(self):
        self.master("good")
        report = self.dir / "fixity_20261024.csv"
        code, output = self.cli(self.dir, report=report)
        self.assertEqual(code, 0, output)
        rows = report.read_text(encoding="utf-8").splitlines()
        self.assertEqual(rows[0].split(",")[:3], ["status", "iso", "manifest"])
        self.assertEqual([r.split(",")[0] for r in rows[1:]], ["PASS"])
        # A FAIL still gets its report, and still exits 1
        bad_iso, _ = self.master("bad")
        bad_iso.write_bytes(b"tampered")
        report2 = self.dir / "fixity_20261024_2.csv"
        code, output = self.cli(self.dir, report=report2)
        self.assertEqual(code, 1, output)
        self.assertEqual(sorted(r.split(",")[0] for r in report2.read_text().splitlines()[1:]),
                         ["FAIL", "PASS"])

    def test_write_failure_removes_only_this_runs_report(self):
        iso, manifest = self.master()
        before = (iso.read_bytes(), manifest.read_bytes())
        report = self.dir / "report.csv"

        class FailingWriter:
            """csv.writer stand-in: the header lands, then the disk is full"""
            def __init__(self, f):
                self.f, self.rows = f, 0

            def writerow(self, row):
                self.rows += 1
                if self.rows > 1:
                    raise OSError(28, "No space left on device")
                self.f.write(",".join(str(x) for x in row) + "\n")

        with mock.patch.object(verify.csv, "writer", FailingWriter):
            code, output = self.cli(self.dir, report=report)
        self.assertEqual(code, 1, output)
        self.assertFalse(os.path.lexists(report), "a partial report was left behind")
        self.assertEqual((iso.read_bytes(), manifest.read_bytes()), before)

    def test_ctrl_c_stops_the_run_and_leaves_no_report(self):
        self.master("a")
        self.master("b")
        report = self.dir / "report.csv"

        def interrupt(path, label=""):
            self.hashed.append(Path(path))
            raise KeyboardInterrupt

        with mock.patch.object(verify, "hash_file", interrupt):
            code, output = self.cli(self.dir, report=report)
        self.assertEqual(code, 130, output)
        self.assertEqual(len(self.hashed), 1, "the run went on after Ctrl-C")
        self.assertFalse(os.path.lexists(report))

    def interrupted_run(self, report, unlink_fails):
        """Two bundles, Ctrl-C while the first is hashed; with unlink_fails,
        removing the unfinished report is refused (EACCES)"""
        self.master("a")
        self.master("b")

        def interrupt(path, label=""):
            self.hashed.append(Path(path))
            raise KeyboardInterrupt

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(verify, "hash_file", interrupt))
            refused = stack.enter_context(unlink_denied(report)) if unlink_fails else None
            code, output = self.cli(self.dir, report=report)
        self.assertEqual(code, 130, output)
        self.assertEqual(len(self.hashed), 1, "the run went on after Ctrl-C")
        self.assertNotIn("no report written", output)  # a removal that can fail is never promised
        self.assertIn("Interrupted by operator (Ctrl-C) — verification incomplete; the --csv report "
                      "was not completed", output)
        return output, refused

    def test_ctrl_c_with_a_failed_removal_names_what_was_left(self):
        # T-149 (F5b): the unfinished report is this run's own file, but it
        # could not be removed; the run says so instead of staying silent
        report = self.dir / "report.csv"
        output, refused = self.interrupted_run(report, unlink_fails=True)
        self.assertEqual(refused, [report], "the removal was never attempted")
        self.assertIn(f"Could not remove the unfinished --csv report {report} ([Errno 13] Permission "
                      f"denied); it is the file this run created: remove it yourself", output)
        self.assertEqual(report.read_bytes(), b"")

    def test_control_ctrl_c_with_a_working_removal_warns_of_nothing(self):
        report = self.dir / "report.csv"
        output, _ = self.interrupted_run(report, unlink_fails=False)
        self.assertNotIn("Could not remove", output)
        self.assertNotIn("left in place", output)
        self.assertFalse(os.path.lexists(report))

    def test_a_file_that_replaced_the_reservation_is_left_alone(self):
        self.master()
        report = self.dir / "report.csv"
        real_hash = verify.hash_file

        def replace_report(path, label=""):
            # Someone else's file takes the report path mid-run
            if os.path.lexists(report):
                os.unlink(report)
            report.write_text("SOMEONE ELSE'S FILE")
            return real_hash(path, label)

        with mock.patch.object(verify, "hash_file", replace_report):
            code, output = self.cli(self.dir, report=report)
        self.assertEqual(code, 1, output)
        self.assertEqual(report.read_text(), "SOMEONE ELSE'S FILE")


class ReportAliasTests(Base):
    """T-137: the --csv reservation is never hashed as a master or read as a
    manifest, through the same path or another spelling of it (a symlink on
    either side); such a manifest is an ERROR and the report is not kept,
    so it cannot be left where a manifest expects its master. Controls: the
    same records with the report elsewhere"""

    def empty_master_record(self, folder, iso_name="master.payload"):
        """A hand-made record for a missing master of zero bytes: the empty
        reservation would match its hashes"""
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / "disc_manifest.json"
        manifest.write_text(json.dumps(record(iso_name, b"")))
        return manifest

    def assert_alias_refused(self, folder, report, manifest, output, code, expect):
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [], "the reservation was hashed")
        self.assertNotIn("PASS", output)
        self.assertIn(expect, output)
        self.assertIn("Not keeping CSV report", output)
        self.assertNotIn("CSV report written to", output)
        self.assertFalse(os.path.lexists(report), "the report was kept")

    def test_a_report_named_as_a_missing_master_is_never_passed(self):
        folder = self.dir / "alias"
        manifest = self.empty_master_record(folder)
        before = manifest.read_bytes()
        report = folder / "master.payload"
        code, output = self.cli(folder, report=report)
        self.assert_alias_refused(folder, report, manifest, output, code,
                                  "ERROR: the ISO name master.payload is the --csv report")
        self.assertEqual(manifest.read_bytes(), before)

    def test_a_report_reached_through_a_symlinked_folder_is_never_passed(self):
        folder = self.dir / "alias"
        self.empty_master_record(folder)
        linked = self.dir / "linked"
        os.symlink(folder, linked)
        report = linked / "master.payload"
        code, output = self.cli(folder, report=report)
        self.assert_alias_refused(folder, report, None, output, code,
                                  "ERROR: the ISO name master.payload is the --csv report")
        self.assertTrue(linked.is_symlink())

    def test_a_master_symlink_to_the_report_is_never_passed(self):
        folder = self.dir / "alias"
        self.empty_master_record(folder, "disc.iso")
        report = self.dir / "report.csv"
        os.symlink(report, folder / "disc.iso")  # dangling until the report is reserved
        code, output = self.cli(folder, report=report)
        self.assert_alias_refused(folder, report, None, output, code,
                                  "ERROR: the ISO name disc.iso is the --csv report")
        self.assertEqual(os.readlink(folder / "disc.iso"), str(report))

    def test_a_manifest_symlink_to_the_report_is_never_read(self):
        folder = self.dir / "alias"
        folder.mkdir()
        report = self.dir / "report.csv"
        os.symlink(report, folder / "disc_manifest.json")
        reads = []
        real_read = Path.read_bytes

        def read_bytes(path):
            reads.append(Path(path))
            return real_read(path)

        with mock.patch.object(Path, "read_bytes", read_bytes):
            code, output = self.cli(folder, report=report)
        self.assert_alias_refused(folder, report, None, output, code,
                                  "ERROR: this is the --csv report this run is writing")
        self.assertNotIn(folder / "disc_manifest.json", reads)

    def test_control_the_same_records_with_the_report_elsewhere(self):
        folder = self.dir / "alias"
        self.empty_master_record(folder)
        report = self.dir / "elsewhere.csv"
        code, output = self.cli(folder, report=report)
        self.assertEqual(code, 1, output)
        self.assertIn("MISSING_ISO", output)
        self.assertIn("CSV report written to", output)
        rows = list(csv.reader(report.open(encoding="utf-8")))
        self.assertEqual([r[0] for r in rows[1:]], ["MISSING_ISO"])
        # and a healthy master next to a report in its own folder still passes
        self.master(folder=self.dir / "healthy")
        report2 = self.dir / "healthy" / "fixity.csv"
        code, output = self.cli(self.dir / "healthy", report=report2)
        self.assertEqual(code, 0, output)
        self.assertEqual([r[0] for r in list(csv.reader(report2.open(encoding="utf-8")))[1:]], ["PASS"])

    def test_a_failed_removal_after_an_alias_refusal_names_the_leftover(self):
        # T-149 (F5a): the refused report sits at the name a manifest expects
        # for its master; if it cannot be removed, the run says where and why
        folder = self.dir / "alias"
        manifest = self.empty_master_record(folder)
        report = folder / "master.payload"
        with unlink_denied(report) as refused:
            code, output = self.cli(folder, report=report)
        self.assertEqual(refused, [report], "the removal was never attempted")
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [], "the reservation was hashed")
        self.assertIn("ERROR: the ISO name master.payload is the --csv report", output)
        self.assertIn("Not keeping CSV report", output)
        self.assertIn(f"Could not remove the unfinished --csv report {report} ([Errno 13] Permission "
                      f"denied); it is the file this run created: remove it yourself; {manifest} names "
                      f"that path as its ISO", output)
        self.assertEqual(report.read_bytes(), b"")  # left, and reported

    def test_control_an_alias_refusal_with_a_working_removal_warns_of_nothing(self):
        folder = self.dir / "alias"
        manifest = self.empty_master_record(folder)
        report = folder / "master.payload"
        code, output = self.cli(folder, report=report)
        self.assert_alias_refused(folder, report, manifest, output, code,
                                  "ERROR: the ISO name master.payload is the --csv report")
        self.assertNotIn("Could not remove", output)
        self.assertNotIn("left in place", output)

    def test_a_report_spelled_through_a_symlink_and_dotdot_is_not_taken_for_another_file(self):
        # T-148 (N9): coll/link/../report.csv is other/report.csv to the
        # system, so a manifest in coll naming report.csv names another
        # (missing) file, not the report
        coll, other = self.dir / "coll", self.dir / "other"
        (other / "deep").mkdir(parents=True)
        coll.mkdir()
        (coll / "disc_manifest.json").write_text(json.dumps(record("report.csv")))
        os.symlink(other / "deep", coll / "link")
        report = coll / "link" / ".." / "report.csv"
        code, output = self.cli(coll, other / "deep", report=report)
        self.assertEqual(code, 1, output)
        self.assertIn("MISSING_ISO: ISO not found next to manifest: report.csv", output)
        self.assertNotIn("is the --csv report", output)
        self.assertNotIn("Not keeping CSV report", output)
        self.assertIn(f"CSV report written to: {report}", output)
        self.assertFalse(os.path.lexists(coll / "report.csv"))
        rows = list(csv.reader(io.StringIO((other / "report.csv").read_text(encoding="utf-8"))))
        self.assertEqual([r[0] for r in rows[1:]], ["MISSING_ISO"])


class ReportAliasInspectionTests(Base):
    """T-137 repair: when it cannot be inspected whether a manifest, or the
    ISO it names, is the --csv reservation (an error other than "no such
    file"), that input is an ERROR naming the path and the error, it is not
    read or hashed, healthy siblings are still checked, and the report is not
    kept. Each fault fires once, armed when check_manifest starts on that
    manifest, or (T-148 repair) only while the alias check resolves a path,
    so discovery is untouched. Controls: absence that is
    established keeps its outcome and the report; no --csv, no change"""

    @contextlib.contextmanager
    def inspection_fails(self, manifest, target, error):
        """os.stat (following links) raises error once for target, from the
        moment check_manifest starts on manifest; yields the paths it fired for"""
        real_stat, real_check = os.stat, verify.check_manifest
        armed, fired = [], []

        def stat(path, *args, **kwargs):
            if armed and not fired and not isinstance(path, int) \
                    and Path(os.fsdecode(path)) == target and kwargs.get("follow_symlinks", True):
                fired.append(Path(os.fsdecode(path)))
                raise error
            return real_stat(path, *args, **kwargs)

        def check(path, *args, **kwargs):
            if Path(path) == manifest:
                armed.append(True)
            return real_check(path, *args, **kwargs)

        with mock.patch.object(os, "stat", stat), mock.patch.object(verify, "check_manifest", check):
            yield fired

    def alias_layout(self, role, label):
        """A folder whose manifest (role "manifest") or ISO name (role
        "master", with a record of an empty file) is a symlink to the report
        path, plus a healthy sibling bundle. Returns (root, manifest, link, report)"""
        root = self.dir / label
        folder = root / "alias"
        folder.mkdir(parents=True)
        report = root / "report.csv"
        manifest = folder / "disc_manifest.json"
        if role == "master":
            manifest.write_text(json.dumps(record("disc.iso", b"")))
            link = folder / "disc.iso"
        else:
            link = manifest
        os.symlink(report, link)
        self.master(folder=root / "sibling")
        return root, manifest, link, report

    def run_alias(self, role, error, label):
        root, manifest, link, report = self.alias_layout(role, label)
        reads = []
        real_read = Path.read_bytes

        def read_bytes(path):
            reads.append(Path(path))
            return real_read(path)

        with self.inspection_fails(manifest, link, error) as fired, \
                mock.patch.object(Path, "read_bytes", read_bytes):
            code, output = self.cli(root, report=report)
        self.assertEqual(fired, [link], "the fault never fired")
        return root, manifest, link, report, code, output, reads

    def assert_uncertain(self, root, link, report, code, output, errno_text):
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [root / "sibling" / "disc.iso"],
                         "only the healthy sibling may be hashed")
        self.assertEqual(output.count("PASS: verified"), 1, output)  # the sibling
        self.assertIn(errno_text, output)
        self.assertIn("for 1 manifest(s) it could not be checked whether they or their ISO "
                      "are the report", output)
        self.assertNotIn("name it as their ISO or as themselves", output)  # not a proven alias
        self.assertNotIn("CSV report written to", output)
        self.assertFalse(os.path.lexists(report), "a report that may be an input was kept")
        self.assertTrue(link.is_symlink())

    def test_a_master_link_to_the_report_whose_inspection_fails_is_never_hashed(self):
        root, manifest, link, report, code, output, _ = self.run_alias("master", io_error(), "eio")
        self.assert_uncertain(root, link, report, code, output, "[Errno 5] Input/output error")
        self.assertIn(f"{link}: ERROR — could not check whether the ISO name disc.iso is the "
                      f"--csv report this run is writing ([Errno 5] Input/output error); "
                      f"not checked", output)

    def test_a_manifest_link_to_the_report_whose_inspection_fails_is_never_read(self):
        root, manifest, link, report, code, output, reads = self.run_alias("manifest", io_error(), "eio")
        self.assert_uncertain(root, link, report, code, output, "[Errno 5] Input/output error")
        self.assertNotIn(manifest, reads, "the manifest link was read")
        self.assertIn(f"{manifest}: ERROR — could not check whether this is the --csv report "
                      f"this run is writing ([Errno 5] Input/output error); not read", output)

    def test_other_inspection_errors_are_treated_the_same(self):
        for role, error, text in (("master", denied(), "[Errno 13] Permission denied"),
                                  ("master", OSError(errno.ELOOP, "Too many levels of symbolic links"),
                                   f"[Errno {errno.ELOOP}] Too many levels of symbolic links"),
                                  ("manifest", denied(), "[Errno 13] Permission denied")):
            with self.subTest(role=role, error=text):
                self.hashed.clear()
                label = f"{role}-{error.errno}"
                root, manifest, link, report, code, output, reads = self.run_alias(role, error, label)
                self.assert_uncertain(root, link, report, code, output, text)
                self.assertNotIn(manifest if role == "manifest" else link, reads)

    def test_control_established_absence_keeps_its_outcome_and_the_report(self):
        # No such file, a dangling link, and a path component that is a file
        for label in ("missing", "dangling", "not-a-folder"):
            with self.subTest(label):
                self.hashed.clear()
                root = self.dir / label
                folder = root / "bundle"
                folder.mkdir(parents=True)
                (folder / "disc_manifest.json").write_text(json.dumps(record("disc.iso")))
                if label == "dangling":
                    os.symlink(root / "unmounted" / "disc.iso", folder / "disc.iso")
                elif label == "not-a-folder":
                    (root / "plain").write_bytes(b"a file, not a folder")
                    os.symlink(root / "plain" / "disc.iso", folder / "disc.iso")
                self.master(folder=root / "sibling")
                report = root / "report.csv"
                code, output = self.cli(root, report=report)
                self.assertEqual(code, 1, output)
                self.assertIn("MISSING_ISO: ISO not found next to manifest: disc.iso", output)
                self.assertIn(f"CSV report written to: {report}", output)
                rows = list(csv.reader(io.StringIO(report.read_text(encoding="utf-8"))))
                # A dangling link is also an EXCLUDED part of the search (T-134)
                self.assertEqual(sorted(row[0] for row in rows[1:]),
                                 ["MISSING_ISO", "PASS"] if label == "missing"
                                 else ["EXCLUDED", "MISSING_ISO", "PASS"])

    def test_control_without_csv_nothing_changes(self):
        root, manifest, link, report = self.alias_layout("master", "no-report")
        code, output = self.cli(root)  # no reservation: the link dangles, the sibling passes
        self.assertEqual(code, 1, output)
        self.assertIn("MISSING_ISO: ISO not found next to manifest: disc.iso", output)
        self.assertEqual(self.hashed, [root / "sibling" / "disc.iso"])
        self.assertNotIn("--csv", output)
        self.assertFalse(os.path.lexists(report))
        healthy = self.dir / "healthy"
        self.master(folder=healthy)
        self.assertEqual(self.cli(healthy)[0], 0)

    def unresolvable_alias(self, inject):
        """T-148 repair: coll/link points at other/deep; other's manifest,
        given as coll/link/../disc_manifest.json, names report.csv (missing
        beside it), and the report is coll/report.csv - a different file
        that a guessed coll/link/.. (coll) would name. With inject,
        inspecting coll/link fails (EIO) whenever the alias check resolves a
        path through it; discovery is untouched. A healthy bundle is given too"""
        coll, other, healthy = self.dir / "coll", self.dir / "other", self.dir / "healthy"
        (other / "deep").mkdir(parents=True)
        coll.mkdir()
        (other / "disc_manifest.json").write_text(json.dumps(record("report.csv")))
        os.symlink(other / "deep", coll / "link")
        self.master(folder=healthy)
        self.hashed.clear()
        given, report = coll / "link" / ".." / "disc_manifest.json", coll / "report.csv"
        if not inject:
            return given, healthy, report, [], self.cli(given, healthy, report=report)
        with resolution_fails(coll / "link", io_error(), under="_report_alias") as fired:
            result = self.cli(given, healthy, report=report)
        return given, healthy, report, fired, result

    def test_an_alias_check_whose_path_cannot_be_resolved_stays_uncertain(self):
        # Never a false proven alias, never a bypass: the manifest is an
        # ERROR (not read), the healthy bundle is still checked, and the
        # report is not kept
        given, healthy, report, fired, (code, output) = self.unresolvable_alias(inject=True)
        self.assertIn("_physical", fired, "the fault never reached the alias check's resolution")
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [healthy / "disc.iso"])
        self.assertEqual(output.count("PASS: verified"), 1, output)
        self.assertIn(f"{given}: ERROR — could not check whether this is the --csv report this run "
                      f"is writing ([Errno 5] Input/output error); not read", output)
        self.assertIn("for 1 manifest(s) it could not be checked whether they or their ISO "
                      "are the report", output)
        self.assertNotIn("name it as their ISO or as themselves", output)  # not a proven alias
        self.assertNotIn("CSV report written to", output)
        self.assertFalse(os.path.lexists(report), "a report that may be an input was kept")

    def test_control_the_same_alias_check_without_a_fault_keeps_the_report(self):
        given, healthy, report, _, (code, output) = self.unresolvable_alias(inject=False)
        self.assertEqual(code, 1, output)
        self.assertIn("MISSING_ISO: ISO not found next to manifest: report.csv", output)
        self.assertEqual(self.hashed, [healthy / "disc.iso"])
        self.assertNotIn("is the --csv report", output)
        self.assertNotIn("Not keeping CSV report", output)
        self.assertIn(f"CSV report written to: {report}", output)
        rows = list(csv.reader(io.StringIO(report.read_text(encoding="utf-8"))))
        self.assertEqual(sorted(r[0] for r in rows[1:]), ["MISSING_ISO", "PASS"])


class ReportOwnershipTests(Base):
    """T-138: a report path that stops naming this run's file while the
    report is written is never announced as saved, and a path whose
    ownership cannot be established is never unlinked; another file at the
    path stays byte-for-byte. Faults are injected at os.fdopen (just after
    the last pre-write check) and os.fstat (the reservation's own check)"""

    FOREIGN = b"SYNTHETIC FOREIGN FILE AT THE REPORT PATH\n"

    def test_a_report_replaced_or_removed_while_it_is_written_is_not_announced(self):
        real_fdopen = os.fdopen
        for change in ("replaced", "removed"):
            with self.subTest(change):
                folder = self.dir / change
                iso, manifest = self.master(folder=folder)
                inputs = (iso.read_bytes(), manifest.read_bytes())
                report = folder / "report.csv"
                fired = []

                def late_change(fd, *args, change=change, report=report, fired=fired, **kwargs):
                    report.unlink()
                    if change == "replaced":
                        report.write_bytes(self.FOREIGN)
                    fired.append(change)
                    return real_fdopen(fd, *args, **kwargs)

                with mock.patch.object(os, "fdopen", late_change):
                    code, output = self.cli(folder, report=report)
                self.assertEqual(fired, [change], "the fault never fired")
                self.assertEqual(code, 1, output)
                self.assertIn(f"Could not save CSV report: {report} was {change}", output)
                self.assertNotIn("CSV report written to", output)
                if change == "replaced":
                    self.assertEqual(report.read_bytes(), self.FOREIGN)
                else:
                    self.assertFalse(os.path.lexists(report))
                self.assertEqual((iso.read_bytes(), manifest.read_bytes()), inputs)

    def fstat_failing(self, report, replace):
        real_fstat = os.fstat
        fired = []

        def fstat(fd):
            # Only the reservation's own check: the first fstat once it exists
            if not fired and os.path.lexists(report):
                if replace:
                    report.unlink()
                    report.write_bytes(self.FOREIGN)
                fired.append(replace)
                raise OSError(errno.EIO, "synthetic: Input/output error")
            return real_fstat(fd)

        return mock.patch.object(os, "fstat", fstat), fired

    def test_an_fstat_failure_after_replacement_leaves_the_foreign_file(self):
        self.master()
        report = self.dir / "report.csv"
        patcher, fired = self.fstat_failing(report, replace=True)
        with patcher:
            code, output = self.cli(self.dir, report=report)
        self.assertEqual(fired, [True], "the fault never fired")
        self.assertEqual(code, 1, output)
        self.assertIn("cannot confirm that --csv", output)
        self.assertIn("Input/output error", output)
        self.assertEqual(report.read_bytes(), self.FOREIGN)
        self.assertEqual(self.hashed, [], "an ISO was hashed after the report was refused")

    def test_an_fstat_failure_alone_leaves_the_unconfirmed_reservation(self):
        self.master()
        report = self.dir / "report.csv"
        patcher, fired = self.fstat_failing(report, replace=False)
        with patcher:
            code, output = self.cli(self.dir, report=report)
        self.assertEqual(fired, [False], "the fault never fired")
        self.assertEqual(code, 1, output)
        self.assertIn("left in place", output)
        self.assertEqual(report.read_bytes(), b"")  # this run's own empty file, unproven
        self.assertEqual(self.hashed, [])

    def test_control_an_unchanged_report_path_is_written_and_announced(self):
        iso, _ = self.master()
        report = self.dir / "report.csv"
        code, output = self.cli(self.dir, report=report)
        self.assertEqual(code, 0, output)
        self.assertIn(f"CSV report written to: {report}", output)
        self.assertEqual([r[0] for r in list(csv.reader(report.open(encoding="utf-8")))[1:]], ["PASS"])

    UNCHECKED = "could not be checked ([Errno 5] Input/output error)"

    @contextlib.contextmanager
    def ownership_unchecked(self, stage):
        """The ownership check (_report_lost) cannot be made, once: before
        the report is written ("pre": the reservation is still empty) or
        after ("post": it holds the report). Every other call is the real
        check; yields the stages it fired at"""
        real, fired = verify._report_lost, []

        def report_lost(path, identity):
            if not fired and (stage == "pre" or os.path.getsize(path) > 0):
                fired.append(stage)
                return self.UNCHECKED
            return real(path, identity)

        with mock.patch.object(verify, "_report_lost", report_lost):
            yield fired

    def test_a_report_whose_ownership_could_not_be_checked_is_left_in_place(self):
        # T-149 (F5c): "nothing there was removed" stays true - a later check
        # that would succeed never removes it
        for stage, said in (("pre", "Could not write CSV report"), ("post", "Could not save CSV report")):
            with self.subTest(stage):
                folder = self.dir / stage
                self.master(folder=folder)
                report = folder / "report.csv"
                with self.ownership_unchecked(stage) as fired:
                    code, output = self.cli(folder, report=report)
                self.assertEqual(fired, [stage], "the fault never fired")
                self.assertEqual(code, 1, output)
                self.assertIn(f"{said}: {report} {self.UNCHECKED}", output)
                self.assertIn("nothing there was removed", output)
                self.assertNotIn("CSV report written to", output)
                self.assertTrue(os.path.lexists(report), "a path whose ownership was not established was removed")
                with open(report, newline="", encoding="utf-8") as f:
                    rows = list(csv.reader(f))
                self.assertEqual([r[0] for r in rows[1:]] if rows else None,
                                 None if stage == "pre" else ["PASS"])

    def test_cleanup_that_cannot_check_ownership_leaves_the_file_and_says_so(self):
        # T-149: Ctrl-C, and the cleanup's own ownership check cannot be made
        self.master("a")
        self.master("b")
        report = self.dir / "report.csv"
        checks = []

        def report_lost(path, identity):
            checks.append(Path(path))
            return self.UNCHECKED

        def interrupt(path, label=""):
            self.hashed.append(Path(path))
            raise KeyboardInterrupt

        with mock.patch.object(verify, "hash_file", interrupt), \
                mock.patch.object(verify, "_report_lost", report_lost):
            code, output = self.cli(self.dir, report=report)
        self.assertEqual(checks, [report], "the cleanup check never ran")
        self.assertEqual(code, 130, output)
        self.assertIn(f"The unfinished --csv report was left in place: whether {report} still names "
                      f"the file this run created {self.UNCHECKED}", output)
        self.assertEqual(report.read_bytes(), b"")

    def test_ctrl_c_while_the_report_is_reserved_exits_130_and_names_the_path(self):
        # T-149 (F6), injected: the interrupt lands in the reservation's own
        # fstat; the file's identity is unproven, so it is left and named
        self.master()
        report = self.dir / "report.csv"
        real_fstat, fired = os.fstat, []

        def fstat(fd):
            if not fired and os.path.lexists(report):
                fired.append(fd)
                raise KeyboardInterrupt
            return real_fstat(fd)

        with mock.patch.object(os, "fstat", fstat):
            code, output = self.cli(self.dir, report=report)
        self.assertEqual(len(fired), 1, "the interrupt never fired")
        self.assertEqual(code, 130, output)
        self.assertIn(f"Interrupted by operator (Ctrl-C) while reserving the --csv report; anything now "
                      f"at {report} was left in place (if this run created it, it is empty)", output)
        self.assertEqual(report.read_bytes(), b"")
        self.assertEqual(self.hashed, [])


class ClassificationTests(Base):
    """T-125: families by content, status whitelist, dry-run and integrity rules"""

    def check(self, content, name="disc_manifest.json", folder=None):
        return status_of(self.write_manifest(name, content, folder))

    def test_missing_integrity_record_is_unverifiable(self):
        m = record()
        del m["integrity_verification"]
        (self.dir / "disc.iso").write_bytes(b"tampered")
        self.assertEqual(self.check(m), "UNVERIFIABLE")

    def test_unknown_or_non_text_status_is_unverifiable(self):
        for value in ("succes", "SUCCESS", "completed", 1, None, ["success"]):
            with self.subTest(status=value):
                self.assertEqual(self.check(record(backup_status={"overall_status": value})),
                                 "UNVERIFIABLE")

    def test_unknown_status_is_unverifiable_even_on_a_dry_run_record(self):
        for value in ("succes", "damaged", "unrecognized"):
            with self.subTest(status=value):
                m = {"backup_metadata": {"run_id": "synthetic", "dry_run": True},
                     "backup_status": {"overall_status": value},
                     "output_files": {"iso_filename": "disc.iso"}}
                path = self.write_manifest("disc_manifest.json", m)
                self.assertEqual(status_of(path), "UNVERIFIABLE")
                self.assertEqual(self.cli(self.dir)[0], 1)

    def test_explicit_no_master_statuses_skip(self):
        for value in ("failed", "verification_failed"):
            with self.subTest(status=value):
                self.assertEqual(self.check(record(backup_status={"overall_status": value})),
                                 "SKIPPED")

    def test_dry_run_record_skips_only_without_an_iso(self):
        audit_fix_style = record(backup_status={"overall_status": "dry_run"})
        main_style = record(backup_metadata={"run_id": "x", "dry_run": True})
        main_style["integrity_verification"] = {"source_hash": "dry_run_hash"}
        for label, m in (("dry_run status", audit_fix_style), ("main-era dry_run flag", main_style)):
            with self.subTest(case=label):
                folder = self.dir / label.replace(" ", "_")
                folder.mkdir()
                self.assertEqual(self.check(m, folder=folder), "SKIPPED")
                (folder / "disc.iso").write_bytes(b"the real master")
                self.assertEqual(self.check(m, folder=folder), "UNVERIFIABLE")

    def test_dry_run_record_beside_an_uninspectable_iso_is_an_error(self):
        # T-147 (N13): an inspection error is never taken for "no ISO here"
        audit_fix_style = record(backup_status={"overall_status": "dry_run"})
        main_style = record(backup_metadata={"run_id": "x", "dry_run": True})
        main_style["integrity_verification"] = {"source_hash": "dry_run_hash"}
        for label, m in (("dry_run status", audit_fix_style), ("main-era dry_run flag", main_style)):
            with self.subTest(case=label):
                folder = self.dir / label.replace(" ", "_")
                folder.mkdir()
                manifest = self.write_manifest("disc_manifest.json", m, folder)
                iso = folder / "disc.iso"
                iso.write_bytes(b"the real master")
                with lstat_fails(iso, io_error()) as fired:
                    result = verify.check_manifest(manifest)
                    code, output = self.cli(folder)
                self.assertEqual(fired, [iso, iso], "the fault did not reach both checks")
                self.assertEqual(result.status, "ERROR", result.detail)
                self.assertIn("could not check whether an ISO sits beside this dry-run record (disc.iso): "
                              "[Errno 5] Input/output error", result.detail)
                self.assertEqual(code, 1, output)
                self.assertNotIn("SKIPPED", output)

    def test_iso_names_no_file_can_have_are_unverifiable(self):
        # T-147 (F9): an embedded NUL or an unencodable character, with or
        # without --csv; the report is kept
        for label, name in (("nul", "disc\x00.iso"), ("surrogate", "\ud800.iso")):
            for with_csv in (False, True):
                with self.subTest(name=label, csv=with_csv):
                    self.hashed.clear()
                    folder = self.dir / f"{label}-{with_csv}"
                    folder.mkdir()
                    self.write_manifest("disc_manifest.json", record(name), folder)
                    report = self.dir / f"{label}-{with_csv}.csv" if with_csv else None
                    code, output = self.cli(folder, report=report)
                    self.assertEqual(code, 1, output)
                    self.assertIn(f"UNVERIFIABLE: manifest ISO filename is not a usable file name: "
                                  f"{name!r}", output)
                    self.assertEqual(self.hashed, [])
                    if with_csv:
                        self.assertIn(f"CSV report written to: {report}", output)
                        rows = list(csv.reader(io.StringIO(report.read_text(encoding="utf-8"))))
                        self.assertEqual([r[0] for r in rows[1:]], ["UNVERIFIABLE"])

    def test_declared_sha256_without_its_hash_is_unverifiable(self):
        data = b"healthy synthetic master"
        (self.dir / "disc.iso").write_bytes(data)
        m = record(data=data)
        m["integrity_verification"] = {"hash_algorithms": ["MD5", "SHA-256"],
                                       "source_hash": hashes(data)[0]}
        self.assertEqual(self.check(m), "UNVERIFIABLE")
        # The older MD5-only shape (no algorithms list) still verifies
        m["integrity_verification"] = {"source_hash": hashes(data)[0]}
        self.assertEqual(self.check(m), "PASS")

    def test_unrecognized_manifest_is_unverifiable(self):
        self.assertEqual(self.check({"notes": "some other tool"}), "UNVERIFIABLE")

    def test_access_copy_records_skip(self):
        for name, content in (
                ("disc_access_manifest.json", {"conversion_metadata": {}, "conversion_status": {}}),
                ("disc_skip_manifest.json", {"conversion_metadata": {}, "titlesets": {}}),
                ("disc_failed-20261024T000000-abcdef_manifest.json",
                 {"conversion_metadata": {}, "conversion_status": {"overall_status": "error"}})):
            with self.subTest(name=name):
                self.assertEqual(self.check(content, name=name), "SKIPPED")

    def test_preservation_markers_win_over_conversion_markers(self):
        m = record(conversion_metadata={})
        del m["integrity_verification"]
        self.assertEqual(self.check(m), "UNVERIFIABLE")

    def test_a_master_named_like_an_access_record_is_still_checked(self):
        iso, manifest = self.master("Interview_access")
        code, output = self.cli(self.dir)
        self.assertEqual(code, 0, output)
        self.assertIn(iso, self.hashed, "the master was never examined")

    def test_appledouble_companions_skip_by_content_not_name(self):
        # AppleDouble metadata is recognised by its magic number; a real
        # master whose name starts with ._ is still checked
        self.master()
        (self.dir / "._disc_manifest.json").write_bytes(
            b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \xff\xfe\x00\x01")
        dotted_iso, _ = self.master("._JPC_AV_00001")
        code, output = self.cli(self.dir)
        self.assertEqual(code, 0, output)
        self.assertIn("Found 3 manifest(s)", output)
        self.assertIn("SKIPPED: macOS AppleDouble metadata file", output)
        self.assertIn(dotted_iso, self.hashed)

    def test_missing_md5_source_hash_is_unverifiable(self):
        data = b"healthy synthetic master"
        (self.dir / "disc.iso").write_bytes(data)
        m = record(data=data)
        m["integrity_verification"] = {"hash_algorithms": ["MD5", "SHA-256"],
                                       "sha256_source_hash": hashes(data)[1]}
        self.assertEqual(self.check(m), "UNVERIFIABLE")

    def test_malformed_hash_text_is_unverifiable_not_a_mismatch(self):
        data = b"healthy synthetic master"
        (self.dir / "disc.iso").write_bytes(data)
        for bad in ("N/A", hashes(data)[0][:31], "z" * 32):
            with self.subTest(source_hash=bad):
                m = record(data=data)
                m["integrity_verification"]["source_hash"] = bad
                self.assertEqual(self.check(m), "UNVERIFIABLE")

    def test_both_family_markers_never_skip(self):
        for status in ("failed", "dry_run", "success"):
            with self.subTest(status=status):
                m = record(backup_status={"overall_status": status},
                           conversion_metadata={}, conversion_status={"overall_status": "success"})
                self.assertEqual(self.check(m), "UNVERIFIABLE")

    def test_dot_dot_iso_name_is_unverifiable(self):
        m = record()
        m["output_files"] = {"iso_filename": ".."}
        self.assertEqual(self.check(m), "UNVERIFIABLE")


class MalformedItemTests(Base):
    """T-126: malformed items become per-item results; the run goes on"""

    def broken_variants(self):
        good = record()
        variants = {}
        for key, bad in (("backup_metadata", None), ("integrity_verification", []),
                         ("output_files", None), ("backup_status", "success")):
            m = copy.deepcopy(good)
            m[key] = bad
            variants[f"{key}={bad!r}"] = m
        m = copy.deepcopy(good)
        m["output_files"] = {"iso_filename": 5}
        variants["iso_filename=5"] = m
        m = copy.deepcopy(good)
        m["backup_metadata"] = {"run_id": 7}
        variants["run_id=7"] = m
        m = copy.deepcopy(good)
        m["integrity_verification"]["hash_algorithms"] = "SHA-256"
        variants["hash_algorithms='SHA-256'"] = m
        return variants

    def test_wrong_field_types_are_unverifiable(self):
        for label, m in self.broken_variants().items():
            with self.subTest(case=label):
                self.assertEqual(status_of(self.write_manifest("disc_manifest.json", m)),
                                 "UNVERIFIABLE")

    def test_unreadable_content_is_an_error(self):
        for label, content in (("invalid UTF-8", b'{"backup_status": "\xff\xfe"}'),
                               ("invalid JSON", b"{not json"),
                               ("array root", b"[]"), ("null root", b"null"),
                               ("string root", b'"backup_metadata"'), ("number root", b"5")):
            with self.subTest(case=label):
                self.assertEqual(status_of(self.write_manifest("disc_manifest.json", content)),
                                 "ERROR")

    def test_bad_items_first_do_not_stop_the_healthy_one(self):
        self.master("z_good")
        self.write_manifest("a_null_metadata_manifest.json", self.broken_variants()["backup_metadata=None"])
        self.write_manifest("b_bad_utf8_manifest.json", b'{"backup_metadata": "\xff"}')
        self.write_manifest("c_array_manifest.json", b"[]")
        report = self.dir / "report.csv"
        code, output = self.cli(self.dir, report=report)
        self.assertEqual(code, 1, output)
        self.assertIn("Checked 4 manifest(s)", output)
        statuses = sorted(r.split(",")[0] for r in report.read_text().splitlines()[1:])
        self.assertEqual(statuses, ["ERROR", "ERROR", "PASS", "UNVERIFIABLE"])

    def test_an_unexpected_failure_is_an_error_for_that_item_only(self):
        iso_a, _ = self.master("a")
        self.master("b")
        real_hash = verify.hash_file

        def flaky(path, label=""):
            self.hashed.append(Path(path))
            if Path(path) == iso_a:
                raise ValueError("synthetic unexpected failure")
            return real_hash(path, label)

        with mock.patch.object(verify, "hash_file", flaky):
            code, output = self.cli(self.dir)
        self.assertEqual(code, 1, output)
        self.assertIn("ERROR: could not check this manifest: ValueError", output)
        self.assertIn("PASS", output)


class FixtureScenarioTests(Base):
    """The eight built-in make_verify_fixtures scenarios keep their statuses"""

    def test_eight_fixture_scenarios(self):
        with contextlib.redirect_stdout(io.StringIO()):
            fixtures.build_fixtures(self.dir)
        self.assertEqual(len(fixtures.EXPECTED), 8)
        for name, expected in fixtures.EXPECTED.items():
            with self.subTest(scenario=name):
                self.assertEqual(status_of(self.dir / name / f"{name}_manifest.json"), expected)


class MasterInspectionTests(Base):
    """T-147: the ISO a manifest names is inspected before it is opened, the
    same way on every Python version - no such file is MISSING_ISO, any other
    inspection error is ERROR with its errno, and anything but a regular
    file is ERROR and never opened; a descriptor that is not a regular file
    once opened is never read; check_manifest classifies its manifest too.
    Every case asserts that the tripwire saw no open of a special file"""

    def setUp(self):
        super().setUp()
        self._tripwire_before = len(TRIPWIRE_OPENS)
        self.addCleanup(self._no_special_file_opens)
        patcher = mock.patch.object(verify, "ANSI_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _no_special_file_opens(self):
        self.assertEqual(TRIPWIRE_OPENS[self._tripwire_before:], [],
                         "the product tried to open a FIFO or device (the tripwire stopped it)")

    @staticmethod
    def bundle(folder, iso_name="disc.iso", data=b"healthy synthetic master"):
        """A success record in folder for data; nothing is put at the ISO name"""
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / "disc_manifest.json"
        manifest.write_text(json.dumps(record(iso_name, data)))
        return manifest, folder / iso_name

    def test_anything_but_a_regular_file_at_the_iso_name_is_never_opened(self):
        for label in ("fifo", "link-to-fifo", "folder"):
            with self.subTest(label):
                root = self.dir / label
                self.master(folder=root / "sibling")
                _, iso = self.bundle(root / "bundle")
                if label == "fifo":
                    os.mkfifo(iso)
                elif label == "link-to-fifo":
                    os.mkfifo(root / "bundle" / "pipe")
                    os.symlink(root / "bundle" / "pipe", iso)
                else:
                    iso.mkdir()
                self.hashed.clear()
                before = len(TRIPWIRE_OPENS)
                code, output = self.cli(root)
                self.assertEqual(TRIPWIRE_OPENS[before:], [], "the product tried to open a FIFO")
                self.assertEqual(code, 1, output)
                self.assertEqual(self.hashed, [root / "sibling" / "disc.iso"])
                self.assertIn(f"{iso}: ERROR — ISO disc.iso is not a regular file (folder, FIFO, device "
                              f"or socket); not opened", output)

    def test_a_master_that_is_not_a_regular_file_once_opened_is_never_read(self):
        # The descriptor's own fstat decides: a file swapped for a FIFO after
        # it was inspected is never read (here fstat reports a FIFO)
        iso, manifest = self.master()
        st = os.stat(iso)
        identity, real_fstat, fired = (st.st_dev, st.st_ino), os.fstat, []

        def fstat(fd):
            result = real_fstat(fd)
            if (result.st_dev, result.st_ino) == identity:
                fired.append(fd)
                return os.stat_result((stat.S_IFIFO | 0o600,) + tuple(result)[1:])
            return result

        with mock.patch.object(os, "fstat", fstat):
            code, output = self.cli(self.dir)
        self.assertEqual(code, 1, output)
        self.assertEqual(len(fired), 1, "the fault never fired")
        self.assertIn("ERROR: ISO disc.iso changed to a non-regular file during the check; not read", output)
        self.assertNotIn("PASS", output)

    @contextlib.contextmanager
    def descriptor_events(self, path, fault=None):
        """Records, in order, what happens to the descriptor os.open returns
        for path: ("os.open", flags), ("fstat",), ("set_blocking", flag),
        ("file",) for the file object made from it, ("read",) and ("close",).
        fault: "fifo" - its fstat reports a FIFO; "made" - Ctrl-C lands just
        as the file object is made (the object exists, then is dropped);
        "read" - Ctrl-C lands in its first read. Yields (events, fds)"""
        real_open, real_fstat = os.open, os.fstat
        real_set_blocking, real_close = os.set_blocking, os.close
        events, fds = [], []

        def os_open(name, flags, *args, **kwargs):
            fd = real_open(name, flags, *args, **kwargs)
            if not isinstance(name, int) and Path(os.fsdecode(name)) == path:
                fds.append(fd)
                events.append(("os.open", flags))
            return fd

        def fstat(fd):
            result = real_fstat(fd)
            if fd in fds:
                events.append(("fstat",))
                if fault == "fifo":
                    return os.stat_result((stat.S_IFIFO | 0o600,) + tuple(result)[1:])
            return result

        def set_blocking(fd, blocking):
            if fd in fds:
                events.append(("set_blocking", blocking))
            return real_set_blocking(fd, blocking)

        class Interrupting:
            """The file object, except that its first read is a Ctrl-C"""
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()
                return False

            def read(self, *args):
                events.append(("read",))
                raise KeyboardInterrupt

        def file_open(file, *args, **kwargs):
            f = io.open(file, *args, **kwargs)
            if isinstance(file, int) and file in fds:
                events.append(("file",))
                if fault == "made":
                    del f  # dropped as the interrupt unwinds
                    raise KeyboardInterrupt
                if fault == "read":
                    return Interrupting(f)
            return f

        def close(fd):
            if fd in fds:
                events.append(("close",))
            return real_close(fd)

        with mock.patch.object(os, "open", os_open), mock.patch.object(os, "fstat", fstat), \
                mock.patch.object(os, "set_blocking", set_blocking), mock.patch.object(os, "close", close), \
                mock.patch.object(verify, "open", file_open, create=True):
            yield events, fds

    def assert_closed(self, fd):
        """fd no longer names an open descriptor (nothing was opened since)"""
        with self.assertRaises(OSError) as caught:
            os.fstat(fd)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_a_master_is_opened_without_waiting_then_read_with_blocking_reads(self):
        # The protocol: open O_RDONLY|O_NONBLOCK (a FIFO cannot block the
        # open), fstat, only then blocking reads; the descriptor closed once
        data = b"healthy synthetic master"
        iso, _ = self.master(data=data)
        with self.descriptor_events(iso) as (events, fds):
            digests = verify.hash_file(iso, iso.name)
        self.assertEqual(digests, hashes(data))
        self.assertEqual(len(fds), 1, "the master was not opened through os.open")
        self.assert_closed(fds[0])
        self.assertEqual([e[0] for e in events], ["os.open", "fstat", "set_blocking", "file", "close"])
        flags = events[0][1]
        self.assertTrue(flags & os.O_NONBLOCK, "opened without O_NONBLOCK")
        self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
        self.assertEqual(events[2], ("set_blocking", True))

    def test_a_descriptor_that_is_not_a_regular_file_is_closed_unread(self):
        iso, _ = self.master()
        with self.descriptor_events(iso, fault="fifo") as (events, fds):
            with self.assertRaises(OSError) as caught:
                verify.hash_file(iso, iso.name)
        self.assertIn(("fstat",), events, "the fault never fired")
        self.assertEqual(type(caught.exception).__name__, "NotRegularFile")
        # never made blocking, no file object (so nothing read), closed once
        self.assertEqual([e[0] for e in events], ["os.open", "fstat", "close"])
        self.assert_closed(fds[0])

    def test_ctrl_c_while_a_master_is_opened_or_read_stays_a_ctrl_c(self):
        # T-149 review: the interrupt is never turned into an ERROR for this
        # item (a second close raising EBADF), and the descriptor is closed once
        for fault in ("made", "read"):
            with self.subTest(fault):
                iso, _ = self.master(fault)
                with self.descriptor_events(iso, fault=fault) as (events, fds):
                    with self.assertRaises(KeyboardInterrupt):
                        verify.hash_file(iso, iso.name)
                self.assertIn(("file",) if fault == "made" else ("read",), events, "the fault never fired")
                self.assertEqual(events.count(("close",)), 1, events)
                self.assertEqual(events[-1], ("close",))
                self.assert_closed(fds[0])
        # Through a run: Ctrl-C stops it (exit 130), no item becomes an ERROR
        self.hashed.clear()
        iso_a, _ = self.master("made")
        with self.descriptor_events(iso_a, fault="made") as (events, _):
            code, output = self.cli(self.dir)
        self.assertIn(("file",), events, "the fault never fired")
        self.assertEqual(code, 130, output)
        self.assertEqual(self.hashed, [iso_a], "the run went on after Ctrl-C")
        self.assertNotIn("ERROR", output)

    @contextlib.contextmanager
    def iso_stat_fails(self, iso, error):
        """os.stat and Path.stat (following links) raise error for iso. Path.stat
        too: Python 3.9's pathlib reaches os.stat through an accessor bound at
        import, which an os.stat fake never reaches. Yields what fired"""
        real_stat, real_path_stat, fired = os.stat, Path.stat, []

        def os_stat(path, *args, **kwargs):
            if not isinstance(path, int) and Path(os.fsdecode(path)) == iso \
                    and kwargs.get("follow_symlinks", True):
                fired.append("os.stat")
                raise error
            return real_stat(path, *args, **kwargs)

        def path_stat(path, *args, **kwargs):
            if path == iso and kwargs.get("follow_symlinks", True):
                fired.append("Path.stat")
                raise error
            return real_path_stat(path, *args, **kwargs)

        with mock.patch.object(os, "stat", os_stat), mock.patch.object(Path, "stat", path_stat):
            yield fired

    def test_iso_inspection_errors_are_errors_on_every_python(self):
        # T-147 (N13): never "ISO not found" for a master that may be there
        for error in (io_error(), denied(), OSError(errno.ELOOP, "Too many levels of symbolic links")):
            with self.subTest(errno.errorcode[error.errno]):
                iso, manifest = self.master(folder=self.dir / errno.errorcode[error.errno])
                with self.iso_stat_fails(iso, error) as fired:
                    result = verify.check_manifest(manifest)
                self.assertTrue(fired, "the fault never fired")
                self.assertEqual(result.status, "ERROR", result.detail)
                self.assertIn(f"[Errno {error.errno}]", result.detail)
                self.assertNotIn("not found", result.detail)
                self.assertEqual(self.hashed, [])

    def test_a_real_master_behind_a_folder_that_cannot_be_read_is_an_error(self):
        root = self.dir / "collection"
        _, link = self.bundle(root / "bundle")
        locked = self.dir / "locked"
        locked.mkdir()
        (locked / "disc.iso").write_bytes(b"healthy synthetic master")
        os.symlink(locked / "disc.iso", link)
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        try:
            os.stat(link)
        except PermissionError:
            pass
        else:
            self.fail("the master stayed inspectable (running as root?), so this check cannot run")
        code, output = self.cli(root)
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [])
        self.assertNotIn("MISSING_ISO", output)
        lines = [line for line in output.splitlines() if line.startswith(f"  {link}: ERROR — ")]
        self.assertEqual(len(lines), 1, output)
        self.assertIn("[Errno 13]", lines[0])

    def test_a_symlink_chain_too_long_to_follow_at_the_iso_name_is_an_error(self):
        # The system gives up on it (ELOOP), so what it leads to is unknown:
        # ERROR, never MISSING_ISO and never a pass
        root = self.dir / "collection"
        _, iso = self.bundle(root / "bundle")
        target = self.dir / "elsewhere" / "disc.iso"
        target.parent.mkdir()
        target.write_bytes(b"CORRUPTED synthetic master")
        symlink_chain(iso, self.dir / "chain", 41, target)
        assert_beyond_the_system_limit(self, iso)
        code, output = self.cli(root)
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [])
        self.assertNotIn("MISSING_ISO", output)
        self.assertIn(f"{iso}: ERROR — could not inspect ISO disc.iso: [Errno {errno.ELOOP}] ", output)

    def test_check_manifest_never_opens_a_special_file_handed_to_it(self):
        # T-147 (F14): a library call, with no discovery in front of it
        fifo = self.dir / "pipe_manifest.json"
        os.mkfifo(fifo)
        link = self.dir / "link_manifest.json"
        os.symlink(fifo, link)
        for path in (fifo, link):
            with self.subTest(path.name):
                before = len(TRIPWIRE_OPENS)
                try:
                    result = verify.check_manifest(path)
                    outcome = (result.status, result.detail)
                except Exception as e:
                    outcome = (f"raised {type(e).__name__}", str(e))
                self.assertEqual(TRIPWIRE_OPENS[before:], [], "check_manifest tried to open a FIFO")
                self.assertEqual(outcome, ("ERROR", "not a regular file (folder, FIFO, device or socket); "
                                                    "not opened"))

    def test_a_manifest_whose_classification_fails_is_never_read(self):
        # Owner rule: a failed classification never falls through to an open
        # (a library call, with no discovery in front): an inspection error
        # (injected), or a folder named like a manifest (real)
        _, manifest = self.master()
        folder = self.dir / "folder_manifest.json"
        folder.mkdir()
        reads, real_read = [], Path.read_bytes

        def read_bytes(path):
            reads.append(Path(path))
            return real_read(path)

        for path, fault, detail in (
                (manifest, target_stat_fails({manifest}, io_error(), "manifest"),
                 "unreadable manifest: [Errno 5] Input/output error"),
                (folder, contextlib.nullcontext(None),
                 "not a regular file (folder, FIFO, device or socket); not opened")):
            with self.subTest(path.name):
                with fault as calls, mock.patch.object(Path, "read_bytes", read_bytes):
                    result = verify.check_manifest(path)
                self.assertNotIn(path, reads, "the manifest was read after its classification failed")
                self.assertEqual((result.status, result.detail), ("ERROR", detail))
                if calls is not None:
                    self.assertEqual(calls, [("manifest", manifest)], "the fault never fired")
                self.assertEqual(self.hashed, [])

    def test_control_a_master_larger_than_one_read_passes(self):
        data = bytes(range(256)) * (8 * 1024 * 4) + b"!"  # 8 MiB + 1 byte
        iso, manifest = self.master(data=data)
        code, output = self.cli(self.dir)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.hashed, [iso])
        self.assertIn("PASS: verified (MD5+SHA-256)", output)
        result = verify.check_manifest(manifest)
        self.assertEqual((result.status, result.size_bytes), ("PASS", len(data)))
        self.assertEqual((result.md5_actual, result.sha256_actual), hashes(data))

    def test_manifest_text_cannot_rewrite_console_lines(self):
        # T-149 (F8): control characters from a manifest are shown escaped on
        # the console; the CSV keeps exactly what was recorded
        name = "gone\r  PASS: verified (MD5+SHA-256)\x1b[2K"
        self.bundle(self.dir / "bundle", iso_name=name)
        report = self.dir / "report.csv"
        code, output = self.cli(self.dir / "bundle", report=report)
        self.assertEqual(code, 1, output)
        self.assertNotIn("\r", output)
        self.assertNotIn("\x1b", output)
        shown = "gone\\r  PASS: verified (MD5+SHA-256)\\x1b[2K"
        self.assertIn(f"MISSING_ISO: ISO not found next to manifest: {shown}", output)
        self.assertIn(f"{self.dir / 'bundle'}/{shown}: MISSING_ISO", output)
        with open(report, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[1][0], "MISSING_ISO")
        self.assertEqual(rows[1][9], f"ISO not found next to manifest: {name}")

    def test_an_iso_name_cannot_rewrite_the_progress_line(self):
        # T-149 (F8): on a terminal, the hashing progress names the ISO; a
        # name recorded with control characters is shown escaped there too
        iso, _ = self.master("disc\x1b[2K")
        clock = iter(range(0, 10 ** 6))
        with mock.patch.object(verify, "ANSI_ENABLED", True), \
                mock.patch.object(verify.time, "time", lambda: float(next(clock))):
            code, output = self.cli(self.dir)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.hashed, [iso])
        self.assertIn("\r  hashing disc\\x1b[2K.iso: 100%", output)  # the progress line was shown
        self.assertNotIn("\x1b[2K", output)


# --- discovery (T-134) ---------------------------------------------------------

class FakeEntry:
    """A DirEntry stand-in: the real entry, except for the calls a test makes
    fail. predicates_false makes is_dir/is_file/is_symlink return False, as
    the real ones do once an entry has disappeared"""

    def __init__(self, real, stat_error=None, stat_result=None, predicates_false=False):
        self._real, self._stat_error, self._stat_result = real, stat_error, stat_result
        self._false = predicates_false
        self.name, self.path = real.name, real.path

    def __getattr__(self, name):
        return getattr(self._real, name)

    def is_dir(self, *, follow_symlinks=True):
        return False if self._false else self._real.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, *, follow_symlinks=True):
        return False if self._false else self._real.is_file(follow_symlinks=follow_symlinks)

    def is_symlink(self):
        return False if self._false else self._real.is_symlink()

    def stat(self, *, follow_symlinks=True):
        if self._stat_error is not None:
            raise self._stat_error
        if self._stat_result is not None:
            return self._stat_result
        return self._real.stat(follow_symlinks=follow_symlinks)


class FakeListing:
    """os.scandir's iterator for one folder, in name order, that can fail
    after a number of entries and hand out FakeEntry stand-ins"""

    def __init__(self, real, folder, calls, fail_after=None, stand_ins=None):
        with real:
            self.entries = sorted(real, key=lambda e: e.name)
        self.folder, self.calls = folder, calls
        self.fail_after, self.stand_ins = fail_after, stand_ins or {}
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
        if self.fail_after and self.index >= self.fail_after[0]:
            raise self.fail_after[1]
        if self.index >= len(self.entries):
            raise StopIteration
        entry = self.entries[self.index]
        self.index += 1
        changes = self.stand_ins.get(entry.name)
        return FakeEntry(entry, **changes) if changes is not None else entry


@contextlib.contextmanager
def scandir_faults(open_errors=None, fail_after=None, entries=None):
    """os.scandir with faults for chosen folders, delegating everything else
    (the guards, the hashing and the product all use it). open_errors
    {folder: exception}; fail_after {folder: (n, OSError)}; entries {folder:
    {name: FakeEntry keywords}}. Yields the calls it intercepted."""
    real = os.scandir
    open_errors, fail_after, entries = open_errors or {}, fail_after or {}, entries or {}
    calls = []

    def scandir(path=".", *args, **kwargs):
        folder = Path(os.fsdecode(path)) if not isinstance(path, int) else None
        if folder in open_errors:
            calls.append(("open", folder))
            raise open_errors[folder]
        listing = real(path, *args, **kwargs)
        if folder in fail_after or folder in entries:
            calls.append(("listed", folder))
            return FakeListing(listing, folder, calls, fail_after.get(folder), entries.get(folder))
        return listing

    with mock.patch.object(os, "scandir", scandir):
        yield calls


@contextlib.contextmanager
def target_stat_fails(links, error, label="loop"):
    """os.stat, following links, raises error for these existing links (their
    own lstat is untouched). Yields the calls it intercepted, as (label, path)"""
    real = os.stat
    calls = []

    def stat(path, *args, **kwargs):
        if not isinstance(path, int) and Path(os.fsdecode(path)) in links \
                and kwargs.get("follow_symlinks", True):
            calls.append((label, Path(os.fsdecode(path))))
            raise error
        return real(path, *args, **kwargs)

    with mock.patch.object(os, "stat", stat):
        yield calls


@contextlib.contextmanager
def looping(*links):
    """os.stat reports a symlink loop (ELOOP) for these existing links, and
    os.readlink says each points at itself, so following it a link at a
    time reaches the same link again. A real loop link cannot simply be
    used: the guard's Path.resolve() raises on it in 3.9, so it could not
    even be removed afterwards (test_a_real_symlink_loop_holds_nothing
    breaks one before cleanup). Yields the os.stat calls it intercepted"""
    real_readlink = os.readlink

    def readlink(path, *args, **kwargs):
        if not isinstance(path, int) and Path(os.fsdecode(path)) in links:
            return os.path.abspath(os.fsdecode(path))
        return real_readlink(path, *args, **kwargs)

    with target_stat_fails(links, OSError(errno.ELOOP, "Too many levels of symbolic links")) as calls, \
            mock.patch.object(os, "readlink", readlink):
        yield calls


def denied():
    return PermissionError(errno.EACCES, "Permission denied")


def io_error():
    return OSError(errno.EIO, "Input/output error")


def symlink_chain(head: Path, folder: Path, n: int, end: Path):
    """head -> folder/l1 -> l2 -> ... -> folder/l{n} -> end: n + 1 real
    symlinks, made from the end backwards (none of them ever loops). The
    synthetic guard's Path.resolve follows a chain of any length on 3.9 and
    3.14, so cleanup can remove it"""
    folder.mkdir(parents=True, exist_ok=True)
    os.symlink(end, folder / f"l{n}")
    for i in range(n - 1, 0, -1):
        os.symlink(folder / f"l{i + 1}", folder / f"l{i}")
    os.symlink(folder / "l1", head)


def assert_beyond_the_system_limit(test, head: Path):
    """The premise of a chain test: os.stat itself gives up on head (ELOOP).
    Fails, never skips, when the system follows the whole chain"""
    try:
        _REAL_STAT(head)
    except OSError as e:
        test.assertEqual(e.errno, errno.ELOOP, f"os.stat({head}) failed otherwise: {e}")
    else:
        test.fail(f"the system followed the whole chain at {head}, so this check cannot run")


class DiscoveryTests(Base):
    """T-134: every part of the search is accounted for, healthy siblings and
    other paths are still verified, and nothing left unsearched passes"""

    def setUp(self):
        super().setUp()
        self._tripwire_before = len(TRIPWIRE_OPENS)
        self.addCleanup(self._no_special_file_opens)
        patcher = mock.patch.object(verify, "ANSI_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _no_special_file_opens(self):
        self.assertEqual(TRIPWIRE_OPENS[self._tripwire_before:], [],
                         "the product tried to open a FIFO or device (the tripwire stopped it)")

    def masters(self, root, *folders):
        for folder in folders:
            self.master(folder=root / folder)
        self.hashed.clear()

    @staticmethod
    def rows(report):
        with open(report, newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    def exit_message(self, *paths, report=None):
        """main()'s sys.exit() argument (a message string or a status)"""
        argv = ["verifyiso.py"] + [str(p) for p in paths] + (["--csv", str(report)] if report else [])
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                verify.main()
        return caught.exception.code

    def test_healthy_tree_reports_as_before(self):
        root = self.dir / "collection"
        self.masters(root, "a", "b/c", "b/d")
        report = self.dir / "report.csv"
        code, output = self.cli(root, report=report)
        self.assertEqual(code, 0, output)
        self.assertIn("Found 3 manifest(s) to examine\n", output)
        for text in ("DISCOVERY_ERROR", "EXCLUDED", "Search incomplete", "Not searched",
                     "Items needing attention"):
            self.assertNotIn(text, output)
        rows = self.rows(report)
        self.assertEqual(rows[0], ["status", "iso", "manifest", "size_bytes", "run_id",
                                   "md5_expected", "md5_actual", "sha256_expected", "sha256_actual",
                                   "detail", "hash_seconds", "checked_at"])
        self.assertEqual([(r[0], r[2]) for r in rows[1:]],
                         [("PASS", str(root / f / "disc_manifest.json")) for f in ("a", "b/c", "b/d")])

    def test_a_folder_named_like_a_manifest_is_still_searched(self):
        odd = self.dir / "odd_manifest.json"
        self.masters(self.dir, "odd_manifest.json")
        code, output = self.cli(self.dir)
        self.assertEqual(code, 1, output)  # the folder itself is an ERROR row, as before
        self.assertIn(f"{odd}: ERROR", output)
        self.assertEqual(self.hashed, [odd / "disc.iso"])

    def test_a_folder_that_cannot_be_listed_fails_the_search_but_not_its_siblings(self):
        for n, error in enumerate((denied(), io_error())):
            with self.subTest(errno.errorcode[error.errno]):
                root = self.dir / f"case{n}"
                self.masters(root, "a", "b", "c")
                report = self.dir / f"report{n}.csv"
                with scandir_faults(open_errors={root / "b": error}) as calls:
                    code, output = self.cli(root, report=report)
                self.assertIn(("open", root / "b"), calls, "the fault never reached the search")
                self.assertEqual(code, 1, output)
                self.assertNotIn("Traceback", output)
                self.assertEqual(sorted(self.hashed), [root / "a" / "disc.iso", root / "c" / "disc.iso"])
                self.assertIn(f"DISCOVERY_ERROR: {root / 'b'}: could not list folder: [Errno {error.errno}]",
                              output)
                self.assertIn("Search incomplete", output)
                self.assertIn(f"  {root / 'b'}: DISCOVERY_ERROR — could not list folder", output)
                rows = self.rows(report)[1:]
                self.assertEqual([r[0] for r in rows], ["PASS", "PASS", "DISCOVERY_ERROR"])
                self.assertTrue(rows[-1][9].startswith(f"{root / 'b'}: could not list folder"), rows[-1])
                self.assertEqual((rows[-1][1], rows[-1][2], rows[-1][3], rows[-1][10]), ("", "", "0", "0.0"))

    def test_every_bundle_passing_does_not_hide_an_incomplete_search(self):
        # The audit's K1 shape: a readable and a denied subtree, both healthy
        root = self.dir / "collection"
        self.masters(root, "readable", "denied_subtree")
        with scandir_faults(open_errors={root / "denied_subtree": denied()}) as calls:
            code, output = self.cli(root)
        self.assertIn(("open", root / "denied_subtree"), calls, "the fault never reached the search")
        self.assertEqual(code, 1, output)
        self.assertIn("PASS: 1", output)
        self.assertIn("Search incomplete: 1 DISCOVERY_ERROR item(s)", output)

    def test_a_listing_that_stops_partway_keeps_what_it_read(self):
        root = self.dir / "collection"
        self.masters(root, "a", "b", "c")
        with scandir_faults(fail_after={root: (1, io_error())}) as calls:
            code, output = self.cli(root)
        self.assertIn(("listed", root), calls, "the fault never reached the search")
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [root / "a" / "disc.iso"])  # read before the failure
        self.assertIn(f"DISCOVERY_ERROR: {root}: listing stopped partway: [Errno 5]", output)
        self.assertIn(("closed", root), calls, "the listing was not closed")

    def test_entries_that_cannot_be_inspected_or_disappear_keep_their_siblings(self):
        cases = (("inspect", denied(), "could not inspect entry: [Errno 13]"),
                 ("vanished", FileNotFoundError(errno.ENOENT, "No such file or directory"),
                  "entry disappeared during the search"))
        for label, error, text in cases:
            with self.subTest(label):
                root = self.dir / label
                self.masters(root, "a", "b", "c")
                fault = {root: {"a": dict(stat_error=error, predicates_false=True)}}
                with scandir_faults(entries=fault) as calls:
                    code, output = self.cli(root)
                self.assertIn(("listed", root), calls, "the fault never reached the search")
                self.assertEqual(code, 1, output)
                self.assertEqual(sorted(self.hashed), [root / "b" / "disc.iso", root / "c" / "disc.iso"])
                self.assertIn(f"DISCOVERY_ERROR: {root / 'a'}: {text}", output)

    def test_a_folder_that_disappears_is_reported(self):
        root = self.dir / "collection"
        self.masters(root, "a", "b")
        gone = FileNotFoundError(errno.ENOENT, "No such file or directory")
        with scandir_faults(open_errors={root / "b": gone}) as calls:
            code, output = self.cli(root)
        self.assertIn(("open", root / "b"), calls, "the fault never reached the search")
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [root / "a" / "disc.iso"])
        self.assertIn(f"DISCOVERY_ERROR: {root / 'b'}: folder disappeared during the search", output)

    def test_one_bad_given_path_does_not_stop_the_others(self):
        good, unlistable, unstatable = (self.dir / n for n in ("good", "unlistable", "unstatable"))
        for folder in (good, unlistable, unstatable):
            self.masters(folder, "a")
        real_stat = os.stat
        stat_calls = []

        def stat_fault(path, *args, **kwargs):
            if not isinstance(path, int) and Path(os.fsdecode(path)) == unstatable:
                stat_calls.append(unstatable)
                raise io_error()
            return real_stat(path, *args, **kwargs)

        report = self.dir / "report.csv"
        with scandir_faults(open_errors={unlistable: denied()}) as calls, \
                mock.patch.object(os, "stat", stat_fault):
            code, output = self.cli(good, unlistable, unstatable, report=report)
        self.assertIn(("open", unlistable), calls, "the fault never reached the search")
        self.assertTrue(stat_calls, "the stat fault never reached the search")
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [good / "a" / "disc.iso"])
        self.assertIn(f"DISCOVERY_ERROR: {unlistable}: could not list folder: [Errno 13]", output)
        self.assertIn(f"DISCOVERY_ERROR: {unstatable}: could not check path: [Errno 5]", output)
        self.assertEqual(sorted(r[0] for r in self.rows(report)[1:]),
                         ["DISCOVERY_ERROR", "DISCOVERY_ERROR", "PASS"])

    def test_overlapping_and_repeated_paths_check_each_manifest_once(self):
        c = self.dir / "c"
        self.masters(c, "a", "sub")
        for label, paths in (("parent then child", (c, c / "sub")), ("child then parent", (c / "sub", c)),
                             ("same path twice", (c, c))):
            with self.subTest(label):
                self.hashed.clear()
                code, output = self.cli(*paths)
                self.assertEqual(code, 0, output)
                self.assertEqual(sorted(self.hashed), [c / "a" / "disc.iso", c / "sub" / "disc.iso"])

    def test_a_given_manifest_link_keeps_its_own_folder(self):
        x, y = self.dir / "x", self.dir / "y"
        self.masters(y, ".")
        x.mkdir()
        (x / "disc.iso").write_bytes((y / "disc.iso").read_bytes())
        os.symlink(y / "disc_manifest.json", x / "disc_manifest.json")
        code, output = self.cli(x / "disc_manifest.json", y)
        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(self.hashed), [x / "disc.iso", y / "disc.iso"])

    def test_symlinked_folders_inside_a_search_are_listed_not_followed(self):
        outside, r = self.dir / "outside", self.dir / "r"
        self.masters(outside, "batch")
        self.masters(r, "own")
        link = r / "linked"
        os.symlink(outside / "batch", link)
        report = self.dir / "uncovered.csv"
        code, output = self.cli(r, report=report)
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [r / "own" / "disc.iso"])
        self.assertIn(f"EXCLUDED: {link}: symlinked folder not followed -> {outside / 'batch'}", output)
        self.assertEqual([row[0] for row in self.rows(report)[1:]], ["PASS", "EXCLUDED"])
        # Its target given as well: searched once there, and the link hides nothing
        self.hashed.clear()
        report = self.dir / "covered.csv"
        code, output = self.cli(r, outside / "batch", report=report)
        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(self.hashed), [outside / "batch" / "disc.iso", r / "own" / "disc.iso"])
        self.assertIn(f"Not searched (symlink, expected): {link}: symlinked folder not followed", output)
        self.assertIn(f"(its target was searched as {outside / 'batch'})", output)
        self.assertEqual([row[0] for row in self.rows(report)[1:]], ["PASS", "PASS"])

    def test_coverage_is_never_a_name_prefix(self):
        r, r_old = self.dir / "r", self.dir / "r-old"
        self.masters(r_old, "batch")
        self.masters(r, "own")
        os.symlink(r_old / "batch", r / "linked")
        code, output = self.cli(r)
        self.assertEqual(code, 1, output)
        self.assertIn(f"EXCLUDED: {r / 'linked'}", output)

    def test_dangling_links_fail_the_search_and_loops_do_not(self):
        r = self.dir / "dangling"
        self.masters(r, "own")
        os.symlink(self.dir / "unmounted" / "disk", r / "gone")
        code, output = self.cli(r)
        self.assertEqual(code, 1, output)
        self.assertIn(f"EXCLUDED: {r / 'gone'}: symlink target missing -> {self.dir / 'unmounted' / 'disk'}",
                      output)
        loopy = self.dir / "loopy"
        self.masters(loopy, "own")
        os.symlink(loopy / "elsewhere", loopy / "loop")
        report = self.dir / "loop.csv"
        self.hashed.clear()
        with looping(loopy / "loop") as calls:
            code, output = self.cli(loopy, report=report)
        self.assertIn(("loop", loopy / "loop"), calls, "the fault never reached the search")
        self.assertEqual(code, 0, output)
        self.assertIn(f"Not searched (symlink, expected): {loopy / 'loop'}: symlink loop", output)
        self.assertEqual([row[0] for row in self.rows(report)[1:]], ["PASS"])

    def test_a_link_to_an_ancestor_is_covered(self):
        r = self.dir / "r"
        self.masters(r, "own")
        os.symlink(r, r / "own" / "up")
        code, output = self.cli(r)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.hashed, [r / "own" / "disc.iso"])
        self.assertIn(f"(its target was searched as {r})", output)

    def test_symlinks_given_as_paths_are_followed(self):
        target = self.dir / "target"
        self.masters(target, ".")
        folder_link = self.dir / "folder-link"
        os.symlink(target, folder_link)
        code, output = self.cli(folder_link)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.hashed, [folder_link / "disc.iso"])
        os.symlink(target / "disc_manifest.json", target / "alias_manifest.json")
        self.hashed.clear()
        code, output = self.cli(target / "alias_manifest.json")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.hashed, [target / "disc.iso"])

    def test_given_paths_that_are_not_manifests_are_reported(self):
        good = self.dir / "good"
        self.masters(good, ".")
        fifo = self.dir / "pipe"
        os.mkfifo(fifo)
        code, output = self.cli(good, good / "disc.iso", fifo)
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [good / "disc.iso"])
        self.assertIn(f"DISCOVERY_ERROR: {good / 'disc.iso'}: not a manifest file (*_manifest.json)", output)
        self.assertIn(f"DISCOVERY_ERROR: {fifo}: not a file or folder", output)
        self.hashed.clear()
        code, output = self.cli(good / "disc_manifest.json")
        self.assertEqual(code, 0, output)

    def test_special_files_named_like_manifests_are_never_opened(self):
        r = self.dir / "r"
        self.masters(r, "own")
        os.mkfifo(r / "pipe_manifest.json")
        os.symlink(r / "pipe_manifest.json", r / "link_manifest.json")
        code, output = self.cli(r)
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [r / "own" / "disc.iso"])
        for name in ("pipe_manifest.json", "link_manifest.json"):
            self.assertIn(f"{r / name}: ERROR — not a regular file", output)

    def test_a_manifest_link_whose_target_cannot_be_inspected_is_never_read(self):
        r = self.dir / "r"
        self.masters(r, "own")
        alias = r / "alias_manifest.json"
        os.symlink(r / "own" / "disc_manifest.json", alias)
        (r / "disc.iso").write_bytes((r / "own" / "disc.iso").read_bytes())  # the alias's own ISO
        code, output = self.cli(r)  # control: a readable manifest link verifies
        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(self.hashed), [r / "disc.iso", r / "own" / "disc.iso"])
        self.hashed.clear()
        reads = []
        real_read = Path.read_bytes

        def read_bytes(path):
            reads.append(Path(path))
            return real_read(path)

        report = self.dir / "report.csv"
        with target_stat_fails({alias}, io_error(), "target") as calls, \
                mock.patch.object(Path, "read_bytes", read_bytes):
            code, output = self.cli(r, report=report)
        self.assertIn(("target", alias), calls, "the fault never reached the search")
        self.assertEqual(code, 1, output)
        self.assertNotIn(alias, reads, "the unclassified manifest link was read")
        self.assertEqual(self.hashed, [r / "own" / "disc.iso"])  # the healthy sibling is still verified
        self.assertIn(f"{alias}: ERROR — could not inspect the symlink's target: [Errno 5]", output)
        # T-137: its target stays uninspectable, so it is not shown to be
        # separate from the report, which is therefore not kept
        self.assertIn("; could not check whether it is the --csv report this run is writing "
                      "([Errno 5] Input/output error)", output)
        self.assertIn("PASS: verified", output)
        self.assertIn("Not keeping CSV report", output)
        self.assertFalse(os.path.lexists(report))
        # Control: an ordinary link with the same fault is a DISCOVERY_ERROR, as before
        folder_link = r / "folder-link"
        os.symlink(r / "own", folder_link)
        alias.unlink()
        with target_stat_fails({folder_link}, io_error(), "target") as calls:
            code, output = self.cli(r)
        self.assertIn(("target", folder_link), calls, "the fault never reached the search")
        self.assertEqual(code, 1, output)
        self.assertIn(f"DISCOVERY_ERROR: {folder_link}: could not inspect symlink target", output)

    # --- T-137: blocked manifest candidates and the --csv report ---

    @contextlib.contextmanager
    def target_stat_fails_once(self, link, error):
        """os.stat, following links, raises error the first time it is asked
        about link (discovery's own check); later checks see the real target"""
        real, fired = os.stat, []

        def stat(path, *args, **kwargs):
            if not fired and not isinstance(path, int) and Path(os.fsdecode(path)) == link \
                    and kwargs.get("follow_symlinks", True):
                fired.append(Path(os.fsdecode(path)))
                raise io_error()
            return real(path, *args, **kwargs)

        with mock.patch.object(os, "stat", stat):
            yield fired

    def run_blocked(self, r, report, faults):
        """cli() with faults applied and manifest reads recorded"""
        reads = []
        real_read = Path.read_bytes

        def read_bytes(path):
            reads.append(Path(path))
            return real_read(path)

        with faults as fired, mock.patch.object(Path, "read_bytes", read_bytes):
            code, output = self.cli(r, report=report)
        return code, output, reads, fired

    def blocked_link(self, label, target=None):
        """A manifest-named link (to the report path unless target is given)
        beside a healthy bundle"""
        r = self.dir / label
        self.masters(r, "own")
        report = r / "report.csv"
        link = r / "alias_manifest.json"
        os.symlink(target or report, link)  # to the report: dangling until it is reserved
        return r, link, report

    def assert_blocked_and_refused(self, r, link, report, code, output, reads):
        self.assertEqual(code, 1, output)
        self.assertNotIn(link, reads, "the blocked manifest link was read")
        self.assertEqual(self.hashed, [r / "own" / "disc.iso"])  # the healthy sibling only
        self.assertIn("PASS: verified", output)
        self.assertIn(f"{link}: ERROR — could not inspect the symlink's target: "
                      f"[Errno 5] Input/output error; not opened; ", output)
        self.assertIn("Not keeping CSV report", output)
        self.assertNotIn("CSV report written to", output)
        self.assertFalse(os.path.lexists(report), "a report that may be an input was kept")
        self.assertTrue(link.is_symlink())

    def test_a_blocked_manifest_link_proven_to_be_the_report_refuses_it(self):
        r, link, report = self.blocked_link("proven")
        code, output, reads, fired = self.run_blocked(r, report, self.target_stat_fails_once(link, io_error()))
        self.assertEqual(fired, [link], "the fault never reached the search")
        self.assert_blocked_and_refused(r, link, report, code, output, reads)
        self.assertIn("not opened; it is the --csv report this run is writing", output)
        self.assertIn("1 manifest(s) name it as their ISO or as themselves", output)
        self.assertNotIn("could not be checked whether", output)

    def test_a_blocked_manifest_link_still_uninspectable_refuses_the_report(self):
        r, link, report = self.blocked_link("uncertain")
        code, output, reads, calls = self.run_blocked(
            r, report, target_stat_fails({link}, io_error(), "target"))
        self.assertEqual(calls, [("target", link)] * 2, "discovery and the report check were not both reached")
        self.assert_blocked_and_refused(r, link, report, code, output, reads)
        self.assertIn("not opened; could not check whether it is the --csv report this run is "
                      "writing ([Errno 5] Input/output error)", output)
        self.assertIn("for 1 manifest(s) it could not be checked whether they or their ISO are "
                      "the report", output)
        self.assertNotIn("name it as their ISO or as themselves", output)

    def test_control_blocked_candidates_shown_separate_keep_the_report(self):
        # A link blocked once at discovery whose target is then shown to be
        # another manifest, and a real FIFO named like a manifest
        for label in ("separate-link", "fifo"):
            with self.subTest(label):
                if label == "fifo":
                    r = self.dir / label
                    self.masters(r, "own")
                    blocked, report = r / "pipe_manifest.json", r / "report.csv"
                    os.mkfifo(blocked)
                    faults, reason = contextlib.nullcontext([None]), "not a regular file"
                else:
                    r, blocked, report = self.blocked_link(label, target=self.dir / "elsewhere_manifest.json")
                    (self.dir / "elsewhere_manifest.json").write_text("{}")
                    faults, reason = self.target_stat_fails_once(blocked, io_error()), "[Errno 5]"
                code, output, reads, fired = self.run_blocked(r, report, faults)
                self.assertEqual(len(fired), 1, "the fault never reached the search")
                self.assertEqual(code, 1, output)
                self.assertNotIn(blocked, reads)
                self.assertEqual(self.hashed, [r / "own" / "disc.iso"])
                self.assertIn(f"CSV report written to: {report}", output)
                rows = {row[2]: row for row in self.rows(report)[1:]}
                self.assertEqual(rows[str(blocked)][0], "ERROR")
                self.assertIn(reason, rows[str(blocked)][9])
                self.assertNotIn("--csv report", rows[str(blocked)][9])
                self.assertEqual(rows[str(r / "own" / "disc_manifest.json")][0], "PASS")

    def test_a_manifest_link_to_a_fifo_whose_target_cannot_be_inspected_is_never_opened(self):
        r = self.dir / "r"
        self.masters(r, "own")
        os.mkfifo(r / "pipe")
        link = r / "link_manifest.json"
        os.symlink(r / "pipe", link)
        with target_stat_fails({link}, io_error(), "target") as calls:
            code, output = self.cli(r)
        self.assertIn(("target", link), calls, "the fault never reached the search")
        self.assertEqual(TRIPWIRE_OPENS[self._tripwire_before:], [],
                         "the product tried to open a FIFO (the tripwire stopped it)")
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [r / "own" / "disc.iso"])
        self.assertIn(f"{link}: ERROR — could not inspect the symlink's target: [Errno 5]", output)

    def test_system_and_recovery_folders_are_not_exempt(self):
        volume = self.dir / "volume"
        self.masters(volume, "JPC_AV/a", ".TemporaryItems/kept")
        names = (".Spotlight-V100", ".Trashes", "lost+found", "#recycle", "$RECYCLE.BIN")
        for name in names:
            (volume / name).mkdir()
        real_ismount = os.path.ismount

        def ismount(path):  # this folder stands in for a volume root
            return Path(os.fsdecode(path)) == volume or real_ismount(path)

        with scandir_faults(open_errors={volume / n: denied() for n in names}) as calls, \
                mock.patch.object(os.path, "ismount", ismount):
            code, output = self.cli(volume)
        for name in names:
            self.assertIn(("open", volume / name), calls, "the fault never reached the search")
        self.assertEqual(code, 1, output)
        self.assertEqual(sorted(self.hashed), [volume / ".TemporaryItems" / "kept" / "disc.iso",
                                               volume / "JPC_AV" / "a" / "disc.iso"])
        for name in names:
            self.assertIn(f"DISCOVERY_ERROR: {volume / name}: could not list folder: [Errno 13]", output)

    def test_empty_searches(self):
        clean = self.dir / "clean"
        clean.mkdir()
        (clean / "notes.txt").write_text("not a manifest")
        report = self.dir / "clean.csv"
        nothing = "No manifests (*_manifest.json) found under the given paths."
        self.assertEqual(self.exit_message(clean, report=report), nothing)
        self.assertFalse(os.path.lexists(report))
        loopy = self.dir / "loopy"
        loopy.mkdir()
        os.symlink(loopy / "elsewhere", loopy / "loop")
        with looping(loopy / "loop") as calls:
            self.assertEqual(self.exit_message(loopy, report=report), nothing)
        self.assertIn(("loop", loopy / "loop"), calls, "the fault never reached the search")
        self.assertFalse(os.path.lexists(report))
        partial = self.dir / "partial"
        (partial / "locked").mkdir(parents=True)
        with scandir_faults(open_errors={partial / "locked": denied()}) as calls:
            code, output = self.cli(partial, report=report)
        self.assertIn(("open", partial / "locked"), calls, "the fault never reached the search")
        self.assertEqual(code, 1, output)
        self.assertIn("No manifests found in the parts of the given paths that could be searched.", output)
        self.assertEqual([row[0] for row in self.rows(report)[1:]], ["DISCOVERY_ERROR"])

    def test_ctrl_c_during_the_search_stops_it(self):
        root = self.dir / "collection"
        self.masters(root, "a", "b")
        report = self.dir / "report.csv"
        with scandir_faults(open_errors={root / "b": KeyboardInterrupt()}) as calls:
            code, output = self.cli(root, report=report)
        self.assertIn(("open", root / "b"), calls, "the fault never reached the search")
        self.assertEqual(code, 130, output)
        self.assertEqual(self.hashed, [])
        self.assertFalse(os.path.lexists(report))

    def test_a_folder_loop_is_reported_not_followed(self):
        root = self.dir / "collection"
        self.masters(root, "a", "b/c")
        loop = {root / "b": {"c": dict(stat_result=os.stat(root))}}  # c claims to be root itself
        with scandir_faults(entries=loop) as calls:
            code, output = self.cli(root)
        self.assertIn(("listed", root / "b"), calls, "the fault never reached the search")
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [root / "a" / "disc.iso"])
        self.assertIn(f"DISCOVERY_ERROR: {root / 'b' / 'c'}: folder loop: the same folder as {root}", output)

    def test_a_real_unreadable_folder(self):
        root = self.dir / "collection"
        self.masters(root, "a", "b")
        locked = root / "b"
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        try:
            os.listdir(locked)
        except PermissionError:
            pass
        else:
            self.fail("the folder stayed listable (running as root?), so this check cannot run")
        code, output = self.cli(root)
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [root / "a" / "disc.iso"])
        self.assertIn(f"DISCOVERY_ERROR: {locked}: could not list folder: [Errno 13]", output)

    def test_library_calls(self):
        healthy = self.dir / "healthy"
        self.masters(healthy, "a")
        os.symlink(healthy / "a", healthy / "a-again")  # covered within the same search
        self.assertEqual(verify.find_preservation_manifests([healthy]),
                         [healthy / "a" / "disc_manifest.json"])
        broken = self.dir / "broken"
        self.masters(broken, "a", "b")
        with scandir_faults(open_errors={broken / "b": denied()}) as calls:
            with self.assertRaises(verify.DiscoveryIncomplete):
                verify.find_preservation_manifests([broken])
            issues = []
            self.assertEqual(verify.find_preservation_manifests([broken], issues),
                             [broken / "a" / "disc_manifest.json"])
        self.assertIn(("open", broken / "b"), calls, "the fault never reached the search")
        self.assertEqual([(i.status, i.path) for i in issues], [("DISCOVERY_ERROR", broken / "b")])

    # --- T-148: spellings, symlink chains and loops ---

    def dotdot_layout(self, base):
        """The audit's F1 shape: base/coll holds a corrupt master, and
        base/coll/link points at base/other/deep, so coll/link/.. is other
        (a healthy bundle of the same names) to the system, not coll"""
        other, coll = base / "other", base / "coll"
        self.master(folder=other)
        (other / "deep").mkdir()
        iso, _ = self.master(folder=coll)
        iso.write_bytes(b"CORRUPTED synthetic master")
        os.symlink(other / "deep", coll / "link")
        self.hashed.clear()
        return other, coll, coll / "link" / ".." / "disc_manifest.json"

    def test_a_symlink_and_dotdot_spelling_never_hides_another_bundle(self):
        # T-148 (N9): given as a file beside coll's own manifest, or beside
        # coll as a folder: coll's corrupt master is still checked
        for variant in ("file", "folder"):
            with self.subTest(variant):
                other, coll, weird = self.dotdot_layout(self.dir / variant)
                paths = ((weird, coll / "disc_manifest.json") if variant == "file"
                         else (weird, coll, other / "deep"))
                code, output = self.cli(*paths)
                self.assertEqual(code, 1, output)
                self.assertIn("FAIL: hash mismatch", output)
                self.assertEqual(sorted(self.hashed),
                                 sorted([coll / "link" / ".." / "disc.iso", coll / "disc.iso"]))

    def test_control_a_real_folder_and_dotdot_spelling_is_checked_once(self):
        coll = self.dir / "coll"
        self.masters(coll, ".")
        (coll / "sub").mkdir()
        code, output = self.cli(coll / "sub" / ".." / "disc_manifest.json", coll / "disc_manifest.json")
        self.assertEqual(code, 0, output)
        self.assertIn("Found 1 manifest(s) to examine", output)
        self.assertEqual(len(self.hashed), 1)

    def identity_case(self, variant, inject):
        """The review's shape: coll/link/.. given before coll, as a manifest
        file ("file") or as a folder ("folder"); with inject, inspecting
        coll/link fails (EIO) whenever verifyiso resolves a path through it"""
        other, coll, weird = self.dotdot_layout(self.dir / variant)
        paths = ((weird, coll / "disc_manifest.json") if variant == "file" else (weird.parent, coll))
        if not inject:
            return other, coll, weird, [], self.cli(*paths)
        with resolution_fails(coll / "link", io_error()) as fired:
            result = self.cli(*paths)
        return other, coll, weird, fired, result

    def test_a_manifest_whose_folder_cannot_be_resolved_never_hides_another_bundle(self):
        # T-148 repair: the given manifest's folder cannot be resolved, so it
        # is never taken for coll's manifest: it is a DISCOVERY_ERROR (not
        # checked), and coll's corrupt master is still checked and fails
        other, coll, weird, fired, (code, output) = self.identity_case("file", inject=True)
        self.assertIn("_physical", fired, "the fault never reached the given manifest's identity")
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [coll / "disc.iso"])
        self.assertIn("FAIL: hash mismatch", output)
        self.assertNotIn("PASS", output)
        self.assertIn(f"DISCOVERY_ERROR: {weird}: could not resolve its folder's real path: "
                      f"[Errno 5] Input/output error; not checked", output)

    def test_a_folder_that_cannot_be_resolved_never_hides_another_bundle(self):
        # The same with coll/link/.. given as a folder: never searched under a
        # guessed identity, and the link inside coll, whose target cannot be
        # resolved either, is never counted as searched
        other, coll, weird, fired, (code, output) = self.identity_case("folder", inject=True)
        self.assertIn("find_preservation_manifests", fired, "the fault never reached the given folder's identity")
        self.assertEqual(code, 1, output)
        self.assertEqual(self.hashed, [coll / "disc.iso"])
        self.assertIn("FAIL: hash mismatch", output)
        self.assertIn(f"DISCOVERY_ERROR: {weird.parent}: could not resolve its real path: "
                      f"[Errno 5] Input/output error; nothing was checked for it", output)
        self.assertIn(f"EXCLUDED: {coll / 'link'}: symlinked folder not followed -> {other / 'deep'} "
                      f"(whether its target was searched could not be checked: [Errno 5] "
                      f"Input/output error)", output)
        self.assertNotIn("Not searched (symlink, expected)", output)

    def test_control_the_same_spellings_without_a_fault_check_both_bundles(self):
        for variant in ("file", "folder"):
            with self.subTest(variant):
                other, coll, weird, _, (code, output) = self.identity_case(variant, inject=False)
                self.assertEqual(code, 1, output)
                self.assertEqual(sorted(self.hashed),
                                 sorted([coll / "link" / ".." / "disc.iso", coll / "disc.iso"]))
                self.assertEqual(output.count("PASS: verified"), 1, output)  # other's healthy master
                self.assertIn("FAIL: hash mismatch", output)
                self.assertNotIn("could not resolve", output)
                self.assertNotIn("DISCOVERY_ERROR", output)
                if variant == "folder":
                    self.assertIn(f"Not searched (symlink, expected): {coll / 'link'}", output)
                    self.assertNotIn("EXCLUDED", output)

    def chain_to(self, label, target, n=41):
        """root (a healthy bundle in root/own) whose entry root/l0 starts a
        chain of n + 1 symlinks to target: more than the system follows"""
        root = self.dir / label / "root"
        self.masters(root, "own")
        symlink_chain(root / "l0", self.dir / label / "chain", n, target)
        assert_beyond_the_system_limit(self, root / "l0")
        return root, self.dir / label / "chain" / "l1"

    def test_a_symlink_chain_too_long_for_the_system_is_judged_by_its_end(self):
        # T-148 (A2): the audit's F11 shape - 42 links, no loop, to a folder
        # holding a corrupt bundle. ELOOP is no proof of a loop: the chain
        # leads to a folder that was not searched, so it is EXCLUDED
        target = self.dir / "target"
        self.masters(target, "bad")
        (target / "bad" / "disc.iso").write_bytes(b"CORRUPTED synthetic master")
        root, first = self.chain_to("alone", target)
        code, output = self.cli(root)
        self.assertEqual(code, 1, output)
        self.assertNotIn("symlink loop", output)
        self.assertIn(f"EXCLUDED: {root / 'l0'}: symlinked folder not followed -> {first} "
                      f"(a chain of 42 symlink(s))", output)
        self.assertEqual(self.hashed, [root / "own" / "disc.iso"])
        # Its target given as well: searched there, so the corrupt master is
        # checked and fails, and the chain hides nothing
        self.hashed.clear()
        code, output = self.cli(root, target)
        self.assertEqual(code, 1, output)
        self.assertIn("FAIL: hash mismatch", output)
        self.assertIn(target / "bad" / "disc.iso", self.hashed)
        self.assertIn(f"Not searched (symlink, expected): {root / 'l0'}: symlinked folder not followed -> "
                      f"{first} (a chain of 42 symlink(s)) (its target was searched as {target})", output)
        self.assertNotIn("EXCLUDED", output)

    def test_control_a_long_chain_whose_target_was_searched_hides_nothing(self):
        target = self.dir / "target"
        self.masters(target, "good")
        root, _ = self.chain_to("covered", target)
        code, output = self.cli(root, target)
        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(self.hashed), sorted([root / "own" / "disc.iso", target / "good" / "disc.iso"]))
        self.assertIn(f"(its target was searched as {target})", output)

    def test_a_long_chain_to_nothing_is_a_missing_target_not_a_loop(self):
        root, first = self.chain_to("dangling", self.dir / "unmounted" / "disk")
        code, output = self.cli(root)
        self.assertEqual(code, 1, output)
        self.assertNotIn("symlink loop", output)
        self.assertIn(f"EXCLUDED: {root / 'l0'}: symlink target missing -> {first} "
                      f"(a chain of 42 symlink(s)); not followed", output)

    def test_a_chain_that_cannot_be_followed_to_its_end_is_a_discovery_error(self):
        # Longer than the verifier follows one link at a time (256): never
        # taken for a loop, even though its target is searched as well
        limit = getattr(verify, "SYMLINK_CHAIN_LIMIT", 256)
        target = self.dir / "target"
        self.masters(target, "good")
        root, first = self.chain_to("endless", target, n=299)
        self.assertGreater(300, limit)
        code, output = self.cli(root, target)
        self.assertEqual(code, 1, output)
        self.assertNotIn("symlink loop", output)
        self.assertIn(f"DISCOVERY_ERROR: {root / 'l0'}: could not inspect symlink target -> {first} "
                      f"(a chain of more than {limit} symlinks): [Errno {errno.ELOOP}] ", output)
        self.assertEqual(sorted(self.hashed), sorted([root / "own" / "disc.iso", target / "good" / "disc.iso"]))

    def test_a_chain_link_that_cannot_be_inspected_is_a_discovery_error(self):
        target = self.dir / "target"
        self.masters(target, "good")
        root, first = self.chain_to("eio", target)
        broken = self.dir / "eio" / "chain" / "l20"
        with lstat_fails(broken, io_error()) as fired:
            code, output = self.cli(root, target)
        self.assertEqual(code, 1, output)
        self.assertNotIn("symlink loop", output)
        self.assertIn(f"DISCOVERY_ERROR: {root / 'l0'}: could not inspect symlink target -> {first} "
                      f"(a chain of 20 symlink(s)): [Errno 5] Input/output error", output)
        self.assertEqual(fired, [broken], "the fault never fired")

    def test_a_real_symlink_loop_holds_nothing(self):
        # Control: a genuine cycle (loop -> via/back -> loop, via being a
        # link to hub outside the search) is still exempt
        r = self.dir / "loopy"
        self.masters(r, "own")
        hub, via = self.dir / "hub", self.dir / "via"
        hub.mkdir()
        os.symlink(hub, via)
        # Removing via breaks the cycle before cleanup, whose guard resolves
        # each removed path (Path.resolve raises on a loop in 3.9)
        self.addCleanup(os.unlink, via)
        os.symlink(via / "back", r / "loop")
        os.symlink(r / "loop", hub / "back")
        assert_beyond_the_system_limit(self, r / "loop")
        code, output = self.cli(r)
        self.assertEqual(code, 0, output)
        self.assertIn(f"Not searched (symlink, expected): {r / 'loop'}: symlink loop -> {via / 'back'}; "
                      f"not followed", output)
        self.assertEqual(self.hashed, [r / "own" / "disc.iso"])

    # --- T-149: a run with nothing verified ---

    def test_a_run_where_every_manifest_was_skipped_says_no_master_was_verified(self):
        folder = self.dir / "access"
        folder.mkdir()
        self.write_manifest("x_access_manifest.json",
                            {"conversion_metadata": {}, "conversion_status": {"overall_status": "success"}},
                            folder)
        code, output = self.cli(folder)
        self.assertEqual(code, 0, output)  # skips are not failures
        self.assertIn("No master was verified: all 1 manifest(s) were skipped", output)
        # Control: a master checked beside the skip - no such line
        self.master(folder=folder)
        code, output = self.cli(folder)
        self.assertEqual(code, 0, output)
        self.assertNotIn("No master was verified", output)


# --- the child that receives a real SIGINT (T-149) -----------------------------

def _send_sigint_once(state):
    if not state.get("sent"):
        state["sent"] = True
        os.kill(os.getpid(), signal.SIGINT)
        # Give the default handler every chance to raise right here
        for _ in range(1000):
            pass


def _child_main(scenario: str, folder: Path) -> int:
    """verifyiso's main() on folder with --csv folder/report.csv; a REAL
    SIGINT is sent to this process while the report is reserved
    ("reservation", at its fstat) or as the first master is hashed
    ("hashing"; "hashing_unlink_fails" also refuses removing the report)"""
    # Python's own Ctrl-C handler, even if this process inherited SIGINT ignored
    signal.signal(signal.SIGINT, signal.default_int_handler)
    report = folder / "report.csv"
    state = {}
    real_fstat, real_hash = os.fstat, verify.hash_file

    def fstat(fd):
        if scenario == "reservation" and os.path.lexists(report):
            _send_sigint_once(state)
        return real_fstat(fd)

    def hash_file(path, label=""):
        if scenario.startswith("hashing"):
            _send_sigint_once(state)
        return real_hash(path, label)

    argv = ["verifyiso.py", str(folder), "--csv", str(report)]
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(os, "fstat", fstat))
            stack.enter_context(mock.patch.object(verify, "hash_file", hash_file))
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            if scenario == "hashing_unlink_fails":
                stack.enter_context(unlink_denied(report))
            verify.main()
        return 0
    except SystemExit as e:
        if isinstance(e.code, int):
            return e.code
        print(e.code)
        return 1
    finally:
        print("SIGINT_SENT:", bool(state.get("sent")), flush=True)


def run_child(scenario: str, folder: Path):
    env = {"HOME": os.environ.get("HOME", str(folder)), "TMPDIR": tempfile.gettempdir(),
           "PATH": "/nonexistent", "OPTICAL_UNDER_TEST": str(support.ROOT)}
    proc = subprocess.run([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                           "--sigint-child", scenario, str(folder)],
                          env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


class InterruptTests(Base):
    """T-149: a REAL SIGINT, delivered inside a guarded child (this file
    re-run with --sigint-child, bounded by a timeout) while the --csv report
    is reserved or a master is hashed"""

    def child(self, scenario):
        code, output = run_child(scenario, self.dir)
        self.assertNotIn("VIOLATION", output)
        self.assertIn("SIGINT_SENT: True", output, "the scenario never delivered its SIGINT")
        self.assertNotIn("Traceback", output)
        return code, output

    def test_sigint_while_the_report_is_reserved_exits_130_and_names_the_path(self):
        self.master()
        report = self.dir / "report.csv"
        code, output = self.child("reservation")
        self.assertEqual(code, 130, output)
        self.assertIn(f"Interrupted by operator (Ctrl-C) while reserving the --csv report; anything now "
                      f"at {report} was left in place (if this run created it, it is empty)", output)
        self.assertNotIn("PASS", output)
        self.assertEqual(report.read_bytes(), b"")

    def test_sigint_while_hashing_removes_the_unfinished_report(self):
        self.master("a")
        self.master("b")
        report = self.dir / "report.csv"
        code, output = self.child("hashing")
        self.assertEqual(code, 130, output)
        self.assertIn("Interrupted by operator (Ctrl-C) — verification incomplete; the --csv report "
                      "was not completed", output)
        self.assertNotIn("Could not remove", output)
        self.assertFalse(os.path.lexists(report))

    def test_sigint_while_hashing_with_a_failed_removal_names_the_leftover(self):
        self.master("a")
        self.master("b")
        report = self.dir / "report.csv"
        code, output = self.child("hashing_unlink_fails")
        self.assertEqual(code, 130, output)
        self.assertIn(f"Could not remove the unfinished --csv report {report} ([Errno 13] Permission "
                      f"denied); it is the file this run created: remove it yourself", output)
        self.assertEqual(report.read_bytes(), b"")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--sigint-child":
        try:
            exit_code = _child_main(sys.argv[2], Path(sys.argv[3]))
        finally:
            if support.VIOLATIONS:
                print("VIOLATIONS:", support.VIOLATIONS, flush=True)
        sys.exit(exit_code)
    unittest.main()
