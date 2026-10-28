#!/usr/bin/env python3

"""
verifyiso.py - Collection Fixity Verification for ISO Preservation Masters

Re-hashes existing ISO files and compares them against the source hashes
recorded in their makeiso.py manifests — auditing the whole collection
without reloading a single disc.

Why this works even for ISOs made by older makeiso.py versions: the
manifest's `source_hash` was always computed by streaming the optical disc
itself during imaging, so it is a valid fixity baseline regardless of tool
version. Newer manifests also record `sha256_source_hash`, which is compared
too when present.

It never changes an existing file. --csv creates one NEW report file: the
path is reserved before anything is hashed, and an existing path (file,
directory, symlink, /dev/stdout) or a name ending in .iso or _manifest.json
is refused. The reservation is never read as a manifest or hashed as a
master: a manifest naming the report as its ISO, directly or through a
symlink, is an ERROR and the report is then not kept; so is one for which
that could not be checked (an inspection error other than "no such file"
on the manifest or its ISO name). The report is announced only while its
path still names the file this run wrote; these checks guard one
operator's run, and a change to the path after the last check is not
detected. An unfinished report is removed only while the run can show the
path still names the file it created; if that cannot be checked, the file
is left in place, and the run says so, as it does when a removal fails.
No sudo needed.

Usage:
  python3 verifyiso.py /path/to/collection [more/paths ...]
  python3 verifyiso.py /Volumes/Archive/JPC_AV --csv fixity_20261024.csv

Exit codes:
  0    every checked ISO matched its recorded hashes, and every folder under
       the given paths was searched (symlinked folders inside a search are
       never followed: one counts only when its target was searched through
       a given path; a symlink loop, a link that leads back to itself, holds
       nothing). A run in which every manifest was SKIPPED says that no
       master was verified; when the search was complete it still exits 0
  1    at least one mismatch, missing ISO, unverifiable manifest or error; part
       of a given path could not be searched (DISCOVERY_ERROR) or a symlinked
       folder or dangling symlink was not searched (EXCLUDED); no manifests
       were found; the --csv report was refused or could not be written; or
       a given path could not be found ("Path not found": missing, a dangling
       symlink, a path through a file, or one the system stops resolving
       with "too many levels of symbolic links"), which stops the whole run
       before anything is checked
  2    the command line is not valid (no path, an unknown option, --csv with
       no path); nothing is checked
  130  interrupted with Ctrl-C (an unfinished report is removed while it can
       be shown to be this run's file; if that cannot be checked, or the
       removal fails, the run names the file left in place)

Discovery: folders are listed one by one and every problem is reported, not
skipped - a folder that cannot be listed, a listing that stops partway, an
entry that cannot be inspected, a folder that disappears. Reachable folders
and other paths are still searched, but the run then exits 1 even if every
bundle found passed (a given path that cannot be found at all is different:
see exit code 1). No folder is skipped by name, so give the collection
folder rather than a volume root (whose system folders a normal user often
cannot list). A given path that is a symlink is followed. A symlink inside a
search, not named like a manifest, that the system will not follow (a loop,
or a chain of more links than it follows) is followed one link at a time: a
loop holds nothing, a longer chain is judged by where it ends, and one that
cannot be followed to its end is a DISCOVERY_ERROR. Such a link at the ISO
name, or named like a manifest, is an ERROR (not read, not hashed). A given
file must be a *_manifest.json; a FIFO,
device or socket named like a manifest is never opened (ERROR), and the ISO
a manifest names is opened only when it is a regular file (anything else
there is an ERROR, not opened).

Manifests are classified by content, not by file name: a makeiso.py
preservation record is checked; a makeiso-video.py access-copy record is
SKIPPED, as is a macOS AppleDouble (._*) metadata file. A preservation
record is UNVERIFIABLE (and fails the audit) when it cannot be trusted to
verify: missing or unrecognized run status, missing integrity record,
missing MD5 source hash, a blank, non-text or malformed hash (including a
declared SHA-256 with no SHA-256 hash), wrong field types, an ISO filename
that tries to escape the manifest's own directory or cannot name a file (an
embedded NUL, or a character the file system cannot encode), a dry-run
record lying next to an existing ISO (a dry run replaced that master's own
manifest), or markers of both record families at once. A *_manifest.json
that is neither family is UNVERIFIABLE too. These never silently pass.
Explicitly failed runs and dry runs with nothing at all at the ISO name
(not even a dangling symlink) are SKIPPED. MISSING_ISO means the ISO is
not there (no such file, or a dangling symlink); an ISO that is there but
cannot be inspected or read, or a
dry-run record whose ISO name cannot be checked, is an ERROR.
"""

import argparse
import csv
import datetime
import errno
import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# ANSI color only when stdout is a terminal and NO_COLOR is unset
ANSI_ENABLED = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
COLOR = {"cyan": "\033[96m", "green": "\033[92m", "yellow": "\033[93m",
         "red": "\033[91m", "off": "\033[0m"}


def colorize(color: str, text: str) -> str:
    if not ANSI_ENABLED:
        return text
    return f"{COLOR[color]}{text}{COLOR['off']}"


@dataclass
class CheckResult:
    manifest: Path
    status: str                      # PASS | FAIL | MISSING_ISO | UNVERIFIABLE | SKIPPED | ERROR
    iso: Optional[Path] = None
    size_bytes: int = 0
    run_id: Optional[str] = None
    md5_expected: Optional[str] = None
    md5_actual: Optional[str] = None
    sha256_expected: Optional[str] = None
    sha256_actual: Optional[str] = None
    detail: Optional[str] = None
    seconds: float = 0.0
    failed_algorithms: List[str] = field(default_factory=list)
    # The manifest or its ISO name is this run's --csv report ("proven"), or
    # could not be shown to be separate from it ("uncertain")
    report_alias: Optional[str] = None


# Top-level keys only makeiso.py preservation manifests carry (any one marks
# the record as preservation evidence; these win over a conversion marker)
PRESERVATION_KEYS = frozenset({
    "backup_metadata", "backup_status", "integrity_verification", "source_disc",
    "volume_information", "operations_performed", "timing_performance",
    "structural_analysis", "quality_assurance", "technical_details"})
# Top-level keys of makeiso-video.py access-copy records (never masters)
CONVERSION_KEYS = frozenset({"conversion_metadata", "conversion_status"})
# Every overall_status makeiso.py has written: verified, or explicitly no master
VERIFY_STATUSES = ("success", "success_with_warnings")
NO_MASTER_STATUSES = ("failed", "verification_failed")
KNOWN_STATUSES = VERIFY_STATUSES + NO_MASTER_STATUSES + ("dry_run",)
# macOS AppleDouble metadata files (._name) start with this magic number
APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"
HASH_LENGTHS = {"MD5": 32, "SHA-256": 64}


MANIFEST_SUFFIX = "_manifest.json"
# Discovery statuses: a part of the search that failed, and a symlink the
# search did not follow. Neither is a manifest result.
DISCOVERY_ERROR = "DISCOVERY_ERROR"
EXCLUDED = "EXCLUDED"
# A target that is not there: dangling, or a path component is a file
TARGET_MISSING = (errno.ENOENT, errno.ENOTDIR)
# Links followed one at a time, after os.stat gave up on a chain (ELOOP),
# before it is reported as one that could not be followed to its end
SYMLINK_CHAIN_LIMIT = 256


@dataclass
class DiscoveryIssue:
    path: Path                   # as reached from the given path (never resolved)
    status: str                  # DISCOVERY_ERROR | EXCLUDED | ERROR (a candidate never opened)
    detail: str
    exempt: bool = False         # hides nothing: a symlink loop, or a symlinked folder searched elsewhere


class DiscoveryIncomplete(Exception):
    """find_preservation_manifests was called without an issues list and the
    search was incomplete: the manifests it found are not the whole answer"""


def _reason(e: OSError) -> str:
    return f"[Errno {e.errno}] {e.strerror}" if e.errno and e.strerror else str(e)


def _target(path) -> Tuple[Optional[os.stat_result], Optional[OSError]]:
    """os.stat of what a symlink points to: (result, None), or (None, error)"""
    try:
        return os.stat(path), None
    except OSError as e:
        return None, e


def _entry(path) -> Tuple[Optional[os.stat_result], Optional[OSError]]:
    """os.lstat of the entry itself (a dangling symlink is there):
    (result, None), or (None, error)"""
    try:
        return os.lstat(path), None
    except OSError as e:
        return None, e


def _realpath(path) -> Tuple[Optional[str], Optional[OSError]]:
    """The real path of path, as the system reads it: (real path, None), or
    (None, error) when a part of it is missing or cannot be inspected or
    read. Never a guess: os.path.realpath takes a part it cannot inspect for
    a plain folder, so '<symlink>/..' would name the link's own folder, a
    different place. Symlinks followed are bounded by SYMLINK_CHAIN_LIMIT
    (ELOOP beyond: a loop, or a chain too long to follow)"""
    followed = 0

    def resolve(base: str, rest: str) -> str:
        nonlocal followed
        if os.path.isabs(rest):
            base = os.sep
        for name in rest.split(os.sep):
            if not name:
                continue
            if name in (os.curdir, os.pardir):
                # base holds no symlink, so its parent is its real parent
                if not stat.S_ISDIR(os.lstat(base).st_mode):
                    raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), base)
                if name == os.pardir:
                    base = os.path.dirname(base)
                continue
            here = os.path.join(base, name)
            if not stat.S_ISLNK(os.lstat(here).st_mode):
                base = here
                continue
            followed += 1
            if followed > SYMLINK_CHAIN_LIMIT:
                raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), here)
            base = resolve(base, os.readlink(here))
        return base

    try:
        path = os.fspath(path)
        return resolve(os.sep if os.path.isabs(path) else os.getcwd(), path), None
    except OSError as e:
        return None, e


def _physical(path) -> Tuple[Optional[str], Optional[OSError]]:
    """The real path of the folder that holds path, plus path's own name
    (never resolved): '<symlink>/..' names the link target's parent, as the
    system reads it, not the folder the link sits in. (None, error) when
    that folder cannot be resolved (see _realpath): never a guessed key"""
    path = os.fspath(path)
    folder, error = _realpath(os.path.dirname(path) or os.curdir)
    if error is not None:
        return None, error
    return os.path.join(folder, os.path.basename(path)), None


def _symlink_chain(path, error: OSError) -> Tuple[Optional[os.stat_result], Optional[OSError], int]:
    """os.stat gave up on path with error (ELOOP), which a symlink loop gives,
    but so does a chain of more links than the system follows. Follow the
    chain one link at a time to tell them apart, and return (result, error,
    links followed):
      (None, None, n)   a link was reached again (same folder, name and file):
                        a loop, which has no target and holds nothing;
      (result, None, n) the entry the chain ends at;
      (None, e, n)      what the chain leads to is missing or cannot be
                        inspected, the folder of a link in it cannot be
                        resolved (so a loop cannot be told apart), or the
                        chain is longer than SYMLINK_CHAIN_LIMIT (then the
                        original error)."""
    current, seen = os.fspath(path), set()
    while len(seen) <= SYMLINK_CHAIN_LIMIT:
        st, failed = _entry(current)
        if failed is not None:
            return None, failed, len(seen)
        if not stat.S_ISLNK(st.st_mode):
            return st, None, len(seen)
        here, failed = _physical(current)
        if failed is not None:
            return None, failed, len(seen)
        link = (here, st.st_dev, st.st_ino)
        if link in seen:
            return None, None, len(seen)
        seen.add(link)
        try:
            current = os.path.join(os.path.dirname(current), os.readlink(current))
        except OSError as e:
            return None, e, len(seen)
    return None, error, len(seen)


def find_preservation_manifests(roots: List[Path],
                                issues: Optional[List[DiscoveryIssue]] = None) -> List[Path]:
    """Find every *_manifest.json under the roots. Which family a manifest
    belongs to is decided by its content in check_manifest, never by file
    name, since a master's name can be anything.

    Folders are listed one at a time with os.scandir, and anything that stops
    the search from being complete is recorded in issues instead of being
    skipped: a path or folder that cannot be checked or listed (including a
    given path whose real path cannot be resolved: never searched under a
    guessed identity, which could hide another bundle), a listing that
    stops partway (the entries already read are still used), an entry that
    cannot be inspected or disappears, a folder loop. Symlinks inside a search
    are never followed; one to a folder, or a dangling one, is EXCLUDED, and
    counts as searched only if its target was listed through a given path.
    One that os.stat gives up on (ELOOP) is followed a link at a time: only a
    link reached again is an exempt loop; a longer chain is judged by where
    it ends, and one that cannot be followed to its end is a DISCOVERY_ERROR.
    A given symlink is followed. A candidate that is a FIFO, device or socket
    (or a link to one), or a link whose target cannot be checked, is returned
    but recorded as ERROR, never to be opened.
    Each manifest is returned once, however many given paths reach it.

    Without an issues list, the first issue that leaves the search incomplete
    raises DiscoveryIncomplete instead of being dropped."""
    found_issues: List[DiscoveryIssue] = []
    noted = set()
    manifests: List[Path] = []
    seen = set()       # real path of each manifest's own folder + its name
    listed = {}        # real path of each folder listed completely -> path as reached
    folder_links = []  # (issue, real path of the target): decided after every path

    def note(path, key, status, detail, exempt=False):
        if (key, status, detail) not in noted:
            noted.add((key, status, detail))
            issue = DiscoveryIssue(Path(path), status, detail, exempt)
            found_issues.append(issue)
            return issue
        return None

    def candidate(path: Path, key: str, mode: int, found: List[Tuple[Path, str]]):
        """A manifest-named entry; mode is its own (not followed) st_mode"""
        if stat.S_ISLNK(mode):
            st, error = _target(path)
            if error is not None:
                # Unclassified, so never opened: it could be a FIFO or device
                note(path, key, "ERROR", f"could not inspect the symlink's target: {_reason(error)}; "
                                         f"not opened")
                found.append((path, key))
                return
            mode = st.st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            note(path, key, "ERROR", "not a regular file (FIFO, device or socket); not opened")
        found.append((path, key))

    for root in roots:
        found: List[Tuple[Path, str]] = []
        root_key, unresolved = _realpath(root)
        if root_key is None:
            root_key = ("unresolved", os.fspath(root))  # equal to no real path
        try:
            root_st = os.stat(root)
        except FileNotFoundError:
            note(root, root_key, DISCOVERY_ERROR, "path disappeared before it could be searched")
            continue
        except OSError as e:
            note(root, root_key, DISCOVERY_ERROR, f"could not check path: {_reason(e)}")
            continue
        if stat.S_ISREG(root_st.st_mode):
            if root.name.endswith(MANIFEST_SUFFIX):
                key, failed = _physical(root)
                if failed is not None:
                    note(root, root_key, DISCOVERY_ERROR, f"could not resolve its folder's real path: "
                                                          f"{_reason(failed)}; not checked")
                else:
                    found.append((root, key))
            else:
                note(root, root_key, DISCOVERY_ERROR, "not a manifest file (*_manifest.json); nothing "
                                                      "was checked for it - give its folder or its manifest")
        elif not stat.S_ISDIR(root_st.st_mode):
            note(root, root_key, DISCOVERY_ERROR, "not a file or folder; nothing was checked for it")
        elif unresolved is not None:
            note(root, root_key, DISCOVERY_ERROR, f"could not resolve its real path: {_reason(unresolved)}; "
                                                  f"nothing was checked for it")
        else:
            stack = [(root, root_key, ((root_st.st_dev, root_st.st_ino),), (root,))]
            while stack:
                folder, key, chain, chain_paths = stack.pop()
                try:
                    listing = os.scandir(folder)
                except FileNotFoundError:
                    note(folder, key, DISCOVERY_ERROR, "folder disappeared during the search")
                    continue
                except OSError as e:
                    note(folder, key, DISCOVERY_ERROR, f"could not list folder: {_reason(e)}")
                    continue
                entries, complete = [], True
                with listing:
                    while True:
                        try:
                            entry = next(listing)
                        except StopIteration:
                            break
                        except OSError as e:
                            note(folder, key, DISCOVERY_ERROR, f"listing stopped partway: {_reason(e)}")
                            complete = False
                            break
                        entries.append(entry)  # used even if the listing fails later
                if complete:
                    listed.setdefault(key, folder)
                for entry in entries:
                    path, child_key = folder / entry.name, os.path.join(key, entry.name)
                    is_candidate = entry.name.endswith(MANIFEST_SUFFIX)
                    try:
                        if not is_candidate and entry.is_file(follow_symlinks=False):
                            continue  # an ordinary file: nothing to find in it
                    except OSError:
                        pass  # the stat below says what it is, or why it cannot be inspected
                    try:
                        mode_st = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        note(path, child_key, DISCOVERY_ERROR, "entry disappeared during the search")
                        continue
                    except OSError as e:
                        note(path, child_key, DISCOVERY_ERROR, f"could not inspect entry: {_reason(e)}")
                        continue
                    mode = mode_st.st_mode
                    if is_candidate:
                        candidate(path, child_key, mode, found)
                    if stat.S_ISDIR(mode):  # every real folder, whatever its name
                        identity = (mode_st.st_dev, mode_st.st_ino)
                        if identity in chain:
                            note(path, child_key, DISCOVERY_ERROR, f"folder loop: the same folder as "
                                                                   f"{chain_paths[chain.index(identity)]}")
                        else:
                            stack.append((path, child_key, chain + (identity,), chain_paths + (path,)))
                    elif stat.S_ISLNK(mode) and not is_candidate:  # a manifest-named link is a candidate
                        try:
                            text = os.readlink(path)
                        except OSError:
                            text = "?"
                        st, error = _target(path)
                        loop, via = False, ""
                        if error is not None and error.errno == errno.ELOOP:
                            # ELOOP alone is no proof of a loop: a chain of more
                            # links than the system follows gives it too
                            st, error, links = _symlink_chain(path, error)
                            loop = st is None and error is None
                            via = (f" (a chain of {links} symlink(s))" if links <= SYMLINK_CHAIN_LIMIT else
                                   f" (a chain of more than {SYMLINK_CHAIN_LIMIT} symlinks)")
                        if loop:
                            note(path, child_key, EXCLUDED, f"symlink loop -> {text}; not followed",
                                 exempt=True)
                        elif error is not None and error.errno in TARGET_MISSING:
                            note(path, child_key, EXCLUDED, f"symlink target missing -> {text}{via}; "
                                                            f"not followed")
                        elif error is not None:
                            note(path, child_key, DISCOVERY_ERROR,
                                 f"could not inspect symlink target -> {text}{via}: {_reason(error)}")
                        elif stat.S_ISDIR(st.st_mode):
                            issue = note(path, child_key, EXCLUDED,
                                         f"symlinked folder not followed -> {text}{via}")
                            if issue is not None:
                                target, failed = _realpath(child_key)
                                if failed is not None:  # stays EXCLUDED: never shown searched
                                    issue.detail += (f" (whether its target was searched could not be "
                                                     f"checked: {_reason(failed)})")
                                else:
                                    folder_links.append((issue, target))
        for path, key in sorted(found):
            if key not in seen:
                seen.add(key)
                manifests.append(path)

    # A symlinked folder counts as searched only once every given path has been
    # walked, and only if its target folder was itself listed completely
    for issue, target in folder_links:
        if target in listed:
            issue.exempt = True
            issue.detail += f" (its target was searched as {listed[target]})"
    if issues is None:
        incomplete = [i for i in found_issues if not i.exempt]
        if incomplete:
            first = incomplete[0]
            raise DiscoveryIncomplete(f"{first.path}: {first.status}: {first.detail}")
    else:
        issues.extend(found_issues)
    return manifests


def _hash_shape_ok(value: str, algorithm: str) -> bool:
    """A normalized hash of the right length, in hex"""
    return len(value) == HASH_LENGTHS[algorithm] and all(c in "0123456789abcdef" for c in value)


def _normalize_hash(value) -> Optional[str]:
    """Normalize a manifest hash for comparison: hashlib emits lowercase hex,
    so strip surrounding whitespace and lowercase the recorded value too. A
    non-string or blank value normalizes to None (treated as 'no usable hash')."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


class NotRegularFile(OSError):
    """What hash_file opened is not a regular file (it changed after it was
    inspected); nothing was read from it"""


def hash_file(path: Path, progress_label: str = "") -> tuple:
    """One read, both digests. Returns (md5, sha256). Only a regular file is
    read: it is opened without waiting (an ordinary open of a FIFO can block
    forever) and checked with fstat before any byte is read; anything else
    raises NotRegularFile."""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:  # the descriptor stays this function's (closefd=False): closed once, here
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NotRegularFile(f"not a regular file: {path}")
        os.set_blocking(fd, True)
        total = st.st_size
        done = 0
        last_tick = time.time()
        with open(fd, "rb", closefd=False) as f:
            for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                md5.update(chunk)
                sha256.update(chunk)
                done += len(chunk)
                if ANSI_ENABLED and total and time.time() - last_tick >= 0.2:
                    last_tick = time.time()
                    pct = done * 100 // total
                    sys.stdout.write(f"\r  hashing {_shown(progress_label)}: {pct}%   ")
                    sys.stdout.flush()
    finally:
        os.close(fd)
    if ANSI_ENABLED:
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()
    return md5.hexdigest(), sha256.hexdigest()


def _report_alias(path: Path, report: Optional[Tuple[Path, Tuple[int, int]]]
                  ) -> Tuple[Optional[str], Optional[str]]:
    """Whether path is the --csv report this run reserved. ("proven", None):
    the same path (its folder compared as the system reads it, so
    '<symlink>/..' is never taken for the link's own folder), or the same
    file reached through a symlink or another spelling. ("uncertain",
    reason): what path leads to, or the real folder of path or of the
    report, could not be inspected, so it is not shown to be separate.
    (None, None): shown to be another file, or nothing is there (no such
    file, or a path component is a file), so it cannot be the report."""
    if report is None:
        return None, None
    report_path, identity = report
    mine, error = _physical(path)
    theirs, report_error = _physical(report_path)
    for failed in (error, report_error):
        if failed is not None and failed.errno not in TARGET_MISSING:
            return "uncertain", _reason(failed)
    if mine is not None and mine == theirs:
        return "proven", None
    st, error = _target(path)
    if error is not None:
        if error.errno in TARGET_MISSING:
            return None, None
        return "uncertain", _reason(error)
    if (st.st_dev, st.st_ino) == identity:
        return "proven", None
    return None, None


def check_manifest(manifest_path: Path,
                   report: Optional[Tuple[Path, Tuple[int, int]]] = None) -> CheckResult:
    """Verify one ISO against its manifest. report is the --csv report this
    run reserved, (path, identity): it is never read as a manifest or hashed
    as a master (an empty reservation could match a record of an empty file)"""
    result = CheckResult(manifest=manifest_path, status="ERROR")
    alias, reason = _report_alias(manifest_path, report)
    if alias:
        result.report_alias = alias
        result.detail = ("this is the --csv report this run is writing, not a manifest; not read"
                         if alias == "proven" else
                         f"could not check whether this is the --csv report this run is "
                         f"writing ({reason}); not read")
        return result
    # Classified before it is read: only a regular file is ever opened, so a
    # FIFO or device handed in directly cannot block the run
    st, error = _target(manifest_path)
    if error is not None:
        result.detail = f"unreadable manifest: {error}"
        return result
    if not stat.S_ISREG(st.st_mode):
        result.detail = "not a regular file (folder, FIFO, device or socket); not opened"
        return result
    try:
        raw = manifest_path.read_bytes()
    except OSError as e:
        result.detail = f"unreadable manifest: {e}"
        return result
    if raw.startswith(APPLEDOUBLE_MAGIC):
        result.status = "SKIPPED"
        result.detail = "macOS AppleDouble metadata file, not a manifest"
        return result
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        result.detail = f"manifest is not valid UTF-8 text: {e}"
        return result
    except json.JSONDecodeError as e:
        result.detail = f"manifest is not valid JSON: {e}"
        return result
    if not isinstance(manifest, dict):
        result.detail = "manifest is not a JSON object"
        return result

    # Family by content. Any makeiso.py marker makes this preservation
    # evidence; a record carrying both families' markers is damaged or
    # conflicting and must never take any benign skip.
    if PRESERVATION_KEYS & manifest.keys() and CONVERSION_KEYS & manifest.keys():
        result.status = "UNVERIFIABLE"
        result.detail = "manifest carries both makeiso.py and makeiso-video.py markers"
        return result
    if not PRESERVATION_KEYS & manifest.keys():
        if CONVERSION_KEYS & manifest.keys():
            result.status = "SKIPPED"
            result.detail = "makeiso-video access-copy record, not a preservation master"
        else:
            result.status = "UNVERIFIABLE"
            result.detail = "unrecognized manifest: neither a makeiso.py nor a makeiso-video.py record"
        return result

    def unverifiable(detail: str) -> CheckResult:
        result.status = "UNVERIFIABLE"
        result.detail = detail
        return result

    metadata = manifest.get("backup_metadata", {})
    if not isinstance(metadata, dict):
        return unverifiable("manifest backup_metadata is not an object")
    run_id = metadata.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        return unverifiable("manifest run_id is not text")
    result.run_id = run_id
    dry_run_flag = metadata.get("dry_run", False)
    if not isinstance(dry_run_flag, bool):
        return unverifiable("manifest dry_run flag is not true/false")

    # Fail closed: a preservation manifest that has lost its status block is
    # corrupt/truncated, not implicitly a good run. Only an explicitly recorded
    # non-success run is a legitimate skip; an unrecognized status is not.
    backup_status = manifest.get("backup_status")
    overall = backup_status.get("overall_status") if isinstance(backup_status, dict) else None
    if not isinstance(overall, str):
        return unverifiable("manifest missing backup_status/overall_status — cannot trust this master")

    # The status must be one makeiso.py writes before any skip is considered,
    # dry-run flag or not (main-era dry runs recorded "success" + dry_run)
    if overall not in KNOWN_STATUSES:
        return unverifiable(f"unrecognized run status {overall!r} — cannot trust this master")
    dry_run = overall == "dry_run" or dry_run_flag
    if not dry_run and overall in NO_MASTER_STATUSES:
        result.status = "SKIPPED"
        result.detail = f"run status was '{overall}' — no verified master to check"
        return result

    # iso_filename is attacker-controllable manifest data; it must name a sibling
    # file, never traverse out of the manifest's own directory.
    output_files = manifest.get("output_files")
    if not isinstance(output_files, dict):
        return unverifiable("manifest output_files is missing or not an object")
    iso_name = output_files.get("iso_filename")
    if iso_name is None or iso_name == "":
        return unverifiable("manifest records no ISO filename")
    if not isinstance(iso_name, str):
        return unverifiable("manifest ISO filename is not text")
    if iso_name != Path(iso_name).name or os.path.isabs(iso_name) or iso_name == "..":
        return unverifiable(f"manifest ISO filename is not a plain sibling name: {iso_name!r}")
    # A name no file can have is a record defect, decided before any
    # filesystem call (which would raise ValueError on it)
    try:
        os.fsencode(iso_name)
        usable = "\x00" not in iso_name
    except UnicodeEncodeError:
        usable = False
    if not usable:
        return unverifiable(f"manifest ISO filename is not a usable file name: {iso_name!r}")
    result.iso = manifest_path.parent / iso_name
    alias, reason = _report_alias(result.iso, report)
    if alias:
        result.report_alias = alias
        result.detail = (f"the ISO name {iso_name} is the --csv report this run is writing, "
                         f"not a master; not checked" if alias == "proven" else
                         f"could not check whether the ISO name {iso_name} is the --csv report "
                         f"this run is writing ({reason}); not checked")
        return result

    if dry_run:
        # A dry run makes no master, so its record is a legitimate skip — but
        # only when nothing sits under the name it records (established, not
        # assumed from an error). Next to an existing ISO it means a dry run
        # replaced that master's own manifest.
        _, error = _entry(result.iso)
        if error is None:
            return unverifiable(f"dry-run record next to an existing ISO ({iso_name}) — "
                                f"this master's own manifest may have been replaced")
        if error.errno not in TARGET_MISSING:
            result.detail = (f"could not check whether an ISO sits beside this dry-run record "
                             f"({iso_name}): {_reason(error)}; a dry run may have replaced that "
                             f"master's own manifest")
            return result
        result.status = "SKIPPED"
        result.detail = "dry-run record — no master was made"
        return result

    integrity = manifest.get("integrity_verification")
    if not isinstance(integrity, dict):
        return unverifiable("manifest has no integrity_verification record")
    # Distinguish "key absent" (legitimately hash-free) from "key present but
    # blank/invalid" (corrupt or tampered manifest — must not pass silently).
    # Every makeiso.py version records the MD5 source hash (older ones only
    # that), so a missing one means the record is damaged. A SHA-256 key is
    # optional unless the record declares the algorithm.
    md5_present = "source_hash" in integrity
    sha_present = "sha256_source_hash" in integrity
    result.md5_expected = _normalize_hash(integrity.get("source_hash"))
    result.sha256_expected = _normalize_hash(integrity.get("sha256_source_hash"))
    if not md5_present:
        # A preservation manifest with no usable hash cannot be verified; this
        # is a finding, not a benign skip — it must affect the exit code.
        return unverifiable("manifest records no MD5 source hash")
    if not result.md5_expected or (sha_present and not result.sha256_expected):
        return unverifiable("manifest records a blank or non-text hash")
    if not _hash_shape_ok(result.md5_expected, "MD5") or (
            result.sha256_expected and not _hash_shape_ok(result.sha256_expected, "SHA-256")):
        return unverifiable("manifest records a malformed hash (wrong length or not hex)")
    algorithms = integrity.get("hash_algorithms", [])
    if not isinstance(algorithms, list):
        return unverifiable("manifest hash_algorithms is not a list")
    if "SHA-256" in algorithms and not sha_present:
        return unverifiable("manifest declares SHA-256 but records no SHA-256 source hash")

    # Inspected before it is opened, the same way on every Python version:
    # only "no such file" (or a dangling link) is MISSING_ISO; any other
    # inspection error (stale network handle, permissions, too many levels of
    # symlinks) is ERROR, and anything but a regular file is never opened. A file
    # vanishing mid-check is MISSING_ISO — one bad item never stops the batch
    started = time.time()
    st, error = _target(result.iso)
    if error is not None:
        if error.errno in TARGET_MISSING:
            result.status = "MISSING_ISO"
            result.detail = f"ISO not found next to manifest: {iso_name}"
        else:
            result.detail = f"could not inspect ISO {iso_name}: {_reason(error)}"
        return result
    if not stat.S_ISREG(st.st_mode):
        result.detail = f"ISO {iso_name} is not a regular file (folder, FIFO, device or socket); not opened"
        return result
    result.size_bytes = st.st_size
    try:
        result.md5_actual, result.sha256_actual = hash_file(result.iso, result.iso.name)
    except FileNotFoundError:
        result.status = "MISSING_ISO"
        result.detail = f"ISO disappeared during check: {iso_name}"
        return result
    except NotRegularFile:
        result.detail = f"ISO {iso_name} changed to a non-regular file during the check; not read"
        return result
    except OSError as e:
        result.detail = f"could not read ISO: {e}"
        return result
    result.seconds = time.time() - started

    if result.md5_expected and result.md5_actual != result.md5_expected:
        result.failed_algorithms.append("MD5")
    if result.sha256_expected and result.sha256_actual != result.sha256_expected:
        result.failed_algorithms.append("SHA-256")

    if result.failed_algorithms:
        result.status = "FAIL"
        result.detail = f"hash mismatch ({', '.join(result.failed_algorithms)})"
    else:
        result.status = "PASS"
        algorithms = "MD5+SHA-256" if result.sha256_expected else "MD5"
        result.detail = f"verified ({algorithms})"
    return result


class ReportError(Exception):
    """The --csv report path cannot be used; nothing has been written"""


def reserve_report(path: Path) -> Tuple[int, Tuple[int, int]]:
    """Claim the report path before anything is hashed, so a bad destination
    fails fast and no existing file can ever be replaced. The path is used
    exactly as given, never resolved: an existing entry of any kind — file,
    directory, symlink (even a dangling one), /dev/stdout — is refused, as is
    a name that looks like an ISO or a manifest. Returns (fd, identity) of
    the new, empty file this run now owns."""
    if path.name.lower().endswith((".iso", "_manifest.json")):
        raise ReportError(f"refusing --csv {path}: the name looks like an ISO or a manifest")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise ReportError(f"refusing --csv {path}: it already exists "
                          f"(the report must be a new file)") from None
    except OSError as e:
        raise ReportError(f"cannot create --csv report {path}: {e.strerror or e}") from None
    try:
        st = os.fstat(fd)
    except OSError as e:
        # Without the new file's identity nothing shows that the path still
        # names it, so the path is left alone, never unlinked
        os.close(fd)
        raise ReportError(f"cannot confirm that --csv {path} is the file this run created "
                          f"({e.strerror or e}); nothing was written, and whatever is at "
                          f"that path was left in place (remove it yourself only if it is "
                          f"an empty leftover)") from None
    except BaseException:
        os.close(fd)
        raise
    return fd, (st.st_dev, st.st_ino)


# How _report_lost's answer starts when ownership could not be established
REPORT_UNCHECKED = "could not be checked"


def _report_lost(path: Path, identity: Tuple[int, int]) -> Optional[str]:
    """None while the report path still names the file this run created;
    otherwise how that is known not to hold, or why it could not be checked
    (then starting with REPORT_UNCHECKED)"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "was removed"
    except OSError as e:
        return f"{REPORT_UNCHECKED} ({_reason(e)})"
    if (st.st_dev, st.st_ino) != identity:
        return "was replaced by another file"
    return None


def write_csv(results: List[CheckResult], out, discovery: Optional[List[DiscoveryIssue]] = None):
    """One row per manifest checked, then one per discovery item that left
    the search incomplete (its path leads the detail; no manifest, ISO or
    hashes; numeric columns stay numeric)"""
    writer = csv.writer(out)
    writer.writerow(["status", "iso", "manifest", "size_bytes", "run_id",
                     "md5_expected", "md5_actual",
                     "sha256_expected", "sha256_actual",
                     "detail", "hash_seconds", "checked_at"])
    checked_at = datetime.datetime.now().isoformat(timespec="seconds")
    for r in results:
        writer.writerow([r.status, str(r.iso) if r.iso else "", str(r.manifest),
                         r.size_bytes, r.run_id or "",
                         r.md5_expected or "", r.md5_actual or "",
                         r.sha256_expected or "", r.sha256_actual or "",
                         r.detail or "", f"{r.seconds:.1f}", checked_at])
    for issue in discovery or []:
        writer.writerow([issue.status, "", "", 0, "", "", "", "", "",
                         f"{issue.path}: {issue.detail}", "0.0", checked_at])


STATUS_COLOR = {"PASS": "green", "FAIL": "red", "MISSING_ISO": "red",
                "UNVERIFIABLE": "red", "ERROR": "red", "SKIPPED": "yellow",
                DISCOVERY_ERROR: "red", EXCLUDED: "red"}


def _shown(path) -> str:
    """A path for the console, with control characters escaped"""
    return "".join(c if c.isprintable() or c == " " else repr(c)[1:-1] for c in str(path))


def main():
    parser = argparse.ArgumentParser(
        description="Verify ISO preservation masters against their manifest hashes "
                    "(never changes an existing file)")
    parser.add_argument("paths", nargs="+", type=Path,
                        help="Collection folders (searched recursively; symlinked folders inside "
                             "them are listed, not searched through; a symlink named like a "
                             "manifest, or at a manifest's ISO name, is followed to the file) or "
                             "manifest files. Give the collection folder rather than a volume root")
    parser.add_argument("--csv", type=Path, metavar="PATH",
                        help="Write a CSV report of every check, and of every part of the search "
                             "that failed, to a NEW file; refused before anything is hashed if PATH "
                             "already exists or is named like an .iso or _manifest.json file, "
                             "and not kept if a manifest names it as its ISO (or that cannot "
                             "be ruled out)")
    args = parser.parse_args()

    roots = [p.expanduser() for p in args.paths]
    for root in roots:
        try:
            os.stat(root)
        except OSError as e:
            if e.errno in TARGET_MISSING + (errno.ELOOP,):
                sys.exit(f"Path not found: {root}")
            # anything else is recorded by the search itself

    report_path = args.csv.expanduser() if args.csv else None
    report_fd = None
    report_identity = None
    report_written = False
    leave_in_place = False  # a check that decided a message could not be made
    alias_note = ""         # a manifest names the report path as its ISO
    if report_path:
        try:
            report_fd, report_identity = reserve_report(report_path)
        except ReportError as e:
            print(colorize("red", str(e)))
            sys.exit(1)
        except KeyboardInterrupt:
            # Whatever was created has no proven identity, so it is never removed
            print(colorize("yellow", f"\nInterrupted by operator (Ctrl-C) while reserving the --csv "
                                     f"report; anything now at {_shown(report_path)} was left in place "
                                     f"(if this run created it, it is empty)"))
            sys.exit(130)

    try:
        report = (report_path, report_identity) if report_path else None
        issues: List[DiscoveryIssue] = []
        manifests = find_preservation_manifests(roots, issues)
        never_open = {i.path: i.detail for i in issues if i.status == "ERROR"}
        # Search items: those that left the search incomplete, and the
        # symlinks that hide nothing (a loop, or a folder searched elsewhere)
        incomplete = sorted((i for i in issues if i.status != "ERROR" and not i.exempt),
                            key=lambda i: str(i.path))
        expected = sorted((i for i in issues if i.status != "ERROR" and i.exempt),
                          key=lambda i: str(i.path))
        for issue in incomplete:
            print(colorize(STATUS_COLOR[issue.status],
                           f"  {issue.status}: {_shown(issue.path)}: {_shown(issue.detail)}"))
        for issue in expected:
            print(colorize("yellow", f"  Not searched (symlink, expected): {_shown(issue.path)}: "
                                     f"{_shown(issue.detail)}"))
        if not manifests and not incomplete:
            sys.exit("No manifests (*_manifest.json) found under the given paths.")

        results: List[CheckResult] = []
        started = time.time()
        if manifests:
            print(colorize("cyan", f"Found {len(manifests)} manifest(s) to examine"
                                   + (f" (search incomplete: {len(incomplete)} item(s) not searched)"
                                      if incomplete else "") + "\n"))
        else:
            print(colorize("red", "No manifests found in the parts of the given paths that could be searched."))
        for i, manifest_path in enumerate(manifests, 1):
            print(colorize("cyan", f"[{i}/{len(manifests)}] "
                                   f"{_shown(manifest_path.parent.name)}/{_shown(manifest_path.name)}"))
            # One bad item must never stop the batch: anything unexpected
            # becomes an ERROR for this manifest and the next one is checked
            if manifest_path in never_open:
                r = CheckResult(manifest=manifest_path, status="ERROR", detail=never_open[manifest_path])
                # Still never opened, but checked (metadata only) against the
                # report: a link to it, or one still not shown to be separate,
                # keeps the report from being kept
                r.report_alias, reason = _report_alias(manifest_path, report)
                if r.report_alias == "proven":
                    r.detail += "; it is the --csv report this run is writing"
                elif r.report_alias == "uncertain":
                    r.detail += (f"; could not check whether it is the --csv report this run "
                                 f"is writing ({reason})")
            else:
                try:
                    r = check_manifest(manifest_path, report)
                except Exception as e:
                    r = CheckResult(manifest=manifest_path, status="ERROR",
                                    detail=f"could not check this manifest: {type(e).__name__}: {e}")
            results.append(r)
            line = f"  {r.status}: {_shown(r.detail or '')}"
            if r.status == "PASS" and r.seconds:
                speed = (r.size_bytes / (1024 * 1024)) / r.seconds if r.seconds else 0
                line += f" — {r.size_bytes / (1024*1024):.0f} MB in {r.seconds:.1f}s ({speed:.0f} MB/s)"
            print(colorize(STATUS_COLOR.get(r.status, "yellow"), line))

        elapsed = time.time() - started
        counts = {}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1

        print()
        print(colorize("cyan", "=" * 60))
        print(colorize("cyan", f"Checked {len(results)} manifest(s) in {elapsed:.0f}s"))
        for status in ("PASS", "FAIL", "MISSING_ISO", "UNVERIFIABLE", "ERROR", "SKIPPED"):
            if counts.get(status):
                print(colorize(STATUS_COLOR[status], f"  {status}: {counts[status]}"))
        if results and all(r.status == "SKIPPED" for r in results):
            # Not a failure (skips never are), but nothing was hashed
            print(colorize("yellow", f"No master was verified: all {len(results)} manifest(s) "
                                     f"were skipped"))
        if incomplete:
            searched = {s: sum(1 for i in incomplete if i.status == s) for s in (DISCOVERY_ERROR, EXCLUDED)}
            print(colorize("red", "Search incomplete: " + "; ".join(
                f"{n} {s} item(s)" for s, n in searched.items() if n)
                + " - what they hold was not found or checked"))
        # UNVERIFIABLE is a finding, not a benign skip: a master that should carry a
        # checkable hash but doesn't must not let the audit exit 0.
        bad = [r for r in results if r.status in ("FAIL", "MISSING_ISO", "UNVERIFIABLE", "ERROR")]
        if bad or incomplete:
            print()
            print(colorize("red", "Items needing attention:"))
            for r in bad:
                print(colorize("red", f"  {_shown(r.iso or r.manifest)}: {r.status} — {_shown(r.detail)}"))
            for issue in incomplete:
                print(colorize("red", f"  {_shown(issue.path)}: {issue.status} — {_shown(issue.detail)}"))

        if report_path:
            aliased = [r for r in results if r.report_alias == "proven"]
            uncertain = [r for r in results if r.report_alias == "uncertain"]
            if aliased or uncertain:
                # Kept, the report could sit where a manifest expects its master
                # (or itself): a proven alias, or one that could not be ruled out
                reasons = []
                if aliased:
                    reasons.append(f"{len(aliased)} manifest(s) name it as their ISO or as "
                                   f"themselves (first: {_shown(aliased[0].manifest)})")
                if uncertain:
                    reasons.append(f"for {len(uncertain)} manifest(s) it could not be checked "
                                   f"whether they or their ISO are the report (first: "
                                   f"{_shown(uncertain[0].manifest)})")
                named = [r for r in aliased if r.iso is not None]
                if named:
                    alias_note = (f"; {_shown(named[0].manifest)} names that path as its ISO, so "
                                  f"until it is removed a check of that manifest reads it")
                print(colorize("red", f"\nNot keeping CSV report {_shown(report_path)}: "
                                      + "; ".join(reasons) + "; choose a --csv path that no "
                                      "manifest names, and resolve any inspection error above"))
                sys.exit(1)
            # Write only into the file this run reserved; if something else
            # has taken its place, leave that alone and report the failure.
            # A path whose check could not be made is never removed later,
            # so the message stays true
            lost = _report_lost(report_path, report_identity)
            if lost:
                leave_in_place = lost.startswith(REPORT_UNCHECKED)
                print(colorize("red", f"\nCould not write CSV report: {_shown(report_path)} {lost} "
                                      f"during the run; nothing was written to it and nothing "
                                      f"there was removed"))
                sys.exit(1)
            try:
                # The descriptor stays ours (closefd=False) until the report is
                # complete, so cleanup can still prove which file is ours
                with os.fdopen(report_fd, "w", encoding="utf-8", newline="", closefd=False) as out:
                    write_csv(results, out, incomplete)
                # Checked again before the descriptor is closed (so the file's
                # identity cannot be reused): a report the path no longer names
                # is never announced. A change after this check is not detected:
                # it guards one operator's run, not a hostile concurrent writer.
                lost = _report_lost(report_path, report_identity)
                if lost:
                    leave_in_place = lost.startswith(REPORT_UNCHECKED)
                    print(colorize("red", f"\nCould not save CSV report: {_shown(report_path)} {lost} "
                                          f"while the report was being written; nothing there "
                                          f"was removed, and it cannot be shown to hold this "
                                          f"run's report"))
                    sys.exit(1)
                fd, report_fd = report_fd, None
                os.close(fd)
            except Exception as e:
                # The report is part of the deliverable for a scheduled audit
                print(colorize("red", f"\nCould not write CSV report: {_shown(e)}"))
                sys.exit(1)
            report_written = True
            print(colorize("cyan", f"\nCSV report written to: {_shown(report_path)}"))

        sys.exit(1 if bad or incomplete or not results else 0)
    except KeyboardInterrupt:
        print(colorize("yellow", "\nInterrupted by operator (Ctrl-C)"
                                 + ("" if report_written else " — verification incomplete"
                                    + ("; the --csv report was not completed" if report_path else ""))))
        sys.exit(130)
    finally:
        # An unfinished report is removed only while its path still names the
        # file this run created (checked while the descriptor is still open,
        # so its identity cannot be reused), and never after a check that
        # decided a message could not be made. A removal that fails, or a
        # check that cannot be made here, is reported with the path
        if report_path and not report_written:
            if not leave_in_place:  # otherwise already said: nothing there was removed
                lost = _report_lost(report_path, report_identity)
                if lost is None:
                    try:
                        os.unlink(report_path)
                    except OSError as e:
                        print(colorize("red", f"\nCould not remove the unfinished --csv report "
                                              f"{_shown(report_path)} ({_reason(e)}); it is the file "
                                              f"this run created: remove it yourself{alias_note}"))
                elif lost.startswith(REPORT_UNCHECKED):
                    print(colorize("red", f"\nThe unfinished --csv report was left in place: whether "
                                          f"{_shown(report_path)} still names the file this run "
                                          f"created {lost}{alias_note}"))
            if report_fd is not None:
                try:
                    os.close(report_fd)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
