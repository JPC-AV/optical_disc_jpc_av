#!/usr/bin/env python3
"""Synthetic tests for verifyiso.py: CSV report safety, manifest families and
statuses, malformed items, and the eight fixture scenarios (T-122, T-125,
T-126).

Standard library only. Every file is synthetic and private (TMPDIR); the
guards in synthetic_support.py block child processes, sockets, devices and
writes outside the private directory. These tests check verifyiso's
decisions, files and exit codes - not real collection media.

Run:  python3 tests/test_verifyiso.py
OPTICAL_UNDER_TEST=/dir runs the same tests against another copy.
"""

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
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


if __name__ == "__main__":
    unittest.main()
