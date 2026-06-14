#!/usr/bin/env python3

"""
make_verify_fixtures.py - Manifest+ISO fixtures for testing verifyiso.py

verifyiso.py re-hashes an ISO and compares it against the hashes recorded in
its makeiso.py manifest. Its whole job is to DETECT corruption, so its
failure-detection paths are the ones that matter most. The video fixtures in
make_fixtures.py don't exercise verifyiso at all (no manifest+ISO pairs), so
this builder fills that gap.

verifyiso only ever reads the file's bytes, so these "ISOs" are just small byte
blobs — no real ISO filesystem, no hdiutil. That keeps this builder
cross-platform and CI-friendly.

Each scenario lives in its own subdirectory (one ISO + one manifest) so the
manifest's iso_filename can always be a plain sibling name:

  pass/            matching manifest + ISO                  -> PASS
  fail/            ISO tampered after hashing               -> FAIL
  missing/         manifest points at an absent ISO         -> MISSING_ISO
  blank_sha/       valid MD5, SHA-256 recorded as ""        -> UNVERIFIABLE
  md5_only/        valid MD5, no SHA-256 key (older tool)   -> PASS (MD5 only)
  traversal/       iso_filename "../escape.iso"             -> UNVERIFIABLE
  bad_status/      overall_status "failed"                  -> SKIPPED
  missing_status/  no backup_status block                   -> UNVERIFIABLE

Usage:
  python3 tests/make_verify_fixtures.py [--out DIR]
  python3 tests/make_verify_fixtures.py --check   # build, then assert statuses

The --check mode imports verifyiso and asserts each scenario resolves to its
expected status, exiting non-zero on any surprise — a self-contained test.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Expected verifyiso status for each scenario directory (used by --check)
EXPECTED = {
    "pass": "PASS",
    "fail": "FAIL",
    "missing": "MISSING_ISO",
    "blank_sha": "UNVERIFIABLE",
    "md5_only": "PASS",
    "traversal": "UNVERIFIABLE",
    "bad_status": "SKIPPED",
    "missing_status": "UNVERIFIABLE",
}


def _hashes(data: bytes):
    return hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()


def _manifest(iso_filename, *, md5=None, sha256=None, include_md5_key=True,
              include_sha_key=True, overall_status="success",
              include_status=True, include_integrity=True):
    """Build a minimal makeiso.py-shaped preservation manifest."""
    m = {
        "backup_metadata": {"run_id": "20260101T000000_test_disk9"},
        "output_files": {"iso_filename": iso_filename},
    }
    if include_status:
        m["backup_status"] = {"overall_status": overall_status}
    if include_integrity:
        iv = {}
        if include_md5_key:
            iv["source_hash"] = md5
        if include_sha_key:
            iv["sha256_source_hash"] = sha256
        m["integrity_verification"] = iv
    return m


def _write(scenario_dir: Path, iso_name, iso_bytes, manifest):
    scenario_dir.mkdir(parents=True, exist_ok=True)
    if iso_bytes is not None and iso_name is not None:
        (scenario_dir / iso_name).write_bytes(iso_bytes)
    (scenario_dir / f"{scenario_dir.name}_manifest.json").write_text(
        json.dumps(manifest, indent=2))


def build_fixtures(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    # Deterministic, distinct content per scenario (reproducible builds)
    data = b"JPCA verifyiso fixture payload\n" * 64

    # pass: hashes match the bytes on disk
    md5, sha = _hashes(data)
    _write(out_dir / "pass", "good.iso", data,
           _manifest("good.iso", md5=md5, sha256=sha))

    # fail: manifest records the ORIGINAL hashes, but the ISO is tampered
    md5, sha = _hashes(data)
    tampered = bytearray(data)
    tampered[0] ^= 0xFF  # flip one byte
    _write(out_dir / "fail", "bad.iso", bytes(tampered),
           _manifest("bad.iso", md5=md5, sha256=sha))

    # missing: manifest is valid but the ISO it names is absent
    md5, sha = _hashes(data)
    _write(out_dir / "missing", None, None,
           _manifest("gone.iso", md5=md5, sha256=sha))

    # blank_sha: SHA-256 present but empty — a corrupt/tampered manifest, not
    # a legitimately hash-free one. Must not silently verify on MD5 alone.
    md5, sha = _hashes(data)
    _write(out_dir / "blank_sha", "blank.iso", data,
           _manifest("blank.iso", md5=md5, sha256=""))

    # md5_only: older makeiso.py manifests carry no sha256 key at all — still a
    # valid (if weaker) check, should PASS and be labelled MD5.
    md5, sha = _hashes(data)
    _write(out_dir / "md5_only", "md5.iso", data,
           _manifest("md5.iso", md5=md5, include_sha_key=False))

    # traversal: iso_filename tries to escape the manifest's directory
    md5, sha = _hashes(data)
    _write(out_dir / "traversal", None, None,
           _manifest("../escape.iso", md5=md5, sha256=sha))

    # bad_status: an explicitly recorded failed run — a legitimate SKIP
    md5, sha = _hashes(data)
    _write(out_dir / "bad_status", "bs.iso", data,
           _manifest("bs.iso", md5=md5, sha256=sha, overall_status="failed"))

    # missing_status: backup_status block absent — a truncated/corrupt manifest
    # must not be assumed-good (fail closed).
    md5, sha = _hashes(data)
    _write(out_dir / "missing_status", "ms.iso", data,
           _manifest("ms.iso", md5=md5, sha256=sha, include_status=False))

    for name in EXPECTED:
        print(f"built fixture: {out_dir / name}")


def check_fixtures(out_dir: Path) -> int:
    """Build-then-verify: assert each scenario resolves to its expected status."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import verifyiso

    failures = 0
    for name, expected in EXPECTED.items():
        manifest_path = out_dir / name / f"{name}_manifest.json"
        result = verifyiso.check_manifest(manifest_path)
        ok = result.status == expected
        flag = "ok" if ok else "MISMATCH"
        print(f"  [{flag}] {name}: got {result.status}, expected {expected}"
              + ("" if ok else f"  (detail: {result.detail})"))
        if not ok:
            failures += 1
    if failures:
        print(f"\n{failures} scenario(s) did not match expected status.")
    else:
        print(f"\nAll {len(EXPECTED)} scenarios matched expected status.")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(
        description="Build manifest+ISO fixtures for verifyiso.py")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).parent / "fixtures" / "verify",
                        help="Output directory (default: tests/fixtures/verify)")
    parser.add_argument("--check", action="store_true",
                        help="After building, assert each scenario's verifyiso status")
    args = parser.parse_args()
    build_fixtures(args.out)
    if args.check:
        print()
        sys.exit(check_fixtures(args.out))
    print("done.")


if __name__ == "__main__":
    main()
