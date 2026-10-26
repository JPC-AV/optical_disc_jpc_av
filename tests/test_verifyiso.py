#!/usr/bin/env python3
"""Synthetic tests for verifyiso.py: CSV report safety, manifest families and
statuses, malformed items, and the eight fixture scenarios (T-122, T-125,
T-126); and discovery that reports every part of the search it could not
complete (T-134).

Standard library only. Every file is synthetic and private (TMPDIR); the
guards in synthetic_support.py block child processes, sockets, devices and
writes outside the private directory. These tests check verifyiso's
decisions, files and exit codes - not real collection media. Discovery faults
are injected through os.scandir and os.stat fakes that delegate everything
else, with DirEntry stand-ins; symlinks and FIFOs are real files in the
private directory, and a tripwire (installed before the product is loaded)
stops any open of a FIFO or device before it can block.

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
import stat
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


class ReportAliasInspectionTests(Base):
    """T-137 repair: when it cannot be inspected whether a manifest, or the
    ISO it names, is the --csv reservation (an error other than "no such
    file"), that input is an ERROR naming the path and the error, it is not
    read or hashed, healthy siblings are still checked, and the report is not
    kept. Each fault fires once, armed when check_manifest starts on that
    manifest, so discovery is untouched. Controls: absence that is
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


def looping(*links):
    """os.stat reports a symlink loop (ELOOP) for these existing links. A real
    loop link cannot be used: the guard's Path.resolve() raises on it in 3.9,
    so it could not even be removed afterwards"""
    return target_stat_fails(links, OSError(errno.ELOOP, "Too many levels of symbolic links"))


def denied():
    return PermissionError(errno.EACCES, "Permission denied")


def io_error():
    return OSError(errno.EIO, "Input/output error")


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


if __name__ == "__main__":
    unittest.main()
