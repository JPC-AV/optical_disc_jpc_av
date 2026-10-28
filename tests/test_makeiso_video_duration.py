#!/usr/bin/env python3
"""Synthetic tests for makeiso-video.py's duration check, review copies and
deferred promotion (T-071), and for ffmpeg's lifetime and truthful retention
records (T-129, T-130, T-132): ffmpeg is always stopped and reaped before its
output is kept, an ffmpeg that cannot be confirmed stopped stops the run and
batch and leaves the source attached, and an interrupted or repeated
retention records where the output really is. Also how a run ends (T-144,
T-145, T-146): a Ctrl-C anywhere in a run still leaves truthful records, the
canonical log and manifest are never left half-written, promotion holds
Ctrl-C, and a batch stops with a summary. And (T-150, T-155) that a check
which fails is never taken as absence - of the ISO, a VIDEO_TS file, an
output or a final name - that a VIDEO_TS without VOB files is a truthful
skip, that a gap in VOB numbering is a warning, and that ffmpeg gets no
terminal input and its shown command is quoted.

Standard library only, and synthetic throughout: placeholder IFO, VOB and MP4
bytes, a fake encoder and controlled ffprobe answers patched in at the
subprocess boundary, and a fake mount over a plain directory. Real ffmpeg,
ffprobe, hdiutil, diskutil, sudo, mounts, devices and network access are
blocked: tripwire executables are the only things on PATH and outgoing
connections are refused.

The shared guards in synthetic_support.py are installed before the script
is loaded. EncoderLifetimeTests send real SIGINTs to this process at the fake
boundaries (no child process); the script holds or handles each one, and the
tests stop rather than let one escape. The run-ending boundaries are tested
with a real SIGINT in a child instead: this interpreter re-running this file
with --sigint-child (the only child the guards allow), so that a Ctrl-C an
earlier version lets escape reaches only that child, never the test runner.
Each of those also runs with KeyboardInterrupt raised directly.

These tests check the script's decisions, files and records. They do not
establish media fidelity, FFmpeg compatibility or how real discs are authored.

Run from the repository root:
    python3 tests/test_makeiso_video_duration.py
MAKEISO_VIDEO_UNDER_TEST=/path/to/copy.py tests another copy of the script.
CentralRegressions uses only interfaces the earlier version also has.
"""

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
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent

# The shared guards first: nothing below, the script included, runs without them
_support_spec = importlib.util.spec_from_file_location("synthetic_support", HERE / "synthetic_support.py")
support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(support)
support.install()

SCRIPT = Path(os.environ.get("MAKEISO_VIDEO_UNDER_TEST") or HERE.parent / "makeiso-video.py")
SECTOR = 2048
STEM = "JPC_AV_00001"
STAMP = "20261023T120000"  # what the frozen clock gives run tokens


def load_script():
    spec = importlib.util.spec_from_file_location("makeiso_video_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mv = load_script()
# Before any test patches them: the real mount class, and the only way a
# scenario child is started
REAL_ISOMOUNT = mv.ISOMount
REAL_POPEN = subprocess.Popen

### === GUARDS: native tools and the network must never be reached ===

TRIPWIRE_DIR = Path(tempfile.mkdtemp(prefix="t071-tripwires-"))
TRIPWIRE_LOG = TRIPWIRE_DIR / "calls.log"
NATIVE_TOOLS = ("ffmpeg", "ffprobe", "hdiutil", "diskutil", "sudo", "mount", "umount", "drutil")


def setUpModule():
    for name in NATIVE_TOOLS:
        path = TRIPWIRE_DIR / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{TRIPWIRE_LOG}"\nexit 97\n')
        path.chmod(0o755)
    os.environ["PATH"] = str(TRIPWIRE_DIR)


def tearDownModule():
    calls = TRIPWIRE_LOG.read_text() if TRIPWIRE_LOG.exists() else ""
    shutil.rmtree(TRIPWIRE_DIR, ignore_errors=True)
    if calls:
        raise AssertionError(f"native tools were executed:\n{calls}")
    if support.VIOLATIONS or support.tripwire_calls():
        raise AssertionError(f"guarded operations attempted: {support.VIOLATIONS} "
                             f"{support.tripwire_calls()}")


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in a synthetic test")

### === SYNTHETIC IFO BYTES ===


def bcd_time(seconds, frames=0, fps=30):
    """DVD playback time: BCD hh mm ss ff, frame rate in the top bits of ff"""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)

    def bcd(value):
        return ((value // 10) << 4) | (value % 10)

    rate = {30: 0b11, 25: 0b01}[fps]
    return bytes([bcd(hours), bcd(minutes), bcd(secs), (rate << 6) | bcd(frames)])


class Cell:
    """One cell playback entry: time and its sector range in the title VOBs"""

    def __init__(self, seconds, first, last, frames=0, fps=30, time=None,
                 category=0, flags=0, still=0, command=0):
        self.time = time if time is not None else bcd_time(seconds, frames, fps)
        self.first, self.last = first, last
        self.category, self.flags, self.still, self.command = category, flags, still, command


def build_ifo(pgcs, srp_targets=None, programs=1, pgc_still=0, playback_mode=0,
              pgci_sector=1, magic=b"DVDVIDEO-VTS", program_map=None, positions=None,
              command_table=None, command_table_offset=None, program_map_offset=None,
              cell_table_offset=None, cell_position_offset=None,
              end_address_delta=0, truncate_to=None):
    """A minimal synthetic VTS_nn_0.IFO: VTSI_MAT in sector 0 and VTS_PGCI from
    `pgci_sector`. pgcs is a list of (pgc_time_bytes, [Cell, ...]); srp_targets
    gives the PGC index for each search pointer (default one per PGC).

    Each PGC is laid out as libdvdread reads one: the 0xEC-byte header, then
    the optional command table (bytes, e.g. from commands()), the program
    map (`programs` entry cells, default 1, 2, ...; padded to an even
    length), the cell playback table and the cell position table (default
    VOB ID 1, cell IDs 1, 2, ...). program_map / positions override the
    entries; the *_offset arguments override where the header points.
    Only the fields the parser reads are filled in; not a playable DVD."""
    srp_targets = list(range(len(pgcs))) if srp_targets is None else srp_targets
    map_size = programs + programs % 2
    command_size = len(command_table or b"")
    offsets, cursor = [], 8 + 8 * len(srp_targets)
    for _, cells in pgcs:
        offsets.append(cursor)
        cursor += 0xEC + command_size + map_size + 28 * len(cells)
    table = pgci_sector * SECTOR
    size = max(table + cursor, 0x100)
    data = bytearray(size + (-size) % SECTOR)
    data[0:len(magic)] = magic
    data[0xCC:0xD0] = pgci_sector.to_bytes(4, "big")
    data[table:table + 2] = len(srp_targets).to_bytes(2, "big")
    data[table + 4:table + 8] = (cursor - 1 + end_address_delta).to_bytes(4, "big")
    for index, target in enumerate(srp_targets):
        srp = table + 8 + 8 * index
        data[srp] = 0x80 | (index + 1)
        data[srp + 4:srp + 8] = offsets[target].to_bytes(4, "big")
    for (pgc_time, cells), offset in zip(pgcs, offsets):
        pgc = table + offset
        commands_at = 0xEC
        map_at = commands_at + command_size
        cells_at = map_at + map_size
        positions_at = cells_at + 24 * len(cells)
        data[pgc + commands_at:pgc + map_at] = command_table or b""
        data[pgc + 2] = programs
        data[pgc + 3] = len(cells)
        data[pgc + 4:pgc + 8] = pgc_time
        data[pgc + 0xA2] = pgc_still
        data[pgc + 0xA3] = playback_mode
        for field, default, override in ((0xE4, commands_at if command_table else 0,
                                          command_table_offset),
                                         (0xE6, map_at, program_map_offset),
                                         (0xE8, cells_at, cell_table_offset),
                                         (0xEA, positions_at, cell_position_offset)):
            value = default if override is None else override
            data[pgc + field:pgc + field + 2] = value.to_bytes(2, "big")
        entries = list(range(1, programs + 1)) if program_map is None else program_map
        data[pgc + map_at:pgc + map_at + len(entries)] = bytes(entries)
        for number, cell in enumerate(cells):
            entry = pgc + cells_at + 24 * number
            data[entry:entry + 4] = bytes([cell.category, cell.flags, cell.still, cell.command])
            data[entry + 4:entry + 8] = cell.time
            data[entry + 8:entry + 12] = cell.first.to_bytes(4, "big")
            data[entry + 16:entry + 20] = cell.last.to_bytes(4, "big")  # last VOBU start
            data[entry + 20:entry + 24] = cell.last.to_bytes(4, "big")
            vob_id, zero, cell_id = (1, 0, number + 1) if positions is None else positions[number]
            place = pgc + positions_at + 4 * number
            data[place:place + 4] = vob_id.to_bytes(2, "big") + bytes([zero, cell_id])
    return bytes(data if truncate_to is None else data[:truncate_to])


def commands(pre=0, post=0, cell=0):
    """A PGC command table: 8-byte header (counts, last byte), 8 bytes per command"""
    count = pre + post + cell
    header = b"".join(n.to_bytes(2, "big") for n in (pre, post, cell, 8 + 8 * count - 1))
    return header + bytes(8 * count)


def straight_cells(cell_seconds, cell_sectors):
    """Cells that play sectors 0..N-1 once each, in order"""
    cells, first = [], 0
    for seconds, sectors in zip(cell_seconds, cell_sectors):
        cells.append(Cell(seconds, first, first + sectors - 1))
        first += sectors
    return cells


def straight_ifo(cell_seconds, cell_sectors, **kwargs):
    """One program chain that plays straight through its cells"""
    return build_ifo([(bcd_time(sum(cell_seconds)), straight_cells(cell_seconds, cell_sectors))],
                     **kwargs)

### === FAKE TOOLS AND MOUNTS ===

TEMP_NAME = re.compile(r"\.encoding-[0-9a-f]{6}\.mp4$")


def probe_key(target):
    """A probe target's name, with a temp encode named after its final MP4"""
    return TEMP_NAME.sub(".mp4", Path(str(target)).name)


class InterruptingStream:
    def readline(self):
        raise KeyboardInterrupt


def send_sigint():
    """A real SIGINT to this process, as Ctrl-C delivers it"""
    os.kill(os.getpid(), signal.SIGINT)
    for _ in range(1000):  # give a default handler every chance to raise right here
        pass


class RaisingStream:
    def __init__(self, error):
        self.error = error

    def readline(self):
        raise self.error


class SignallingStream:
    """stderr of an ffmpeg that is running when Ctrl-C is pressed"""

    def readline(self):
        send_sigint()
        return "frame=1 time=00:00:01.00\n"


class ProgressStream:
    """stderr of an ffmpeg that runs to the end: a progress line, then EOF"""

    def __init__(self):
        self.lines = ["frame=1 time=00:00:01.00\n"]

    def readline(self):
        return self.lines.pop(0) if self.lines else ""


class LiveEncoder:
    """A running ffmpeg stand-in (encode spec live=True): alive until it is
    killed or its stderr ends. Spec keys: data (written at launch; None = no
    output yet), stop_output (appended to its output, by name, as it is
    killed: output that appears late), stderr_error (its stderr raises it),
    interrupt (its stderr raises KeyboardInterrupt), signalling (its stderr
    delivers a real SIGINT), sigint_on_kill (kill() delivers a real SIGINT),
    kill_error (kill() raises it), never_exits (it survives kill and every
    bounded wait). kill/wait/reap are recorded in tools.events."""
    pid = 4242

    def __init__(self, tools, cmd, spec):
        self.tools, self.spec, self.output = tools, spec, Path(cmd[-1])
        self.name = probe_key(self.output)
        self.alive, self.returncode = True, None
        data = spec.get("data", b"SYNTHETIC LIVE OUTPUT")
        if data is not None:
            self.output.write_bytes(data)
        if spec.get("stderr_error") is not None:
            self.stderr = RaisingStream(spec["stderr_error"])
        elif spec.get("interrupt"):
            self.stderr = InterruptingStream()
        elif spec.get("signalling"):
            self.stderr = SignallingStream()
        else:
            self.stderr = ProgressStream()

    def poll(self):
        if self.alive and isinstance(self.stderr, ProgressStream) and not self.stderr.lines:
            self.alive, self.returncode = False, self.spec.get("returncode", 0)
        return None if self.alive else self.returncode

    def kill(self):
        self.tools.events.append(("kill", self.name, None))
        if self.spec.get("sigint_on_kill"):
            send_sigint()
        if self.alive and self.spec.get("stop_output") is not None:
            with open(self.output, "ab") as f:
                f.write(self.spec["stop_output"])
        if not self.spec.get("never_exits"):
            self.alive, self.returncode = False, -9
        if self.spec.get("kill_error") is not None:
            raise self.spec["kill_error"]

    def wait(self, timeout=None):
        self.tools.events.append(("wait", self.name, timeout))
        if self.alive:
            if timeout is None:
                raise AssertionError("an unbounded wait() on an ffmpeg that is still running")
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        self.tools.events.append(("reaped", self.name, None))
        return self.returncode


class FakeEncoder:
    """Stands in for an ffmpeg process: writes the output it is told to"""

    def __init__(self, tools, cmd):
        output = Path(cmd[-1])
        self.tools, self.name = tools, probe_key(output)
        spec = tools.encodes.get(probe_key(output), {})
        self.returncode = spec.get("returncode", 0)
        data = spec.get("data", b"SYNTHETIC ACCESS COPY " + probe_key(output).encode())
        if data is not None:
            output.write_bytes(data)
        self.stderr = (InterruptingStream() if spec.get("interrupt")
                       else io.StringIO("frame=1 time=00:00:01.00\n"))

    def poll(self):
        return self.returncode

    def kill(self):
        self.tools.events.append(("kill", self.name, None))

    def wait(self, timeout=None):
        self.tools.events.append(("wait", self.name, timeout))
        return self.returncode


class FakeTools:
    """Controlled ffprobe/ffmpeg answers, patched in for subprocess.run/Popen.

    durations: file name -> seconds (None = probe fails; strings pass through)
    streams:   file name -> codec types (None = probe fails; "no-streams-key")
    encodes:   final MP4 name -> {returncode, data (None = no file), interrupt;
               live=True for a LiveEncoder and its keys; sigint_at_launch (a
               real SIGINT once ffmpeg has started, before Popen returns);
               launch_error (Popen raises it, after any sigint_at_launch)}"""

    def __init__(self):
        self.durations, self.streams, self.encodes = {}, {}, {}
        self.calls, self.unexpected = [], []
        self.events, self.processes, self.popen_kwargs, self.launched = [], [], [], []

    def run(self, cmd, capture_output=True, text=True, check=True, **kwargs):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        tool = cmd[0] if cmd else ""
        if tool == "uname":  # platform.processor() in the manifest writer
            return subprocess.CompletedProcess(cmd, 0, "synthetic\n", "")
        if tool in ("ffmpeg", "ffprobe") and cmd[1:] == ["-version"]:
            return subprocess.CompletedProcess(cmd, 0, f"{tool} version 0.0-synthetic\n", "")
        if tool == "ffprobe" and "-show_entries" in cmd:
            value = self.durations.get(probe_key(cmd[-1]))
            if value is None:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0, f"{value}\n", "")
        if tool == "ffprobe" and "-show_streams" in cmd:
            spec = self.streams.get(probe_key(cmd[-1]), ("video", "audio"))
            if spec is None:
                raise subprocess.CalledProcessError(1, cmd)
            payload = {"format": {}}
            if spec != "no-streams-key":
                payload["streams"] = [{"index": i, "codec_type": c} for i, c in enumerate(spec)]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        self.unexpected.append(cmd)
        raise AssertionError(f"command not allowed in a synthetic test: {cmd!r}")

    def popen(self, cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if not cmd or cmd[0] != "ffmpeg":
            self.unexpected.append(cmd)
            raise AssertionError(f"process not allowed in a synthetic test: {cmd!r}")
        self.popen_kwargs.append(kwargs)
        self.launched.append(probe_key(cmd[-1]))
        spec = self.encodes.get(probe_key(cmd[-1]), {})
        if spec.get("launch_error") is not None:
            if spec.get("sigint_at_launch"):
                send_sigint()
            raise spec["launch_error"]
        process = LiveEncoder(self, cmd, spec) if spec.get("live") else FakeEncoder(self, cmd)
        self.processes.append(process)
        if spec.get("sigint_at_launch"):
            send_sigint()  # ffmpeg has started; Popen has not returned its handle yet
        return process

    def duration_probes(self):
        return [probe_key(c[-1]) for c in self.calls if c[0] == "ffprobe" and "-show_entries" in c]


class _FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 23, 12, 0, 0)


FROZEN_CLOCK = types.SimpleNamespace(datetime=_FrozenDatetime)


def sequential_uuids():
    """uuid4 stand-in: the Nth call's hex starts with N as six hex digits"""
    counter = iter(range(1, 1 << 24))
    return lambda: uuid.UUID(int=next(counter) << 104)

### === SCENARIO BASE ===


class Scenario(unittest.TestCase):
    """A private directory, the fakes patched in, and helpers for one disc"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="t071-case-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tools = FakeTools()
        self.lines = []
        self.answers = []
        self.mounts = {}
        mounts = self.mounts
        self.mount_objects = []
        mount_objects = self.mount_objects
        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))

        class FakeMount:
            def __init__(self, iso_path):
                self.iso_path = Path(iso_path)
                self.detach_errors = []
                self.keep_attached = None
                self.mount_points, self.devices = [], ["/dev/disk42"]
                self.exited = None
                mount_objects.append(self)

            def __enter__(self):
                mount_point = mounts.get(self.iso_path)
                self.mount_points = [mount_point] if mount_point else []
                return mount_point

            def __exit__(self, *exc):
                self.exited = "kept attached" if self.keep_attached else "detached"
                return False

        for patcher in (
            mock.patch.object(subprocess, "run", self.tools.run),
            mock.patch.object(subprocess, "Popen", self.tools.popen),
            mock.patch.object(mv, "ISOMount", FakeMount),
            mock.patch.object(mv, "log", self._log),
            mock.patch.object(mv, "ANSI_ENABLED", False),
            mock.patch("builtins.input", self._input),
            mock.patch.object(socket.socket, "connect", _refuse_network),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._assert_no_escapes)

    def _log(self, message):
        self.lines.append(str(message))

    def _input(self, prompt=""):
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return self.answers.pop(0)

    def _assert_no_escapes(self):
        self.assertEqual(self.tools.unexpected, [], "a non-ffmpeg/ffprobe command was attempted")
        self.assertFalse(TRIPWIRE_LOG.exists(), "a native tool was executed")

    def freeze_tokens(self):
        """Predictable run tokens: frozen clock and numbered uuid4 calls"""
        for patcher in (mock.patch.object(mv, "datetime", FROZEN_CLOCK),
                        mock.patch.object(mv, "uuid4", sequential_uuids())):
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- the disc ---

    def make_disc(self, titlesets, stem=STEM, ifos=None):
        """titlesets: {vts: [sectors in each content VOB]}; ifos: {vts: bytes}"""
        isos = self.root / "isos"
        isos.mkdir(exist_ok=True)
        iso = isos / f"{stem}.iso"
        iso.write_bytes(b"SYNTHETIC ISO PLACEHOLDER")
        video_ts = self.root / "mounts" / stem / "VIDEO_TS"
        video_ts.mkdir(parents=True)
        for vts, sectors in titlesets.items():
            for index, count in enumerate(sectors, 1):
                with open(video_ts / f"VTS_{vts:02d}_{index}.VOB", "wb") as f:
                    f.truncate(count * SECTOR)
        for vts, data in (ifos or {}).items():
            (video_ts / f"VTS_{vts:02d}_0.IFO").write_bytes(data)
        self.mounts[iso] = video_ts.parent
        return types.SimpleNamespace(iso=iso, video_ts=video_ts, stem=stem,
                                     out=isos / f"access_{stem}")

    @staticmethod
    def mp4(disc, vts):
        return disc.out / (f"{disc.stem}.mp4" if vts == 1 else f"{disc.stem}_title{vts:02d}.mp4")

    @staticmethod
    def encoded_bytes(disc, vts):
        return b"SYNTHETIC ACCESS COPY " + Scenario.mp4(disc, vts).name.encode()

    def vob_durations(self, vts, *seconds):
        for index, value in enumerate(seconds, 1):
            self.tools.durations[f"VTS_{vts:02d}_{index}.VOB"] = value

    def output(self, disc, vts, seconds, streams=("video", "audio"), **encode):
        name = self.mp4(disc, vts).name
        self.tools.durations[name] = seconds
        self.tools.streams[name] = streams
        self.tools.encodes[name] = encode

    def seed_canonical(self, disc, vts_list=(1,)):
        """Earlier final MP4s, log and manifest; answers the overwrite prompt"""
        disc.out.mkdir(parents=True, exist_ok=True)
        seeded = {}
        paths = [self.mp4(disc, vts) for vts in vts_list] + [
            disc.out / f"{disc.stem}.mp4.log.txt", disc.out / f"{disc.stem}_access_manifest.json"]
        for path in paths:
            path.write_bytes(b"EARLIER CANONICAL " + path.name.encode())
            seeded[path] = path.read_bytes()
        self.answers.append("y")
        return seeded

    def assert_unchanged(self, seeded):
        for path, data in seeded.items():
            self.assertTrue(path.exists(), f"{path.name} disappeared")
            self.assertEqual(path.read_bytes(), data, f"{path.name} was changed")

    # --- running it ---

    def convert(self, disc):
        config = mv.ConversionConfig(iso_path=disc.iso, output_dir=disc.out, operator="T071")
        return mv.ISOToMP4Converter(config).convert()

    def main_exit(self, *args):
        with mock.patch.object(sys, "argv", ["makeiso-video.py", *args, "--operator", "T071"]):
            with self.assertRaises(SystemExit) as caught:
                mv.main()
        return caught.exception.code

    # --- what it left ---

    @staticmethod
    def glob(disc, pattern):
        return sorted(disc.out.glob(pattern)) if disc.out.exists() else []

    def review_copies(self, disc):
        return self.glob(disc, "*_unverified-*.mp4.unverified")

    def partials(self, disc):
        return self.glob(disc, "*_failed-*.mp4.partial")

    def temps(self, disc):
        return self.glob(disc, "*.encoding-*.mp4")

    def records(self, disc, kind):
        return (self.glob(disc, f"{disc.stem}_{kind}-*.log.txt"),
                self.glob(disc, f"{disc.stem}_{kind}-*_manifest.json"))

    def tokened_manifest(self, disc, kind):
        logs, manifests = self.records(disc, kind)
        self.assertEqual((len(logs), len(manifests)), (1, 1), f"{kind} records")
        return json.loads(manifests[0].read_text()), logs[0].read_text()

### === RUN-ENDING SCENARIOS: a real SIGINT in a child, or raised directly ===

CHILD_TIMEOUT = 120   # seconds one scenario child may take
CHILD_SCENARIOS = {}  # name -> function(case, interrupt) -> outcome (plain data)
REAL, RAISED = "real SIGINT, in a child", "KeyboardInterrupt raised directly"


def child_scenario(function):
    """Register a scenario. It builds its disc on the Scenario harness it is
    given, calls interrupt() at its boundary, and returns its outcome as
    plain data, with "fired" counting the faults it delivered. Its files
    stay in the harness's folder for the test to check."""
    CHILD_SCENARIOS[function.__name__] = function
    return function


def raise_interrupt():
    """KeyboardInterrupt raised directly, as no hold can defer it (one off
    the main thread, for instance)"""
    raise KeyboardInterrupt


def scenario_case(root):
    """A fresh Scenario harness outside the test runner, its files in root"""
    case = Scenario()
    case.setUp()
    case.root = root
    return case


def result_data(result):
    return {"success": result.success, "interrupted": result.interrupted,
            "skipped": result.skipped, "unverified": result.unverified,
            "aborted": result.aborted, "error": result.error_message,
            "warnings": list(result.warnings),
            "changed": [str(p) for p in result.canonical_files_changed],
            "titles": [{"status": t.status, "promoted": t.promoted_this_run, "error": t.error,
                        "retained_path": str(t.retained_path) if t.retained_path else None,
                        "retained_as": t.retained_as, "location_unverified": t.location_unverified,
                        "retention_error": t.retention_error} for t in result.titles]}


def capture(case, run):
    """Run a conversion or main() and return its outcome as plain data: a
    Ctrl-C (or anything else) that escapes is reported, never raised.
    handler_restored: the SIGINT handler is the one in place before (in a
    child, Python's own; in this process, whatever the runner has)"""
    before = signal.getsignal(signal.SIGINT)
    try:
        value = run()
    except BaseException as e:
        outcome = {"escaped": type(e).__name__, "escaped_detail": str(e)}
    else:
        outcome = result_data(value) if isinstance(value, mv.ConversionResult) else {"exit": value}
    outcome["handler_restored"] = signal.getsignal(signal.SIGINT) is before
    outcome["lines"] = list(case.lines)
    return outcome


def run_child(name, folder):
    """Run one scenario with a real SIGINT in a child process: this
    interpreter re-running this file with --sigint-child. Bounded; returns
    the child's outcome"""
    env = {"HOME": os.environ.get("HOME", str(folder)), "TMPDIR": tempfile.gettempdir(),
           "PATH": "/nonexistent", "OPTICAL_UNDER_TEST": str(support.ROOT),
           "MAKEISO_VIDEO_UNDER_TEST": str(SCRIPT)}
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if key in os.environ:
            env[key] = os.environ[key]
    with REAL_POPEN([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                     "--sigint-child", name, str(folder)], env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as child:
        try:
            output, _ = child.communicate(timeout=CHILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            child.kill()
            output, _ = child.communicate()
            raise AssertionError(f"the {name} child did not finish within {CHILD_TIMEOUT} s:\n{output}")
    found = [line[len("OUTCOME: "):] for line in output.splitlines() if line.startswith("OUTCOME: ")]
    if child.returncode != 0 or len(found) != 1:
        raise AssertionError(f"the {name} child failed (exit {child.returncode}):\n{output}")
    return json.loads(found[0])


def _child_main(name, folder):
    """In the child: Python's own Ctrl-C handling, the guards and tripwires,
    one scenario with a real SIGINT, its outcome on one line"""
    signal.signal(signal.SIGINT, signal.default_int_handler)
    setUpModule()
    case = scenario_case(folder)
    try:
        outcome = CHILD_SCENARIOS[name](case, send_sigint)
    finally:
        clean = case.doCleanups()
    outcome["clean"] = clean
    print("OUTCOME: " + json.dumps(outcome, default=str), flush=True)
    tearDownModule()  # fails the child if a native tool or guarded operation was attempted
    return 0


class ScenarioRunner:
    """Runs a registered scenario in either mode and checks that it ran
    cleanly and that its fault fired"""

    def run_scenario(self, name, mode, folder):
        folder.mkdir(parents=True)
        if mode == REAL:
            outcome = run_child(name, folder)
        else:
            case = scenario_case(folder)
            try:
                outcome = CHILD_SCENARIOS[name](case, raise_interrupt)
            finally:
                clean = case.doCleanups()
            outcome["clean"] = clean
        self.assertTrue(outcome.get("clean"), f"the {name} harness found a problem: {outcome}")
        self.assertTrue(outcome.get("fired"), f"the {name} fault never fired: {outcome}")
        return outcome

    @staticmethod
    def disc_at(folder, stem):
        """The disc a scenario built under folder (see Scenario.make_disc)"""
        return types.SimpleNamespace(iso=folder / "isos" / f"{stem}.iso",
                                     video_ts=folder / "mounts" / stem / "VIDEO_TS", stem=stem,
                                     out=folder / "isos" / f"access_{stem}")

### === CENTRAL REGRESSIONS (shared interfaces only) ===


class CentralRegressions(Scenario):
    """The reviewed observations, end to end through convert()/main().

    The three controls pass on the earlier version too; the rest failed on
    it for the reason in their name."""

    def test_control_complete_vob_probes_pass_and_promote(self):
        disc = self.make_disc({1: [4, 4]})
        self.vob_durations(1, 300.0, 300.0)
        self.output(disc, 1, 600.0)
        result = self.convert(disc)
        self.assertTrue(result.success)
        self.assertEqual([t.status for t in result.titles], ["completed"])
        self.assertEqual(self.mp4(disc, 1).read_bytes(), self.encoded_bytes(disc, 1))
        manifest = json.loads((disc.out / f"{STEM}_access_manifest.json").read_text())
        self.assertEqual(manifest["conversion_status"]["overall_status"], "success")
        self.assertEqual(self.review_copies(disc) + self.partials(disc) + self.temps(disc), [])

    def test_control_supported_ifo_passes_without_vob_probes(self):
        disc = self.make_disc({1: [3, 5]}, ifos={1: straight_ifo([200, 400], [3, 5])})
        self.vob_durations(1, None, None)  # unprobeable VOBs: the IFO decides
        self.output(disc, 1, 600.0)
        result = self.convert(disc)
        self.assertTrue(result.success)
        self.assertEqual([t.status for t in result.titles], ["completed"])
        self.assertEqual(self.mp4(disc, 1).read_bytes(), self.encoded_bytes(disc, 1))

    def test_control_known_truncation_fails_and_keeps_canonical(self):
        disc = self.make_disc({1: [4]})
        seeded = self.seed_canonical(disc)
        self.vob_durations(1, 600.0)
        self.output(disc, 1, 300.0)
        result = self.convert(disc)
        self.assertFalse(result.success)
        self.assertEqual([t.status for t in result.titles], ["failed"])
        self.assert_unchanged(seeded)
        partials = self.partials(disc)
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].read_bytes(), self.encoded_bytes(disc, 1))
        self.assertEqual(self.review_copies(disc), [])

    def test_unprobeable_output_duration_is_unverified_not_promoted(self):
        disc = self.make_disc({1: [4]})
        seeded = self.seed_canonical(disc)
        self.vob_durations(1, 600.0)
        self.output(disc, 1, None)
        result = self.convert(disc)
        self.assert_unchanged(seeded)
        self.assertEqual([t.status for t in result.titles], ["unverified"])
        self.assertFalse(result.success)
        copies = self.review_copies(disc)
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), self.encoded_bytes(disc, 1))
        self.assertEqual(self.partials(disc), [])

    def test_incomplete_expectation_with_short_output_is_unverified_not_failed(self):
        disc = self.make_disc({1: [4, 4]})
        seeded = self.seed_canonical(disc)
        self.vob_durations(1, 600.0, None)  # second VOB unprobeable
        self.output(disc, 1, 300.0)         # short only against the partial sum
        result = self.convert(disc)
        self.assertEqual([t.status for t in result.titles], ["unverified"])
        self.assertEqual(self.partials(disc), [])
        self.assertEqual(len(self.review_copies(disc)), 1)
        self.assert_unchanged(seeded)

    def test_repeated_pgc_reference_is_advisory_so_vob_probes_decide(self):
        cells = straight_cells([200, 400], [3, 5])
        ifo = build_ifo([(bcd_time(600), cells)], srp_targets=[0, 0])  # same chain twice
        disc = self.make_disc({1: [3, 5]}, ifos={1: ifo})
        self.vob_durations(1, 200.0, 400.0)
        self.output(disc, 1, 600.0)
        result = self.convert(disc)
        self.assertEqual([t.status for t in result.titles], ["completed"])
        self.assertTrue(result.success)

    def test_invalid_bcd_minutes_are_rejected_so_vob_probes_decide(self):
        cells = straight_cells([200, 400], [3, 5])
        ifo = build_ifo([(bytes([0x00, 0x99, 0x00, 0xC0]), cells)])  # 99 minutes: not a time
        disc = self.make_disc({1: [3, 5]}, ifos={1: ifo})
        self.vob_durations(1, 200.0, 400.0)
        self.output(disc, 1, 600.0)
        result = self.convert(disc)
        self.assertEqual([t.status for t in result.titles], ["completed"])
        self.assertTrue(result.success)

    def test_mixed_run_promotes_nothing_and_keeps_canonical_records(self):
        disc = self.make_disc({1: [4], 2: [4]})
        seeded = self.seed_canonical(disc, vts_list=(1, 2))
        self.vob_durations(1, 600.0)
        self.vob_durations(2, 600.0)
        self.output(disc, 1, 600.0)
        self.output(disc, 2, None)
        result = self.convert(disc)
        self.assert_unchanged(seeded)
        self.assertEqual([t.status for t in result.titles], ["held", "unverified"])
        self.assertEqual(len(self.review_copies(disc)), 2)
        logs, manifests = self.records(disc, "unverified")
        self.assertEqual((len(logs), len(manifests)), (1, 1))

    def test_failed_stream_probe_is_unverified(self):
        disc = self.make_disc({1: [4]})
        self.vob_durations(1, 600.0)
        self.output(disc, 1, 600.0, streams=None)
        result = self.convert(disc)
        self.assertEqual([t.status for t in result.titles], ["unverified"])
        self.assertFalse(self.mp4(disc, 1).exists())

    def test_confirmed_absence_of_video_fails(self):
        disc = self.make_disc({1: [4]})
        self.vob_durations(1, 600.0)
        self.output(disc, 1, 600.0, streams=("audio",))
        result = self.convert(disc)
        self.assertEqual([t.status for t in result.titles], ["failed"])
        self.assertFalse(self.mp4(disc, 1).exists())
        self.assertEqual(len(self.partials(disc)), 1)

    def test_one_second_floor_passes_a_short_title(self):
        disc = self.make_disc({1: [4]})
        self.vob_durations(1, 10.0)
        self.output(disc, 1, 9.2)  # 0.8 s short: inside the 1 s floor, outside 5%
        result = self.convert(disc)
        self.assertEqual([t.status for t in result.titles], ["completed"])

    def test_longer_than_tolerance_is_unverified(self):
        disc = self.make_disc({1: [4]})
        self.vob_durations(1, 600.0)
        self.output(disc, 1, 640.0)
        result = self.convert(disc)
        self.assertEqual([t.status for t in result.titles], ["unverified"])
        self.assertFalse(self.mp4(disc, 1).exists())

    def test_single_iso_unverified_exits_1(self):
        disc = self.make_disc({1: [4]})
        self.vob_durations(1, 600.0)
        self.output(disc, 1, None)
        self.assertEqual(self.main_exit("-i", str(disc.iso)), 1)

    def test_batch_counts_unverified_separately(self):
        good = self.make_disc({1: [4]}, stem="JPC_AV_00001")
        self.make_disc({1: [4]}, stem="JPC_AV_00002")
        self.vob_durations(1, 600.0)
        self.output(good, 1, 600.0)
        self.tools.durations["JPC_AV_00002.mp4"] = None
        results = mv.process_batch(self.root / "isos", self.root / "isos", "T071")
        self.assertEqual((results["success"], results.get("unverified", 0), results["failed"]),
                         (1, 1, 0))

    def test_batch_with_an_unverified_iso_exits_1(self):
        good = self.make_disc({1: [4]}, stem="JPC_AV_00001")
        self.make_disc({1: [4]}, stem="JPC_AV_00002")
        self.vob_durations(1, 600.0)
        self.output(good, 1, 600.0)
        self.tools.durations["JPC_AV_00002.mp4"] = None
        self.assertEqual(self.main_exit("--batch", str(self.root / "isos")), 1)

    def test_forced_partial_name_collision_never_clobbers(self):
        self.freeze_tokens()
        disc = self.make_disc({1: [4]})
        disc.out.mkdir(parents=True)
        # Whichever of the first uuid4 calls names the run's token, its
        # .partial name is already taken by someone else's file
        taken = {}
        for n in range(1, 4):
            path = disc.out / f"{STEM}_failed-{STAMP}-{n:06x}.mp4.partial"
            path.write_bytes(b"SOMEONE ELSE'S FILE " + str(n).encode())
            taken[path] = path.read_bytes()
        self.vob_durations(1, 600.0)
        self.output(disc, 1, 300.0)
        result = self.convert(disc)
        self.assert_unchanged(taken)
        kept = [p for p in self.partials(disc) if p not in taken]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), self.encoded_bytes(disc, 1))
        self.assertEqual([t.status for t in result.titles], ["failed"])

### === BUILD-ONLY UNIT TESTS ===


class IfoParserTests(unittest.TestCase):
    """parse_ifo_playback: the straight-through rule and strict validation"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="t071-ifo-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def parse(self, ifo_bytes, vob_sectors=(3, 5), vob_extra_bytes=0):
        ifo = self.root / "VTS_01_0.IFO"
        ifo.write_bytes(ifo_bytes)
        vobs = []
        for index, sectors in enumerate(vob_sectors, 1):
            vob = self.root / f"VTS_01_{index}.VOB"
            with open(vob, "wb") as f:
                f.truncate(sectors * SECTOR + (vob_extra_bytes if index == 1 else 0))
            vobs.append(vob)
        return mv.parse_ifo_playback(ifo, vobs)

    def assert_status(self, parsed, status, phrase=None, seconds="any"):
        self.assertEqual(parsed.status, status, parsed.reason)
        if phrase:
            self.assertIn(phrase, parsed.reason)
        if seconds is None:
            self.assertIsNone(parsed.seconds)
        elif seconds != "any":
            self.assertAlmostEqual(parsed.seconds, seconds, places=9)

    def poke(self, data, offset, value):
        data = bytearray(data)
        data[offset:offset + len(value)] = value
        return bytes(data)

    PGC = SECTOR + 16  # the only PGC in a one-SRP table

    # --- supported ---

    def test_straight_through_is_supported(self):
        self.assert_status(self.parse(straight_ifo([200, 400], [3, 5])), "supported", seconds=600)

    def test_frames_count_toward_the_time(self):
        cells = [Cell(240, 0, 2, frames=5), Cell(360, 3, 7, frames=10)]
        parsed = self.parse(build_ifo([(bcd_time(600, frames=15), cells)]))
        self.assert_status(parsed, "supported", seconds=600.5)

    def test_25_fps(self):
        cells = [Cell(200, 0, 2, fps=25), Cell(400, 3, 7, fps=25, frames=12)]
        parsed = self.parse(build_ifo([(bcd_time(600, frames=12, fps=25), cells)]))
        self.assert_status(parsed, "supported", seconds=600 + 12 / 25)

    def test_stc_discontinuity_and_seamless_play_allowed(self):
        cells = straight_cells([200, 400], [3, 5])
        cells[1].category = 0x02 | 0x08
        self.assert_status(self.parse(build_ifo([(bcd_time(600), cells)])), "supported")

    def test_cell_sum_within_a_frame_per_cell(self):
        cells = [Cell(200, 0, 2, frames=1), Cell(400, 3, 7, frames=1)]  # 2 frames over
        self.assert_status(self.parse(build_ifo([(bcd_time(600), cells)])), "supported")

    # --- unsupported layouts: advisory, with the time recorded ---

    def test_repeated_srp_reference_is_advisory(self):
        ifo = build_ifo([(bcd_time(600), straight_cells([200, 400], [3, 5]))], srp_targets=[0, 0])
        self.assert_status(self.parse(ifo), "advisory", "2 program chains")

    def test_several_program_chains_are_advisory(self):
        ifo = build_ifo([(bcd_time(200), [Cell(200, 0, 2)]), (bcd_time(400), [Cell(400, 3, 7)])])
        self.assert_status(self.parse(ifo), "advisory", "2 program chains", seconds=None)

    def cell_flag_case(self, phrase, **cell_fields):
        cells = straight_cells([200, 400], [3, 5])
        for key, value in cell_fields.items():
            setattr(cells[1], key, value)
        self.assert_status(self.parse(build_ifo([(bcd_time(600), cells)])), "advisory",
                           phrase, seconds=600)

    def test_angle_block_is_advisory(self):
        self.cell_flag_case("angle block", category=0x50)

    def test_seamless_angle_is_advisory(self):
        self.cell_flag_case("angle block", category=0x01)

    def test_interleaved_cell_is_advisory(self):
        self.cell_flag_case("interleaved", category=0x04)

    def test_cell_still_is_advisory(self):
        self.cell_flag_case("still frame", still=5)

    def test_vobu_still_is_advisory(self):
        self.cell_flag_case("still frame", flags=0x40)

    def test_cell_command_is_advisory(self):
        self.cell_flag_case("cell command", command=1)

    def test_pgc_still_is_advisory(self):
        ifo = straight_ifo([200, 400], [3, 5], pgc_still=10)
        self.assert_status(self.parse(ifo), "advisory", "still frame")

    def test_shuffle_playback_is_advisory(self):
        ifo = straight_ifo([200, 400], [3, 5], playback_mode=0x81)
        self.assert_status(self.parse(ifo), "advisory", "shuffle")

    def tiling_case(self, cells, phrase, vob_sectors=(3, 5)):
        ifo = build_ifo([(bcd_time(sum(200 for _ in cells)), cells)])
        self.assert_status(self.parse(ifo, vob_sectors=vob_sectors), "advisory", phrase)

    def test_gap_between_cells_is_advisory(self):
        self.tiling_case([Cell(200, 0, 2), Cell(200, 4, 7)], "straight through")

    def test_overlapping_cells_are_advisory(self):
        self.tiling_case([Cell(200, 0, 3), Cell(200, 3, 7)], "straight through")

    def test_out_of_order_cells_are_advisory(self):
        self.tiling_case([Cell(200, 3, 7), Cell(200, 0, 2)], "straight through")

    def test_repeated_cell_is_advisory(self):
        self.tiling_case([Cell(200, 0, 7), Cell(200, 0, 7)], "straight through", (8,))

    def test_not_starting_at_sector_zero_is_advisory(self):
        self.tiling_case([Cell(200, 1, 7)], "straight through", (8,))

    def test_not_reaching_the_end_is_advisory(self):
        self.tiling_case([Cell(200, 0, 5)], "cover 6 sectors", (3, 5))

    def test_running_past_the_vobs_is_advisory(self):
        self.tiling_case([Cell(200, 0, 9)], "cover 10 sectors", (3, 5))

    def test_vob_size_not_whole_sectors_is_advisory(self):
        parsed = self.parse(straight_ifo([200, 400], [3, 5]), vob_extra_bytes=100)
        self.assert_status(parsed, "advisory", "whole sectors")

    def test_pgc_time_not_matching_cells_is_advisory(self):
        cells = straight_cells([200, 400], [3, 5])
        self.assert_status(self.parse(build_ifo([(bcd_time(660), cells)])), "advisory",
                           "sum of its cells", seconds=660)

    # --- malformed: invalid, no time at all ---

    def test_missing_ifo(self):
        self.assertEqual(mv.parse_ifo_playback(None, []).status, "missing")
        self.assertEqual(mv.parse_ifo_playback(self.root / "nope.IFO", []).status, "missing")

    def test_wrong_magic(self):
        ifo = straight_ifo([200, 400], [3, 5], magic=b"DVDVIDEO-VMG")
        self.assert_status(self.parse(ifo), "invalid", seconds=None)

    def test_truncated_file(self):
        self.assert_status(self.parse(straight_ifo([200, 400], [3, 5], truncate_to=0x80)), "invalid")

    def test_table_pointer_zero(self):
        ifo = self.poke(straight_ifo([200, 400], [3, 5]), 0xCC, (0).to_bytes(4, "big"))
        self.assert_status(self.parse(ifo), "invalid", "pointer")

    def test_table_pointer_past_the_file(self):
        ifo = self.poke(straight_ifo([200, 400], [3, 5]), 0xCC, (9).to_bytes(4, "big"))
        self.assert_status(self.parse(ifo), "invalid", "pointer")

    def test_no_program_chains(self):
        ifo = self.poke(straight_ifo([200, 400], [3, 5]), SECTOR, (0).to_bytes(2, "big"))
        self.assert_status(self.parse(ifo), "invalid", "no program chains")

    def test_table_end_past_the_file(self):
        ifo = straight_ifo([200, 400], [3, 5], end_address_delta=5000)
        self.assert_status(self.parse(ifo), "invalid", "runs past")

    def test_pgc_offset_out_of_bounds(self):
        ifo = self.poke(straight_ifo([200, 400], [3, 5]), SECTOR + 12, (4000).to_bytes(4, "big"))
        self.assert_status(self.parse(ifo), "invalid", "out of bounds")

    def test_cell_table_offset_inside_the_header(self):
        ifo = straight_ifo([200, 400], [3, 5], cell_table_offset=0x10)
        self.assert_status(self.parse(ifo), "invalid", "cell table")

    def test_cell_table_past_the_table(self):
        ifo = self.poke(straight_ifo([200, 400], [3, 5]), self.PGC + 3, bytes([50]))
        self.assert_status(self.parse(ifo), "invalid", "cell table")

    def test_no_cells(self):
        ifo = self.poke(straight_ifo([200, 400], [3, 5]), self.PGC + 3, bytes([0]))
        self.assert_status(self.parse(ifo), "invalid", "no cells")

    def test_no_programs(self):
        self.assert_status(self.parse(straight_ifo([200, 400], [3, 5], programs=0)),
                           "invalid", "no programs")

    def test_inconsistent_cell_sectors(self):
        cells = [Cell(200, 5, 2), Cell(400, 3, 7)]
        self.assert_status(self.parse(build_ifo([(bcd_time(600), cells)])),
                           "invalid", "sector range")

    def bad_pgc_time(self, raw):
        cells = straight_cells([200, 400], [3, 5])
        self.assert_status(self.parse(build_ifo([(bytes(raw), cells)])), "invalid",
                           "valid BCD", seconds=None)

    def test_bcd_minutes_99(self):
        self.bad_pgc_time([0x00, 0x99, 0x00, 0xC0])

    def test_bcd_minutes_60(self):
        self.bad_pgc_time([0x00, 0x60, 0x00, 0xC0])

    def test_bcd_seconds_nibble_over_9(self):
        self.bad_pgc_time([0x00, 0x10, 0x5A, 0xC0])

    def test_bcd_hours_nibble_over_9(self):
        self.bad_pgc_time([0xA0, 0x10, 0x00, 0xC0])

    def test_frame_rate_code_00(self):
        self.bad_pgc_time([0x00, 0x10, 0x00, 0x00])

    def test_frame_rate_code_10(self):
        self.bad_pgc_time([0x00, 0x10, 0x00, 0x80])

    def test_frames_not_below_25(self):
        self.bad_pgc_time([0x00, 0x10, 0x00, 0x40 | 0x25])

    def test_frames_not_below_30(self):
        self.bad_pgc_time([0x00, 0x10, 0x00, 0xC0 | 0x30])

    def test_zero_time(self):
        cells = straight_cells([200, 400], [3, 5])
        self.assert_status(self.parse(build_ifo([(bcd_time(0), cells)])), "invalid", "zero")

    def test_invalid_cell_time(self):
        cells = straight_cells([200, 400], [3, 5])
        cells[0].time = bytes([0x00, 0x03, 0x7A, 0xC0])
        self.assert_status(self.parse(build_ifo([(bcd_time(600), cells)])), "invalid", "cell 1")

    def test_mixed_frame_rates(self):
        cells = [Cell(200, 0, 2, fps=25), Cell(400, 3, 7)]
        self.assert_status(self.parse(build_ifo([(bcd_time(600), cells)])),
                           "invalid", "different frame rate")


class ExpectationTests(Scenario):
    """expected_titleset_duration: which source is used, and when none is"""

    def vobs(self, *sectors):
        paths = []
        for index, count in enumerate(sectors, 1):
            path = self.root / f"VTS_01_{index}.VOB"
            with open(path, "wb") as f:
                f.truncate(count * SECTOR)
            paths.append(path)
        return paths

    def ifo(self, data):
        path = self.root / "VTS_01_0.IFO"
        path.write_bytes(data)
        return path

    def test_supported_ifo_is_used_without_probing(self):
        vobs = self.vobs(3, 5)
        e = mv.expected_titleset_duration(vobs, self.ifo(straight_ifo([200, 400], [3, 5])))
        self.assertEqual((e.seconds, e.source, e.ifo.status), (600, "ifo", "used"))
        self.assertIsNone(e.vob_probe_complete)
        self.assertEqual(self.tools.duration_probes(), [])

    def test_advisory_ifo_does_not_block_complete_vob_probes(self):
        vobs = self.vobs(3, 5)
        ifo = build_ifo([(bcd_time(600), straight_cells([200, 400], [3, 5]))], srp_targets=[0, 0])
        self.vob_durations(1, 250.0, 350.0)
        e = mv.expected_titleset_duration(vobs, self.ifo(ifo))
        self.assertEqual((e.seconds, e.source, e.ifo.status), (600.0, "vob_probe", "advisory"))

    def test_invalid_ifo_does_not_block_complete_vob_probes(self):
        vobs = self.vobs(3, 5)
        ifo = build_ifo([(bytes([0, 0x99, 0, 0xC0]), straight_cells([200, 400], [3, 5]))])
        self.vob_durations(1, 250.0, 350.0)
        e = mv.expected_titleset_duration(vobs, self.ifo(ifo))
        self.assertEqual((e.seconds, e.source, e.ifo.status), (600.0, "vob_probe", "invalid"))

    def test_advisory_ifo_and_incomplete_probes_give_no_expectation(self):
        vobs = self.vobs(3, 5)
        ifo = build_ifo([(bcd_time(600), straight_cells([200, 400], [3, 5]))], srp_targets=[0, 0])
        self.vob_durations(1, 250.0, None)
        e = mv.expected_titleset_duration(vobs, self.ifo(ifo))
        self.assertIsNone(e.seconds)
        self.assertIn("program chains", e.reason)
        self.assertIn("1 of 2 VOB duration(s) could not be measured", e.reason)
        self.assertEqual((e.vob_probe_seconds, e.vob_probe_complete), (250.0, False))

    def test_implausible_ifo_time_is_rejected_and_vob_probes_used(self):
        vobs = self.vobs(7680)  # 15 MiB: one second would be 125 Mbps
        self.vob_durations(1, 600.0)
        e = mv.expected_titleset_duration(vobs, self.ifo(straight_ifo([1], [7680])))
        self.assertEqual((e.seconds, e.source, e.ifo.status), (600.0, "vob_probe", "advisory"))
        self.assertIn("Mbps", e.ifo.reason)

    def test_implausible_vob_probes_give_no_expectation(self):
        vobs = self.vobs(7680)
        self.vob_durations(1, 1.0)
        e = mv.expected_titleset_duration(vobs, None)
        self.assertIsNone(e.seconds)
        self.assertIn("Mbps", e.reason)

    def test_non_finite_and_non_positive_vob_durations_are_unmeasured(self):
        for bad in ("inf", "nan", "-inf", 0.0, -5.0, "N/A"):
            with self.subTest(bad=bad):
                vobs = self.vobs(3, 5)
                self.vob_durations(1, 300.0, bad)
                e = mv.expected_titleset_duration(vobs, None)
                self.assertIsNone(e.seconds)
                self.assertFalse(e.vob_probe_complete)

    def test_single_unprobeable_vob_gives_no_expectation(self):
        vobs = self.vobs(4)
        self.vob_durations(1, None)
        e = mv.expected_titleset_duration(vobs, None)
        self.assertIsNone(e.seconds)
        self.assertEqual(e.ifo.status, "missing")

    def test_complete_vob_probes_without_ifo(self):
        vobs = self.vobs(3, 5)
        self.vob_durations(1, 250.5, 349.5)
        e = mv.expected_titleset_duration(vobs, None)
        self.assertEqual((e.seconds, e.source, e.vob_probe_complete), (600.0, "vob_probe", True))


class DurationCheckTests(unittest.TestCase):
    """duration_check: outcomes, tolerance boundaries and the 1 s floor"""

    def check(self, expected, output, video=1, reason="no usable expected duration (test)"):
        return mv.duration_check(expected, None if expected else reason, output, video)

    def test_boundaries_at_five_percent(self):
        for output, outcome in ((570.0, "passed"), (569.99, "failed"),
                                (630.0, "passed"), (630.01, "unverified"), (600.0, "passed")):
            with self.subTest(output=output):
                result, _, tolerance = self.check(600.0, output)
                self.assertEqual(result, outcome)
                self.assertEqual(tolerance, 30.0)

    def test_one_second_floor(self):
        for expected, output, outcome in ((10.0, 9.0, "passed"), (10.0, 8.99, "failed"),
                                          (10.0, 11.0, "passed"), (10.0, 11.01, "unverified"),
                                          (20.0, 19.0, "passed"), (20.0, 18.99, "failed")):
            with self.subTest(expected=expected, output=output):
                result, _, tolerance = self.check(expected, output)
                self.assertEqual(result, outcome)
                self.assertEqual(tolerance, 1.0)
        self.assertEqual(self.check(30.0, 28.5)[0], "passed")  # 5% = 1.5 s beats the floor
        self.assertEqual(self.check(30.0, 28.4)[0], "failed")

    def test_unmeasurable_output_is_unverified(self):
        for output in (None, float("inf"), float("nan"), 0.0, -1.0):
            with self.subTest(output=output):
                result, reason, _ = self.check(600.0, output)
                self.assertEqual(result, "unverified")
                self.assertIn("could not be measured", reason)

    def test_no_expectation_is_never_a_truncation(self):
        for output in (1.0, 300.0, 600.0):
            with self.subTest(output=output):
                result, reason, tolerance = self.check(None, output)
                self.assertEqual(result, "unverified")
                self.assertIn("(test)", reason)
                self.assertIsNone(tolerance)

    def test_failed_stream_probe_is_unverified(self):
        result, reason, _ = self.check(600.0, 600.0, video=None)
        self.assertEqual(result, "unverified")
        self.assertIn("stream probe failed", reason)

    def test_failed_stream_probe_with_known_truncation_fails(self):
        self.assertEqual(self.check(600.0, 300.0, video=None)[0], "failed")

    def test_confirmed_no_video_fails(self):
        for output in (600.0, None):
            with self.subTest(output=output):
                result, reason, _ = self.check(600.0, output, video=0)
                self.assertEqual(result, "failed")
                self.assertIn("no video", reason)


class ProbeTests(Scenario):
    """Probe helpers: failure is distinct from a successful empty answer"""

    def test_stream_probe_failure_is_none(self):
        self.tools.streams["x.mp4"] = None
        self.assertIsNone(mv.probe_stream_counts("x.mp4"))
        self.tools.streams["x.mp4"] = "no-streams-key"
        self.assertIsNone(mv.probe_stream_counts("x.mp4"))

    def test_stream_probe_success(self):
        self.tools.streams["x.mp4"] = ()
        self.assertEqual(mv.probe_stream_counts("x.mp4"), {"video": 0, "audio": 0, "subtitle": 0})
        self.tools.streams["x.mp4"] = ("video", "audio", "audio", "subtitle", "data")
        self.assertEqual(mv.probe_stream_counts("x.mp4"), {"video": 1, "audio": 2, "subtitle": 1})

    def test_non_finite_duration_is_none(self):
        for raw in ("inf", "nan", "-inf", "N/A"):
            with self.subTest(raw=raw):
                self.tools.durations["x.mp4"] = raw
                self.assertIsNone(mv.get_video_duration("x.mp4"))
        self.tools.durations["x.mp4"] = "600.5"
        self.assertEqual(mv.get_video_duration("x.mp4"), 600.5)


class OutcomeAndRecordTests(Scenario):
    """Per-title and run outcomes, records, batch counts and exits"""

    def test_unverified_run_records(self):
        disc = self.make_disc({1: [4]})
        seeded = self.seed_canonical(disc)
        self.vob_durations(1, 600.0)
        self.output(disc, 1, None)
        result = self.convert(disc)
        self.assertTrue(result.unverified)
        self.assertIsNone(result.error_message)
        self.assert_unchanged(seeded)
        manifest, log_text = self.tokened_manifest(disc, "unverified")
        self.assertEqual(self.records(disc, "failed"), ([], []))
        status = manifest["conversion_status"]
        self.assertEqual(status["overall_status"], "unverified")
        self.assertEqual(status["canonical_files_changed"], [])
        self.assertIn("could not be measured", status["unverified_reason"])
        title = manifest["titlesets"]["outputs"][0]
        self.assertEqual(title["status"], "unverified")
        self.assertEqual(title["duration_check"]["result"], "unverified")
        self.assertEqual(title["duration_check"]["expected_seconds"], 600.0)
        self.assertEqual(title["duration_check"]["expected_source"], "vob_probe")
        # The earlier canonical file exists but was not created by this run
        self.assertTrue(self.mp4(disc, 1).exists())
        self.assertEqual((title["promoted_this_run"], title["replaced_existing_canonical"]),
                         (False, False))
        self.assertEqual(title["final_intended_path"], str(self.mp4(disc, 1)))
        self.assertEqual(title["retained_as"], "review_copy")
        self.assertEqual(Path(title["retained_path"]), self.review_copies(disc)[0])
        self.assertIsNotNone(title["sha256_checksum"])
        self.assertIn("(UNVERIFIED)", log_text)
        self.assertIn("Status:           UNVERIFIED (nothing promoted)", log_text)
        self.assertIn("none promoted or changed by this run", log_text)
        self.assertIn("not a deliverable", log_text)
        self.assertEqual(self.temps(disc), [])

    def test_held_title_records_its_reason(self):
        disc = self.make_disc({1: [4], 2: [4]})
        self.vob_durations(1, 600.0)
        self.vob_durations(2, 600.0)
        self.output(disc, 1, 600.0)
        self.output(disc, 2, 640.0)  # longer than the tolerance
        result = self.convert(disc)
        held, unverified = result.titles
        self.assertEqual((held.status, held.duration_check, held.promoted_this_run),
                         ("held", "passed", False))
        self.assertIn(f"{STEM}_title02.mp4 is unverified", held.hold_reason)
        self.assertEqual(held.retained_as, "review_copy")
        self.assertEqual(held.retained_path.read_bytes(), self.encoded_bytes(disc, 1))
        self.assertEqual(unverified.duration_check, "unverified")
        self.assertIn("longer than expected", unverified.duration_check_reason)
        manifest, _ = self.tokened_manifest(disc, "unverified")
        outputs = manifest["titlesets"]["outputs"]
        self.assertEqual(outputs[0]["duration_check"]["result"], "passed")
        self.assertEqual(outputs[0]["status"], "held")
        self.assertFalse(self.mp4(disc, 1).exists())

    def test_failure_outranks_unverified(self):
        disc = self.make_disc({1: [4], 2: [4]})
        self.vob_durations(1, 600.0)
        self.vob_durations(2, 600.0)
        self.output(disc, 1, 300.0)  # truncated
        self.output(disc, 2, None)   # unverified
        result = self.convert(disc)
        self.assertFalse(result.success)
        self.assertFalse(result.unverified)
        self.assertEqual([t.status for t in result.titles], ["failed", "unverified"])
        manifest, _ = self.tokened_manifest(disc, "failed")
        self.assertEqual(manifest["conversion_status"]["overall_status"], "error")
        self.assertEqual(self.records(disc, "unverified"), ([], []))
        self.assertEqual(len(self.partials(disc)), 1)
        self.assertEqual(len(self.review_copies(disc)), 1)
        self.assertEqual(manifest["titlesets"]["outputs"][0]["partial_renamed_to"],
                         str(self.partials(disc)[0]))

    def test_interrupted_encode_records_every_planned_titleset(self):
        disc = self.make_disc({1: [4], 2: [4], 3: [4]})
        for vts in (1, 2, 3):
            self.vob_durations(vts, 600.0)
        self.output(disc, 1, 600.0)
        self.output(disc, 2, 600.0, interrupt=True)
        self.output(disc, 3, 600.0)
        result = self.convert(disc)
        self.assertTrue(result.interrupted)
        self.assertFalse(result.success)
        self.assertEqual([t.status for t in result.titles], ["held", "failed", "not_attempted"])
        self.assertIn("interrupted", result.titles[0].hold_reason)
        self.assertEqual(len(self.review_copies(disc)), 1)
        self.assertEqual(len(self.partials(disc)), 1)
        manifest, _ = self.tokened_manifest(disc, "failed")
        self.assertEqual(manifest["titlesets"]["count"], 3)
        self.assertTrue(manifest["conversion_status"]["interrupted"])
        self.assertEqual(self.temps(disc), [])

    def test_empty_and_missing_outputs_fail(self):
        for data, phrase in ((b"", "empty"), (None, "not created")):
            with self.subTest(data=data):
                disc = self.make_disc({1: [4]}, stem=f"JPC_AV_0000{2 if data is None else 3}")
                self.vob_durations(1, 600.0)
                self.output(disc, 1, 600.0, data=data)
                result = self.convert(disc)
                self.assertEqual(result.titles[0].status, "failed")
                self.assertIn(phrase, result.titles[0].error)
                self.assertFalse(self.mp4(disc, 1).exists())

    def test_ffmpeg_error_fails(self):
        disc = self.make_disc({1: [4]})
        self.vob_durations(1, 600.0)
        self.output(disc, 1, 600.0, returncode=1)
        result = self.convert(disc)
        self.assertEqual(result.titles[0].status, "failed")
        self.assertIn("return code 1", result.titles[0].error)
        self.assertEqual(len(self.partials(disc)), 1)

    def test_non_finite_output_duration_is_unverified_and_recorded(self):
        disc = self.make_disc({1: [4]})
        self.vob_durations(1, 600.0)
        self.output(disc, 1, "inf")
        result = self.convert(disc)
        self.assertEqual(result.titles[0].status, "unverified")
        manifest, _ = self.tokened_manifest(disc, "unverified")
        self.assertIsNone(manifest["titlesets"]["outputs"][0]["duration_seconds"])

    def test_exits(self):
        good = self.make_disc({1: [4]}, stem="JPC_AV_00001")
        bad = self.make_disc({1: [4]}, stem="JPC_AV_00002")
        self.vob_durations(1, 600.0)
        self.output(good, 1, 600.0)
        self.output(bad, 1, 300.0)
        self.assertEqual(self.main_exit("-i", str(good.iso)), 0)
        self.assertEqual(self.main_exit("-i", str(bad.iso)), 1)

    def test_batch_counts_with_all_three_outcomes(self):
        discs = [self.make_disc({1: [4]}, stem=f"JPC_AV_0000{n}") for n in (1, 2, 3)]
        self.vob_durations(1, 600.0)
        for disc, seconds in zip(discs, (600.0, None, 300.0)):
            self.output(disc, 1, seconds)
        results = mv.process_batch(self.root / "isos", self.root / "isos", "T071")
        self.assertEqual((results["success"], results["unverified"], results["failed"]), (1, 1, 1))
        self.assertEqual([d["unverified"] for d in results["details"]], [False, True, False])
        self.assertIn("Unverified (nothing promoted; review copies kept): 1", self.lines)


class EncoderLifetimeTests(Scenario):
    """ffmpeg is stopped and reaped before its output is kept (T-129, T-130):
    Ctrl-C while it starts or is being stopped, stderr reader errors, and an
    ffmpeg that cannot be confirmed stopped, which stops the run and the
    batch and leaves the source attached. Real SIGINTs go to this process at
    the fake boundaries; only behavioural interfaces are used."""

    READ_ERROR = OSError(errno.EIO, "synthetic stderr read error")

    def setUp(self):
        super().setUp()
        if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
            self.skipTest("needs Python's default SIGINT handler")

    def run_convert(self, disc):
        try:
            return self.convert(disc)
        except KeyboardInterrupt:
            self.fail("Ctrl-C escaped convert()")

    def run_main(self, *args):
        try:
            return self.main_exit(*args)
        except KeyboardInterrupt:
            self.fail("Ctrl-C escaped main()")

    def lifecycle(self, disc, vts):
        name = self.mp4(disc, vts).name
        return [(kind, timeout) for kind, n, timeout in self.tools.events if n == name]

    def assert_stopped_and_reaped(self, disc, vts):
        events = self.lifecycle(disc, vts)
        kinds = [kind for kind, _ in events]
        self.assertTrue(events, "ffmpeg was never stopped")
        self.assertEqual(kinds[0], "kill", events)
        self.assertEqual(kinds[-1], "reaped", events)
        waits = [timeout for kind, timeout in events if kind == "wait"]
        self.assertTrue(waits and all(t is not None and 0 < t <= 60 for t in waits), events)
        name = self.mp4(disc, vts).name
        self.assertTrue(all(p.poll() is not None for p in self.tools.processes if p.name == name))

    def assert_handler_is_default(self):
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def assert_record_names_existing_files(self, disc, kind):
        manifest, _ = self.tokened_manifest(disc, kind)
        for record in manifest["titlesets"]["outputs"]:
            if record["retained_path"]:
                self.assertTrue(Path(record["retained_path"]).is_file(), record)
        return manifest

    def titles(self, vts_specs, stem=STEM, seed=True):
        """A disc with one titleset per spec (encode keywords for vts 1, 2, ...)"""
        disc = self.make_disc({vts: [4] for vts in range(1, len(vts_specs) + 1)}, stem=stem)
        seeded = self.seed_canonical(disc) if seed else {}
        for vts, spec in enumerate(vts_specs, 1):
            self.vob_durations(vts, 600.0)
            self.output(disc, vts, 600.0, **spec)
        return disc, seeded

    # --- controls ---

    def test_control_healthy_encodes_are_never_stopped(self):
        disc, seeded = self.titles([{"live": True}, {"live": True}])
        for vts in (1, 2):
            self.tools.encodes[self.mp4(disc, vts).name]["data"] = self.encoded_bytes(disc, vts)
        result = self.run_convert(disc)
        self.assertTrue(result.success, result.error_message)
        for vts in (1, 2):
            self.assertEqual(self.mp4(disc, vts).read_bytes(), self.encoded_bytes(disc, vts))
        self.assertEqual(self.tools.events, [], "a healthy ffmpeg was killed or waited for")
        self.assertFalse(getattr(result, "cleanup_incomplete", False))
        self.assertEqual([m.exited for m in self.mount_objects], ["detached"])
        self.assertEqual(self.temps(disc) + self.partials(disc), [])
        self.assert_handler_is_default()

    def test_ffmpeg_stderr_is_decoded_tolerantly(self):
        disc, _ = self.titles([{}], seed=False)
        self.run_convert(disc)
        self.assertEqual(len(self.tools.popen_kwargs), 1)
        kwargs = self.tools.popen_kwargs[0]
        self.assertEqual((kwargs.get("encoding"), kwargs.get("errors")), ("utf-8", "replace"))

    # --- F1: a reader error stops ffmpeg before its output is kept ---

    def test_reader_error_stops_ffmpeg_before_keeping_its_output(self):
        undecodable = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        cases = (("output_and_late_bytes", undecodable, b"LAUNCH ", b"STOP", b"LAUNCH STOP"),
                 ("only_late_output", self.READ_ERROR, None, b"LATE", b"LATE"),
                 ("no_output", self.READ_ERROR, None, None, None))
        for n, (label, error, data, stop, kept) in enumerate(cases, 11):
            with self.subTest(label):
                disc, seeded = self.titles([dict(live=True, stderr_error=error, data=data,
                                                 stop_output=stop), {}], stem=f"JPC_AV_000{n}")
                result = self.run_convert(disc)
                first, second = result.titles
                self.assert_stopped_and_reaped(disc, 1)
                self.assertEqual((first.status, first.error), ("failed", str(error)))
                self.assertFalse(result.interrupted)
                self.assertEqual(second.status, "held")
                self.assertEqual(self.temps(disc), [])
                partials = self.partials(disc)
                if kept is None:
                    self.assertEqual(partials, [])
                    self.assertIsNone(first.retained_path)
                else:
                    self.assertEqual([p.read_bytes() for p in partials], [kept])
                    self.assertEqual((first.retained_path, first.retained_as), (partials[0], "partial"))
                self.assert_record_names_existing_files(disc, "failed")
                self.assert_unchanged(seeded)

    # --- F2: Ctrl-C while ffmpeg starts or is being stopped ---

    def test_ctrl_c_while_ffmpeg_starts_stops_the_remaining_titlesets(self):
        disc, seeded = self.titles([{}, dict(live=True, sigint_at_launch=True, data=None,
                                             stop_output=b"LATE"), {}])
        result = self.run_convert(disc)
        self.assertTrue(result.interrupted)
        self.assertEqual([t.status for t in result.titles], ["held", "failed", "not_attempted"])
        self.assertEqual(len(self.tools.launched), 2)
        second = result.titles[1]
        self.assertEqual(second.error, "Interrupted by operator (Ctrl-C)")
        self.assert_stopped_and_reaped(disc, 2)
        partials = self.partials(disc)
        self.assertEqual([p.read_bytes() for p in partials], [b"LATE"])
        self.assertEqual(second.retained_path, partials[0])
        self.assertEqual(len(self.review_copies(disc)), 1)
        self.assertEqual(self.temps(disc), [])
        manifest = self.assert_record_names_existing_files(disc, "failed")
        self.assertTrue(manifest["conversion_status"]["interrupted"])
        self.assertIn("Interrupted by operator (Ctrl-C)", result.error_message)
        self.assert_unchanged(seeded)
        self.assert_handler_is_default()

    def test_ctrl_c_while_a_launch_fails_still_stops_the_run(self):
        launch_error = OSError(errno.EAGAIN, "synthetic: Resource temporarily unavailable")
        disc, seeded = self.titles([{}, dict(sigint_at_launch=True, launch_error=launch_error), {}])
        result = self.run_convert(disc)
        self.assertTrue(result.interrupted)
        self.assertEqual(self.tools.events, [], "no ffmpeg was started, so none is stopped")
        second = result.titles[1]
        self.assertEqual(second.status, "failed")
        self.assertTrue(second.error.startswith(f"could not start ffmpeg: {launch_error}"), second.error)
        self.assertIn("Interrupted by operator (Ctrl-C)", second.error)
        self.assertEqual(result.titles[2].status, "not_attempted")
        self.assertEqual(self.partials(disc) + self.temps(disc), [])
        self.assert_unchanged(seeded)
        self.assert_handler_is_default()

    def test_ctrl_c_while_ffmpeg_starts_stops_the_batch(self):
        first = self.make_disc({1: [4]}, stem="JPC_AV_00001")
        second = self.make_disc({1: [4]}, stem="JPC_AV_00002")
        seeded = self.seed_canonical(second)
        self.vob_durations(1, 600.0)
        self.output(first, 1, 600.0, live=True, sigint_at_launch=True)
        self.output(second, 1, 600.0)
        # A Ctrl-C that stopped the batch exits 130 (T-144, decision G2-D2)
        self.assertEqual(self.run_main("--batch", str(self.root / "isos")), 130)
        self.assertEqual(self.tools.launched, ["JPC_AV_00001.mp4"])
        self.assert_unchanged(seeded)
        self.assertEqual(sorted(p.name for p in second.out.iterdir()),
                         sorted(p.name for p in seeded))
        manifest, _ = self.tokened_manifest(first, "failed")
        self.assertTrue(manifest["conversion_status"]["interrupted"])
        self.assertIn("Batch interrupted by operator — stopping.", self.lines)

    def test_ctrl_c_while_ffmpeg_is_being_stopped_completes_the_record(self):
        for n, (label, cause) in enumerate((("reader_error", {"stderr_error": self.READ_ERROR}),
                                            ("ctrl_c", {"interrupt": True})), 21):
            with self.subTest(label):
                disc, seeded = self.titles([dict(live=True, data=b"LAUNCH ", stop_output=b"STOP",
                                                 sigint_on_kill=True, **cause), {}],
                                           stem=f"JPC_AV_000{n}")
                result = self.run_convert(disc)
                first, second = result.titles
                self.assert_stopped_and_reaped(disc, 1)
                self.assertTrue(result.interrupted)
                self.assertEqual(first.status, "failed")
                self.assertEqual(second.status, "not_attempted")
                partials = self.partials(disc)
                self.assertEqual([p.read_bytes() for p in partials], [b"LAUNCH STOP"])
                self.assertEqual(first.retained_path, partials[0])
                self.assertEqual(self.temps(disc), [])
                if label == "reader_error":
                    self.assertIn(str(self.READ_ERROR), first.error)
                    self.assertIn("Interrupted by operator (Ctrl-C)", first.error)
                else:
                    self.assertEqual(first.error, "Interrupted by operator (Ctrl-C)")
                self.assert_record_names_existing_files(disc, "failed")
                self.assert_unchanged(seeded)
                self.assert_handler_is_default()

    def test_real_ctrl_c_during_the_encode_is_not_held(self):
        disc, seeded = self.titles([dict(live=True, signalling=True, data=b"LAUNCH ",
                                         stop_output=b"STOP"), {}])
        result = self.run_convert(disc)
        first, second = result.titles
        self.assertTrue(result.interrupted)
        self.assertEqual((first.status, first.error), ("failed", "Interrupted by operator (Ctrl-C)"))
        self.assertEqual(second.status, "not_attempted")
        self.assert_stopped_and_reaped(disc, 1)
        self.assertEqual([p.read_bytes() for p in self.partials(disc)], [b"LAUNCH STOP"])
        self.assert_unchanged(seeded)

    def test_conversion_off_the_main_thread_runs_unprotected(self):
        cases = (("healthy", {"live": True}), ("reader_error", dict(live=True, data=b"LAUNCH",
                                                                    stderr_error=self.READ_ERROR)))
        for n, (label, spec) in enumerate(cases, 31):
            with self.subTest(label):
                disc, _ = self.titles([spec], stem=f"JPC_AV_000{n}", seed=False)
                outcome = {}
                thread = threading.Thread(target=lambda: outcome.update(result=self.convert(disc)))
                thread.start()
                thread.join(60)
                self.assertFalse(thread.is_alive())
                result = outcome["result"]
                if label == "healthy":
                    self.assertTrue(result.success, result.error_message)
                    self.assertTrue(self.mp4(disc, 1).is_file())
                else:
                    self.assert_stopped_and_reaped(disc, 1)
                    self.assertEqual([p.read_bytes() for p in self.partials(disc)], [b"LAUNCH"])
                self.assert_handler_is_default()

    # --- C1: an ffmpeg that cannot be confirmed stopped ---

    def test_ffmpeg_that_cannot_be_confirmed_stopped_stops_the_run(self):
        refused = PermissionError(errno.EPERM, "synthetic: Operation not permitted")
        cases = (("never_exits", {"never_exits": True}, False),
                 ("never_exits_and_kill_error", {"never_exits": True, "kill_error": refused}, False),
                 ("kill_error_but_it_exits", {"kill_error": refused}, True))
        for n, (label, spec, exits) in enumerate(cases, 41):
            with self.subTest(label):
                disc, seeded = self.titles([dict(live=True, data=b"LAUNCH",
                                                 stderr_error=self.READ_ERROR, **spec), {}],
                                           stem=f"JPC_AV_000{n}")
                result = self.run_convert(disc)
                first, second = result.titles
                self.assertFalse(result.success)
                self.assertFalse(result.interrupted)
                self.assertEqual(first.error, str(self.READ_ERROR))
                events = self.lifecycle(disc, 1)
                self.assertTrue(events, "ffmpeg was never stopped")
                self.assertEqual(events[0][0], "kill", events)
                waits = [t for kind, t in events if kind == "wait"]
                self.assertTrue(waits and all(t is not None and 0 < t <= 60 for t in waits), events)
                mount = self.mount_objects[-1]
                pid_warnings = [w for w in result.warnings if "pid 4242" in w]
                if exits:
                    self.assert_stopped_and_reaped(disc, 1)
                    self.assertEqual([p.read_bytes() for p in self.partials(disc)], [b"LAUNCH"])
                    self.assertEqual(second.status, "held")
                    self.assertFalse(getattr(result, "cleanup_incomplete", False))
                    self.assertEqual(pid_warnings, [])
                    self.assertEqual(mount.exited, "detached")
                    continue
                self.assertNotIn("reaped", [kind for kind, _ in events])
                self.assertTrue(getattr(result, "cleanup_incomplete", False))
                self.assertEqual(second.status, "not_attempted")
                self.assertEqual(self.tools.launched.count(self.mp4(disc, 2).name), 0)
                temps = self.temps(disc)
                self.assertEqual([t.read_bytes() for t in temps], [b"LAUNCH"])
                self.assertEqual((first.retained_path, first.retained_as), (temps[0], "encoding_temp"))
                self.assertEqual(self.partials(disc), [])
                self.assertTrue(mount.keep_attached)
                self.assertEqual(mount.exited, "kept attached")
                details = result.encoder_cleanup
                self.assertEqual({k: details[k] for k in ("pid", "source_iso", "possible_output",
                                                          "mount_points", "devices")},
                                 {"pid": 4242, "source_iso": str(disc.iso),
                                  "possible_output": str(temps[0]),
                                  "mount_points": [str(disc.video_ts.parent)],
                                  "devices": ["/dev/disk42"]})
                self.assertIn("still running", details["reason"])
                if "kill_error" in spec:
                    self.assertIn("kill failed", details["reason"])
                self.assertEqual(len(pid_warnings), 1, result.warnings)
                for part in ("may still be running", str(temps[0]), str(disc.video_ts.parent),
                             "/dev/disk42"):
                    self.assertIn(part, pid_warnings[0])
                manifest, log_text = self.tokened_manifest(disc, "failed")
                status = manifest["conversion_status"]
                self.assertTrue(status["cleanup_incomplete"])
                self.assertFalse(status["interrupted"])
                self.assertEqual(status["encoder_cleanup"], details)
                self.assertIn(pid_warnings[0], status["warnings"])
                self.assertIn("encoder cleanup incomplete", status["error_message"])
                for part in ("ENCODER CLEANUP INCOMPLETE", "PID:              4242",
                             f"Possible output:  {temps[0]}", "Devices:          /dev/disk42"):
                    self.assertIn(part, log_text)
                self.assert_unchanged(seeded)

    def test_unconfirmed_stop_stops_the_batch_and_leaves_the_source_attached(self):
        first = self.make_disc({1: [4]}, stem="JPC_AV_00001")
        second = self.make_disc({1: [4]}, stem="JPC_AV_00002")
        seeded = self.seed_canonical(second)
        self.vob_durations(1, 600.0)
        self.output(first, 1, 600.0, live=True, never_exits=True, stderr_error=self.READ_ERROR)
        self.output(second, 1, 600.0)
        self.assertEqual(self.run_main("--batch", str(self.root / "isos")), 1)
        self.assertEqual(self.tools.launched, ["JPC_AV_00001.mp4"])
        self.assertEqual([m.exited for m in self.mount_objects], ["kept attached"])
        self.assert_unchanged(seeded)
        manifest, _ = self.tokened_manifest(first, "failed")
        self.assertTrue(manifest["conversion_status"].get("cleanup_incomplete"))
        self.assertFalse(manifest["conversion_status"]["interrupted"])
        self.assertTrue(any(line.startswith("Batch stopped") for line in self.lines), self.lines)
        self.assertNotIn("Batch interrupted by operator — stopping.", self.lines)

    def test_ctrl_c_pending_as_the_stop_hold_ends_finds_the_recovery_state_set(self):
        stop_code = mv.ISOToMP4Converter._stop_failed_encode.__code__
        causes = (("reader_error", {"stderr_error": self.READ_ERROR}, str(self.READ_ERROR)),
                  ("ctrl_c", {"interrupt": True}, "Interrupted by operator (Ctrl-C)"))
        for n, (label, cause, error) in enumerate(causes, 51):
            with self.subTest(label):
                disc, seeded = self.titles([dict(live=True, data=b"LAUNCH", never_exits=True, **cause),
                                            {}], stem=f"JPC_AV_000{n}")
                state = {}
                real_exit = mv.DeferredInterrupt.__exit__

                def exit_then_ctrl_c(hold, *exc):
                    outcome = real_exit(hold, *exc)
                    if not state and sys._getframe(1).f_code is stop_code:
                        state["sent"] = True
                        send_sigint()  # pending the moment the stop hold is gone
                    return outcome

                with mock.patch.object(mv.DeferredInterrupt, "__exit__", exit_then_ctrl_c):
                    result = self.run_convert(disc)
                self.assertTrue(state, "the Ctrl-C after the stop hold was never sent")
                first, second = result.titles
                self.assertTrue(result.interrupted)
                self.assertEqual((first.status, first.error), ("failed", error))
                self.assertEqual(second.status, "not_attempted")
                self.assertEqual(self.tools.launched.count(self.mp4(disc, 2).name), 0)
                # the mount is protected and the recovery details exist already
                self.assertTrue(getattr(result, "cleanup_incomplete", False))
                mount = self.mount_objects[-1]
                self.assertTrue(mount.keep_attached)
                self.assertEqual(mount.exited, "kept attached")
                temps = self.temps(disc)
                self.assertEqual([t.read_bytes() for t in temps], [b"LAUNCH"])
                self.assertEqual(self.partials(disc), [])
                details = getattr(result, "encoder_cleanup", None) or {}
                self.assertEqual({k: details.get(k) for k in ("pid", "source_iso", "possible_output",
                                                              "mount_points", "devices")},
                                 {"pid": 4242, "source_iso": str(disc.iso),
                                  "possible_output": str(temps[0]),
                                  "mount_points": [str(disc.video_ts.parent)],
                                  "devices": ["/dev/disk42"]})
                self.assertIn("still running", details.get("reason") or "")
                manifest, log_text = self.tokened_manifest(disc, "failed")
                status = manifest["conversion_status"]
                self.assertTrue(status["cleanup_incomplete"])
                self.assertTrue(status["interrupted"])
                self.assertEqual(status["encoder_cleanup"], details)
                self.assertIn("ENCODER CLEANUP INCOMPLETE", log_text)
                self.assert_unchanged(seeded)

@contextlib.contextmanager
def interrupt_after_rename_returns(real_signal=False):
    """Ctrl-C at the first line event in _retain_attempt's own frame after its
    os.replace call returned with the rename committed. The frame is found by
    the function's code object and the moment by a pass-through os.replace;
    nothing depends on line numbers or source text. real_signal delivers a
    real SIGINT there (the default handler raises it), else KeyboardInterrupt
    is raised directly. Yields the rename's target and the output's identity."""
    code = mv.ISOToMP4Converter._retain_attempt.__code__
    real_replace = os.replace
    state = {"returned": False, "fired": False, "identity": None, "target": None}

    def replace(src, dst, *args, **kwargs):
        mine = sys._getframe(1).f_code is code and state["identity"] is None
        if mine:
            st = os.lstat(src)
            state["identity"], state["target"] = (st.st_dev, st.st_ino), Path(dst)
        real_replace(src, dst, *args, **kwargs)
        if mine:
            state["returned"] = True  # only once the rename committed

    def local(frame, event, arg):
        if state["returned"] and not state["fired"] and event == "line":
            state["fired"] = True
            if real_signal:
                send_sigint()
            raise KeyboardInterrupt
        return local

    def tracer(frame, event, arg):
        return local if frame.f_code is code else None

    previous = sys.gettrace()
    with mock.patch.object(mv.os, "replace", replace):
        sys.settrace(tracer)
        try:
            yield state
        finally:
            sys.settrace(previous)


class RetentionTests(ScenarioRunner, Scenario):
    """Non-clobbering .partial / .mp4.unverified names and failed moves"""

    def unverified_case(self, stem=STEM):
        disc = self.make_disc({1: [4]}, stem=stem)
        self.vob_durations(1, 600.0)
        self.output(disc, 1, None)
        return disc

    def failed_case(self):
        disc = self.make_disc({1: [4]})
        self.vob_durations(1, 600.0)
        self.output(disc, 1, 300.0)
        return disc

    def test_forced_review_copy_collision_never_clobbers(self):
        self.freeze_tokens()
        disc = self.unverified_case()
        disc.out.mkdir(parents=True)
        taken = {}
        for n in range(1, 4):
            path = disc.out / f"{STEM}_unverified-{STAMP}-{n:06x}.mp4.unverified"
            path.write_bytes(b"SOMEONE ELSE'S FILE " + str(n).encode())
            taken[path] = path.read_bytes()
        result = self.convert(disc)
        self.assert_unchanged(taken)
        title = result.titles[0]
        self.assertNotIn(title.retained_path, taken)
        self.assertEqual(title.retained_as, "review_copy")
        self.assertEqual(title.retained_path.read_bytes(), self.encoded_bytes(disc, 1))
        self.assertTrue(title.retained_path.name.startswith(f"{STEM}_unverified-{STAMP}-"))
        manifest, _ = self.tokened_manifest(disc, "unverified")
        self.assertEqual(manifest["titlesets"]["outputs"][0]["retained_path"],
                         str(title.retained_path))

    def test_forced_partial_collision_records_the_actual_name(self):
        self.freeze_tokens()
        disc = self.failed_case()
        disc.out.mkdir(parents=True)
        first = disc.out / f"{STEM}_failed-{STAMP}-000003.mp4.partial"
        first.write_bytes(b"SOMEONE ELSE'S FILE")
        result = self.convert(disc)
        title = result.titles[0]
        self.assertEqual(first.read_bytes(), b"SOMEONE ELSE'S FILE")
        self.assertEqual(title.retained_path, disc.out / f"{STEM}_failed-{STAMP}-000003-000004.mp4.partial")
        manifest, _ = self.tokened_manifest(disc, "failed")
        self.assertEqual(manifest["titlesets"]["outputs"][0]["partial_renamed_to"],
                         str(title.retained_path))

    def test_symlink_at_the_review_name_is_left_alone(self):
        self.freeze_tokens()
        disc = self.unverified_case()
        disc.out.mkdir(parents=True)
        links = []
        for n in range(1, 4):
            link = disc.out / f"{STEM}_unverified-{STAMP}-{n:06x}.mp4.unverified"
            os.symlink(self.root / f"elsewhere-{n}", link)
            links.append(link)
        result = self.convert(disc)
        for n, link in enumerate(links, 1):
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(self.root / f"elsewhere-{n}"))
            self.assertFalse((self.root / f"elsewhere-{n}").exists())
        self.assertEqual(result.titles[0].retained_as, "review_copy")

    def test_exhausted_names_leave_the_output_in_place(self):
        disc = self.unverified_case()
        real_open = os.open

        def always_taken(path, flags, *args):
            if str(path).endswith(".mp4.unverified") and flags & os.O_EXCL:
                raise FileExistsError(17, "File exists", str(path))
            return real_open(path, flags, *args)

        with mock.patch.object(mv.os, "open", always_taken):
            result = self.convert(disc)
        title = result.titles[0]
        self.assertEqual(title.retained_as, "encoding_temp")
        self.assertIn("no free name", title.retention_error)
        self.assertEqual(title.retained_path, self.temps(disc)[0])
        self.assertEqual(title.retained_path.read_bytes(), self.encoded_bytes(disc, 1))

    def failing_move(self, suffix, error=PermissionError(13, "Permission denied")):
        real_replace = os.replace

        def replace(src, dst):
            if str(dst).endswith(suffix):
                raise error
            return real_replace(src, dst)

        return mock.patch.object(mv.os, "replace", replace)

    def assert_left_at_temp(self, disc, title, suffix):
        temps = self.temps(disc)
        self.assertEqual(len(temps), 1)
        self.assertEqual(title.retained_path, temps[0])
        self.assertEqual(title.retained_as, "encoding_temp")
        self.assertEqual(temps[0].read_bytes(), self.encoded_bytes(disc, 1))
        self.assertIn("could not move", title.retention_error)
        self.assertEqual(self.glob(disc, f"*{suffix}"), [], "an empty placeholder was left")

    def test_failed_review_copy_move_keeps_the_output_and_says_where(self):
        disc = self.unverified_case()
        with self.failing_move(".mp4.unverified"):
            result = self.convert(disc)
        title = result.titles[0]
        self.assert_left_at_temp(disc, title, ".mp4.unverified")
        self.assertTrue(result.unverified)
        self.assertTrue(any("could not be kept" in w and str(title.retained_path) in w
                            for w in result.warnings))
        manifest, log_text = self.tokened_manifest(disc, "unverified")
        record = manifest["titlesets"]["outputs"][0]
        self.assertEqual((record["retained_as"], record["retained_path"]),
                         ("encoding_temp", str(title.retained_path)))
        self.assertIn("left at its temporary name", log_text)

    def test_failed_partial_move_keeps_the_output_and_says_where(self):
        disc = self.failed_case()
        with self.failing_move(".mp4.partial"):
            result = self.convert(disc)
        title = result.titles[0]
        self.assert_left_at_temp(disc, title, ".mp4.partial")
        manifest, _ = self.tokened_manifest(disc, "failed")
        record = manifest["titlesets"]["outputs"][0]
        self.assertIsNone(record["partial_renamed_to"])
        self.assertEqual(record["retained_path"], str(title.retained_path))

    def test_reservation_failure_keeps_the_output(self):
        disc = self.unverified_case()
        real_open = os.open

        def refuse(path, flags, *args):
            if str(path).endswith(".mp4.unverified"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, flags, *args)

        with mock.patch.object(mv.os, "open", refuse):
            result = self.convert(disc)
        title = result.titles[0]
        self.assertIn("could not reserve", title.retention_error)
        self.assertEqual(title.retained_path, self.temps(disc)[0])

    def test_unremovable_placeholder_is_reported_not_retained(self):
        disc = self.unverified_case()
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if str(path).endswith(".mp4.unverified"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_unlink(path, *args, **kwargs)

        with self.failing_move(".mp4.unverified"), mock.patch.object(mv.os, "unlink", unlink):
            result = self.convert(disc)
        title = result.titles[0]
        self.assertIn("could not be removed", title.retention_error)
        self.assertEqual(title.retained_path, self.temps(disc)[0])
        placeholders = self.glob(disc, "*.mp4.unverified")
        self.assertEqual(len(placeholders), 1)
        self.assertEqual(placeholders[0].stat().st_size, 0)
        self.assertNotEqual(title.retained_path, placeholders[0])

    # --- T-132 (F4): an interrupted or repeated retention records where the output is ---

    def move_then_interrupt(self, suffix, commit=True, elsewhere=None):
        """os.replace for the first move to a name ending in suffix: does the
        real move (unless commit is False), then raises KeyboardInterrupt, as
        a Ctrl-C seen when the rename call returns. elsewhere: instead, the
        output goes to that path and an unrelated file takes the target."""
        real_replace = os.replace
        fired = {}

        def replace(src, dst):
            if not fired and str(dst).endswith(suffix):
                st = os.lstat(src)
                fired.update(target=Path(dst), identity=(st.st_dev, st.st_ino))
                if elsewhere is not None:
                    real_replace(src, elsewhere)
                    unrelated = Path(dst).with_name("unrelated.tmp")
                    unrelated.write_bytes(b"SOMEONE ELSE'S FILE")
                    real_replace(unrelated, dst)
                elif commit:
                    real_replace(src, dst)
                raise KeyboardInterrupt
            return real_replace(src, dst)

        patcher = mock.patch.object(mv.os, "replace", replace)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertTrue(fired, "the interrupt never fired"))
        return fired

    def run_convert(self, disc):
        try:
            return self.convert(disc)
        except KeyboardInterrupt:
            self.fail("Ctrl-C escaped convert()")

    def assert_kept(self, disc, title, kept, kept_as, fired):
        self.assertEqual(len(kept), 1, kept)
        self.assertEqual((title.retained_path, title.retained_as), (kept[0], kept_as))
        self.assertIsNone(title.retention_error)
        st = os.lstat(kept[0])
        self.assertEqual((st.st_dev, st.st_ino), fired["identity"])
        self.assertEqual(kept[0].read_bytes(), self.encoded_bytes(disc, 1))
        self.assertEqual(self.temps(disc), [])

    def test_interrupt_after_a_committed_review_copy_move_records_it(self):
        disc = self.unverified_case()
        seeded = self.seed_canonical(disc)
        fired = self.move_then_interrupt(".mp4.unverified")
        result = self.run_convert(disc)
        title = result.titles[0]
        self.assertTrue(result.interrupted)
        self.assertFalse(result.success)
        self.assert_kept(disc, title, self.review_copies(disc), "review_copy", fired)
        manifest, log_text = self.tokened_manifest(disc, "failed")
        record = manifest["titlesets"]["outputs"][0]
        self.assertEqual((record["retained_path"], record["retained_as"]),
                         (str(title.retained_path), "review_copy"))
        self.assertIn(f"Kept as:  {title.retained_path} (review copy", log_text)
        self.assertFalse(any("could not be kept" in w for w in result.warnings), result.warnings)
        self.assert_unchanged(seeded)

    def test_interrupt_before_the_move_commits_is_unchanged(self):
        with self.subTest("review copy"):
            disc = self.unverified_case(stem="JPC_AV_00051")
            self.move_then_interrupt(".mp4.unverified", commit=False)
            result = self.run_convert(disc)
            title = result.titles[0]
            temps = self.temps(disc)
            self.assertEqual(len(temps), 1)
            self.assertEqual((title.retained_path, title.retained_as), (temps[0], "encoding_temp"))
            self.assertEqual(self.glob(disc, "*.mp4.unverified"), [], "a placeholder was left")
            _, log_text = self.tokened_manifest(disc, "failed")
            self.assertIn("left at its temporary name", log_text)
        with self.subTest("partial: the handler's second call moves it"):
            disc = self.make_disc({1: [4]}, stem="JPC_AV_00052")
            self.vob_durations(1, 600.0)
            self.output(disc, 1, 300.0)
            self.move_then_interrupt(".mp4.partial", commit=False)
            result = self.run_convert(disc)
            title = result.titles[0]
            partials = self.partials(disc)
            self.assertEqual(len(partials), 1)
            self.assertEqual((title.retained_path, title.retained_as), (partials[0], "partial"))
            self.assertEqual(self.temps(disc), [])

    def test_interrupt_after_a_committed_partial_move_survives_the_second_call(self):
        for n, (label, seconds, encode) in enumerate((("failed duration check", 300.0, {}),
                                                      ("ffmpeg error", 600.0, {"returncode": 1})), 53):
            with self.subTest(label):
                disc = self.make_disc({1: [4]}, stem=f"JPC_AV_000{n}")
                seeded = self.seed_canonical(disc)
                self.vob_durations(1, 600.0)
                self.output(disc, 1, seconds, **encode)
                fired = self.move_then_interrupt(".mp4.partial")
                result = self.run_convert(disc)
                title = result.titles[0]
                self.assertTrue(result.interrupted)
                self.assert_kept(disc, title, self.partials(disc), "partial", fired)
                manifest, _ = self.tokened_manifest(disc, "failed")
                self.assertEqual(manifest["titlesets"]["outputs"][0]["partial_renamed_to"],
                                 str(title.retained_path))
                self.assert_unchanged(seeded)

    def test_interrupt_during_the_kept_as_log_keeps_the_record(self):
        disc = self.failed_case()
        state = {}

        def log(message):
            self.lines.append(str(message))
            if ": kept as " in str(message) and not state:
                state["fired"] = True
                raise KeyboardInterrupt

        with mock.patch.object(mv, "log", log):
            result = self.run_convert(disc)
        self.assertTrue(state, "the interrupt never fired")
        title = result.titles[0]
        self.assertTrue(result.interrupted)
        partials = self.partials(disc)
        self.assertEqual(len(partials), 1)
        self.assertEqual((title.retained_path, title.retained_as), (partials[0], "partial"))
        self.assertEqual(partials[0].read_bytes(), self.encoded_bytes(disc, 1))
        manifest, _ = self.tokened_manifest(disc, "failed")
        self.assertEqual(manifest["titlesets"]["outputs"][0]["partial_renamed_to"], str(partials[0]))

    def test_an_unrelated_file_at_the_reserved_name_is_never_claimed(self):
        disc = self.unverified_case()
        elsewhere = self.root / "moved-by-someone-else.mp4"
        fired = self.move_then_interrupt(".mp4.unverified", elsewhere=elsewhere)
        result = self.run_convert(disc)
        title = result.titles[0]
        target = fired["target"]
        self.assertEqual(target.read_bytes(), b"SOMEONE ELSE'S FILE")
        self.assertNotEqual((os.lstat(target).st_dev, os.lstat(target).st_ino), fired["identity"])
        self.assertEqual(elsewhere.read_bytes(), self.encoded_bytes(disc, 1))
        self.assertNotEqual(title.retained_path, target)
        self.assertIsNone(title.retained_path)

    # --- C2: a failed check is not absence ---

    def lstat_failing_for(self, wanted, error):
        """os.lstat raises error for paths that wanted(path) selects"""
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if wanted(Path(os.fsdecode(path))):
                raise error
            return real_lstat(path, *args, **kwargs)

        return mock.patch.object(mv.os, "lstat", lstat)

    def test_a_check_that_fails_is_not_taken_as_absence(self):
        for n, error in enumerate((PermissionError(errno.EACCES, "synthetic: Permission denied"),
                                   OSError(errno.EIO, "synthetic: Input/output error")), 61):
            with self.subTest(errno.errorcode[error.errno]):
                disc = self.make_disc({1: [4]}, stem=f"JPC_AV_000{n}")
                self.vob_durations(1, 600.0)
                self.output(disc, 1, 300.0)
                with self.lstat_failing_for(lambda p: TEMP_NAME.search(p.name), error):
                    result = self.run_convert(disc)
                title = result.titles[0]
                temps = self.temps(disc)
                self.assertEqual(len(temps), 1, "the output was moved or lost")
                self.assertTrue(getattr(title, "location_unverified", False))
                self.assertIn(str(error), title.retention_error)
                self.assertTrue(any("could not be verified" in w and str(error) in w
                                    for w in result.warnings), result.warnings)
                manifest, log_text = self.tokened_manifest(disc, "failed")
                record = manifest["titlesets"]["outputs"][0]
                self.assertTrue(record.get("location_unverified"))
                self.assertIn(str(error), record["retention_error"])
                self.assertIn("Location: ", log_text)

    def test_an_unverifiable_location_after_an_interrupted_move_is_kept(self):
        disc = self.failed_case()
        fired = self.move_then_interrupt(".mp4.partial")
        error = OSError(errno.EIO, "synthetic: Input/output error")
        with self.lstat_failing_for(lambda p: "target" in fired and p == fired["target"], error):
            result = self.run_convert(disc)
        title = result.titles[0]
        self.assertTrue(result.interrupted)
        self.assertEqual(fired["target"].read_bytes(), self.encoded_bytes(disc, 1))
        self.assertTrue(getattr(title, "location_unverified", False))
        self.assertIn(str(fired["target"]), title.retention_error)
        self.assertIn(str(error), title.retention_error)
        self.assertIsNone(title.retained_path, "a name known not to hold it, or one not verified")
        self.assertTrue(any("could not be verified" in w for w in result.warnings), result.warnings)
        manifest, _ = self.tokened_manifest(disc, "failed")
        self.assertTrue(manifest["titlesets"]["outputs"][0].get("location_unverified"))

    # --- the F1-F5 follow-up: the rename-return boundary and repeated retention ---

    RENAME_RETURN_CASES = ("partial: failed duration check", "partial: ffmpeg error",
                           "partial: post-encode exception", "review copy")

    def rename_return_case(self, label, stem):
        if label == "review copy":
            return self.unverified_case(stem=stem)
        disc = self.make_disc({1: [4]}, stem=stem)
        self.vob_durations(1, 600.0)
        if label == "partial: failed duration check":
            self.output(disc, 1, 300.0)
        elif label == "partial: ffmpeg error":
            self.output(disc, 1, 600.0, returncode=1)
        else:
            self.output(disc, 1, 600.0)  # the checksum step below fails
        return disc

    def test_interrupt_as_the_rename_returns_is_reconciled(self):
        for n, label in enumerate(self.RENAME_RETURN_CASES):
            for real_signal in (False, True):
                with self.subTest(label, real_signal=real_signal):
                    if real_signal and signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
                        self.skipTest("needs Python's default SIGINT handler")
                    disc = self.rename_return_case(label, f"JPC_AV_07{n}{int(real_signal)}0")
                    seeded = self.seed_canonical(disc)
                    with contextlib.ExitStack() as stack:
                        if label.endswith("post-encode exception"):
                            stack.enter_context(mock.patch.object(
                                mv, "calculate_checksums",
                                side_effect=RuntimeError("synthetic checksum failure")))
                        state = stack.enter_context(interrupt_after_rename_returns(real_signal))
                        result = self.run_convert(disc)
                    self.assertTrue(state["fired"], "the interrupt never came after the rename")
                    self.assertTrue(result.interrupted)
                    review = label == "review copy"
                    kept_as = "review_copy" if review else "partial"
                    kept = self.review_copies(disc) if review else self.partials(disc)
                    title = result.titles[0]
                    self.assert_kept(disc, title, kept, kept_as, state)
                    manifest, _ = self.tokened_manifest(disc, "failed")
                    record = manifest["titlesets"]["outputs"][0]
                    self.assertEqual((record["retained_path"], record["retained_as"]),
                                     (str(kept[0]), kept_as))
                    if not review:
                        self.assertEqual(record["partial_renamed_to"], str(kept[0]))
                    self.assertFalse(any("could not be kept" in w for w in result.warnings),
                                     result.warnings)
                    self.assert_unchanged(seeded)

    def test_interrupt_after_the_location_is_recorded_keeps_it(self):
        # Both fields already name the kept file when Ctrl-C arrives; a check
        # of that file that then fails must not undo the record
        for n, label in enumerate(("partial: failed duration check", "review copy")):
            with self.subTest(label):
                disc = self.rename_return_case(label, f"JPC_AV_08{n}00")
                seeded = self.seed_canonical(disc)
                kept_as = "review_copy" if label == "review copy" else "partial"
                state = {}
                real_setattr = mv.TitleOutput.__setattr__

                def store_then_interrupt(obj, name, value):
                    real_setattr(obj, name, value)
                    if not state and name == "retained_as" and value == kept_as:
                        st = os.lstat(obj.retained_path)
                        state.update(target=obj.retained_path, identity=(st.st_dev, st.st_ino))
                        raise KeyboardInterrupt

                error = OSError(errno.EIO, "synthetic: Input/output error")
                with mock.patch.object(mv.TitleOutput, "__setattr__", store_then_interrupt), \
                        self.lstat_failing_for(lambda p: p == state.get("target"), error):
                    result = self.run_convert(disc)
                self.assertTrue(state, "the interrupt never came after the record was set")
                self.assertTrue(result.interrupted)
                title = result.titles[0]
                kept = self.review_copies(disc) if label == "review copy" else self.partials(disc)
                self.assert_kept(disc, title, kept, kept_as, state)
                self.assertFalse(getattr(title, "location_unverified", False))
                manifest, _ = self.tokened_manifest(disc, "failed")
                record = manifest["titlesets"]["outputs"][0]
                self.assertEqual((record["retained_path"], record["retained_as"]),
                                 (str(state["target"]), kept_as))
                self.assertFalse(any("could not be kept" in w for w in result.warnings),
                                 result.warnings)
                self.assert_unchanged(seeded)

    def test_a_repeated_failed_check_keeps_the_earlier_candidates(self):
        disc = self.failed_case()
        seeded = self.seed_canonical(disc)
        fired = self.move_then_interrupt(".mp4.partial")
        error = OSError(errno.EIO, "synthetic: Input/output error")
        # both names unreadable from the interrupted move on
        with self.lstat_failing_for(lambda p: "target" in fired and (
                p == fired["target"] or TEMP_NAME.search(p.name)), error):
            result = self.run_convert(disc)
        self.assertTrue(result.interrupted)
        title = result.titles[0]
        target = fired["target"]
        self.assertEqual(target.read_bytes(), self.encoded_bytes(disc, 1))
        self.assertTrue(getattr(title, "location_unverified", False))
        self.assertIn(str(target), title.retention_error)
        self.assertIn("; then could not check", title.retention_error)
        manifest, log_text = self.tokened_manifest(disc, "failed")
        record = manifest["titlesets"]["outputs"][0]
        self.assertIn(str(target), record["retention_error"])
        self.assertIn(str(target), log_text)
        self.assertTrue(any(str(target) in w and "could not be verified" in w
                            for w in result.warnings), result.warnings)
        self.assert_unchanged(seeded)

    def test_a_missing_temp_keeps_the_finding_that_neither_name_holds_it(self):
        disc = self.failed_case()
        seeded = self.seed_canonical(disc)
        elsewhere = self.root / "moved-by-someone-else.mp4"
        self.move_then_interrupt(".mp4.partial", elsewhere=elsewhere)
        result = self.run_convert(disc)
        self.assertTrue(result.interrupted)
        title = result.titles[0]
        self.assertEqual(elsewhere.read_bytes(), self.encoded_bytes(disc, 1))
        self.assertIsNone(title.retained_path)
        self.assertIsNotNone(title.retention_error)
        self.assertIn("neither", title.retention_error)
        manifest, _ = self.tokened_manifest(disc, "failed")
        self.assertIn("neither", manifest["titlesets"]["outputs"][0]["retention_error"] or "")
        self.assertTrue(any("neither" in w and "it was not found" in w for w in result.warnings),
                        result.warnings)
        self.assert_unchanged(seeded)

    # --- MV-13 (T-145): a Ctrl-C while a kept name is reserved ---

    def test_ctrl_c_during_the_reservation_leaves_no_placeholder(self):
        # A real SIGINT in a child, held while the name is reserved: the
        # empty placeholder is removed before the Ctrl-C is acted on, and
        # the attempt is kept under that name
        folder = self.root / "reservation-real"
        outcome = self.run_scenario("reservation", REAL, folder)
        self.assertNotIn("escaped", outcome)
        disc = self.disc_at(folder, outcome["stem"])
        partials = self.partials(disc)
        self.assertEqual([p.stat().st_size > 0 for p in partials], [True],
                         "an empty placeholder was left beside the kept file")
        self.assertEqual(partials[0].read_bytes(), self.encoded_bytes(disc, 1))
        self.assertTrue(outcome["interrupted"])
        title = outcome["titles"][0]
        self.assertEqual((title["retained_path"], title["retained_as"]), (str(partials[0]), "partial"))
        manifest, _ = self.tokened_manifest(disc, "failed")
        self.assertEqual(manifest["titlesets"]["outputs"][0]["partial_renamed_to"], str(partials[0]))
        self.assertEqual(self.temps(disc), [])


    def test_ctrl_c_during_the_reservation_names_a_placeholder_it_cannot_remove(self):
        # A real SIGINT in a child, held while the name is reserved; the
        # placeholder then cannot be removed: the records say so
        folder = self.root / "reservation-unremovable-real"
        outcome = self.run_scenario("reservation_unremovable", REAL, folder)
        self.assertNotIn("escaped", outcome)
        disc = self.disc_at(folder, outcome["stem"])
        partials = self.partials(disc)
        empty = [p for p in partials if p.stat().st_size == 0]
        kept = [p for p in partials if p.stat().st_size > 0]
        self.assertEqual((len(empty), len(kept)), (1, 1), partials)
        manifest, _ = self.tokened_manifest(disc, "failed")
        warnings = manifest["conversion_status"]["warnings"]
        self.assertTrue(any("after a Ctrl-C while its kept name was reserved" in w and empty[0].name in w
                            and "could not be removed" in w for w in warnings), warnings)
        self.assertEqual(manifest["titlesets"]["outputs"][0]["partial_renamed_to"], str(kept[0]))
        self.assertIn("unlink refused", outcome["fired"])


class PromotionTests(Scenario):
    """Deferred promotion: all-pass, and failure or interruption partway"""

    def two_passing_titles(self):
        disc = self.make_disc({1: [4], 2: [4]})
        seeded = self.seed_canonical(disc, vts_list=(1,))  # title02 is new
        for vts in (1, 2):
            self.vob_durations(vts, 600.0)
            self.output(disc, vts, 600.0)
        return disc, seeded

    def replace_raising_on(self, target, error, after_rename=False):
        real_replace = os.replace

        def replace(src, dst):
            if Path(dst) == target:
                if after_rename:
                    real_replace(src, dst)
                raise error
            return real_replace(src, dst)

        return mock.patch.object(mv.os, "replace", replace)

    def test_all_pass_promotes_every_title(self):
        disc, _ = self.two_passing_titles()
        result = self.convert(disc)
        self.assertTrue(result.success)
        first, second = result.titles
        self.assertEqual((first.status, first.promoted_this_run, first.replaced_existing_canonical),
                         ("completed", True, True))
        self.assertEqual((second.status, second.promoted_this_run, second.replaced_existing_canonical),
                         ("completed", True, False))
        for vts in (1, 2):
            self.assertEqual(self.mp4(disc, vts).read_bytes(), self.encoded_bytes(disc, vts))
        manifest = json.loads((disc.out / f"{STEM}_access_manifest.json").read_text())
        status = manifest["conversion_status"]
        self.assertTrue(status["mp4_created"])
        self.assertEqual(status["canonical_files_changed"],
                         [str(self.mp4(disc, 1)), str(self.mp4(disc, 2))])
        outputs = manifest["titlesets"]["outputs"]
        self.assertEqual([(o["duration_check"]["result"], o["promoted_this_run"],
                           o["replaced_existing_canonical"], o["retained_path"]) for o in outputs],
                         [("passed", True, True, None), ("passed", True, False, None)])
        self.assertEqual(self.review_copies(disc) + self.partials(disc) + self.temps(disc), [])

    def assert_stopped_after_first(self, disc, seeded, result, interrupted):
        first, second = result.titles
        self.assertFalse(result.success)
        self.assertEqual(result.interrupted, interrupted)
        self.assertEqual((first.status, first.promoted_this_run), ("completed", True))
        self.assertEqual(self.mp4(disc, 1).read_bytes(), self.encoded_bytes(disc, 1))
        self.assertEqual((second.status, second.promoted_this_run), ("held", False))
        self.assertIn("promotion stopped partway", second.hold_reason)
        self.assertFalse(self.mp4(disc, 2).exists())
        self.assertEqual(second.retained_as, "review_copy")
        self.assertEqual(second.retained_path.read_bytes(), self.encoded_bytes(disc, 2))
        self.assertEqual(result.canonical_files_changed, [self.mp4(disc, 1)])
        # The earlier canonical records were not rewritten, and say so
        records = {p: d for p, d in seeded.items() if p.suffix in (".txt", ".json")}
        self.assert_unchanged(records)
        self.assertTrue(any(f"{STEM}_access_manifest.json" in w and "no longer match" in w
                            for w in result.warnings), result.warnings)
        manifest, log_text = self.tokened_manifest(disc, "failed")
        status = manifest["conversion_status"]
        self.assertEqual(status["canonical_files_changed"], [str(self.mp4(disc, 1))])
        self.assertTrue(status["promotion_error"])
        self.assertIn(f"changed by this run: {STEM}.mp4", log_text)
        return manifest

    def test_second_promotion_failure_stops_and_reports_changes(self):
        disc, seeded = self.two_passing_titles()
        with self.replace_raising_on(self.mp4(disc, 2), PermissionError(13, "Permission denied")):
            result = self.convert(disc)
        self.assertIn("could not be promoted", result.error_message)
        self.assert_stopped_after_first(disc, seeded, result, interrupted=False)

    def test_interrupted_second_promotion_stops_and_reports_changes(self):
        disc, seeded = self.two_passing_titles()
        with self.replace_raising_on(self.mp4(disc, 2), KeyboardInterrupt()):
            result = self.convert(disc)
        self.assert_stopped_after_first(disc, seeded, result, interrupted=True)

    def test_interrupt_just_after_a_rename_counts_it_as_promoted(self):
        disc, _ = self.two_passing_titles()
        with self.replace_raising_on(self.mp4(disc, 2), KeyboardInterrupt(), after_rename=True):
            result = self.convert(disc)
        self.assertFalse(result.success)
        self.assertEqual([t.promoted_this_run for t in result.titles], [True, True])
        self.assertEqual(result.canonical_files_changed, [self.mp4(disc, 1), self.mp4(disc, 2)])
        self.assertTrue(any("no longer match them" in w for w in result.warnings))

    def test_first_promotion_failure_changes_nothing(self):
        disc, seeded = self.two_passing_titles()
        with self.replace_raising_on(self.mp4(disc, 1), PermissionError(13, "Permission denied")):
            result = self.convert(disc)
        self.assertFalse(result.success)
        self.assert_unchanged(seeded)
        self.assertEqual(result.canonical_files_changed, [])
        self.assertEqual([t.status for t in result.titles], ["held", "held"])
        self.assertEqual(len(self.review_copies(disc)), 2)
        self.assertFalse(any("no longer match" in w for w in result.warnings))


class BuildReviewRegressions(Scenario):
    """The two T-071 build-review findings: an IFO is eligible only with its
    required PGC tables present and consistent; unmeasured stream counts are
    unknown, not zero. The controls pass on the reviewed build too; the rest
    failed on it for the reason in their name."""

    CELLS = ([200, 400], [3, 5])  # a 600 s straight-through chain over 8 sectors

    def parse(self, ifo_bytes, vob_sectors=(3, 5)):
        ifo = self.root / "VTS_01_0.IFO"
        ifo.write_bytes(ifo_bytes)
        vobs = []
        for index, sectors in enumerate(vob_sectors, 1):
            vob = self.root / f"VTS_01_{index}.VOB"
            with open(vob, "wb") as f:
                f.truncate(sectors * SECTOR)
            vobs.append(vob)
        return mv.parse_ifo_playback(ifo, vobs)

    def assert_invalid(self, phrase, **ifo_options):
        parsed = self.parse(straight_ifo(*self.CELLS, **ifo_options))
        self.assertEqual(parsed.status, "invalid", parsed.reason)
        self.assertIn(phrase, parsed.reason)
        self.assertIsNone(parsed.seconds)

    # --- controls ---

    def test_control_structurally_valid_ifo_is_supported(self):
        for programs in (1, 2):
            with self.subTest(programs=programs):
                parsed = self.parse(straight_ifo(*self.CELLS, programs=programs))
                self.assertEqual((parsed.status, parsed.seconds), ("supported", 600), parsed.reason)

    def test_control_structurally_valid_ifo_decides_end_to_end(self):
        disc = self.make_disc({1: [3, 5]}, ifos={1: straight_ifo(*self.CELLS, programs=2)})
        self.vob_durations(1, None, None)
        self.output(disc, 1, 600.0)
        result = self.convert(disc)
        title = result.titles[0]
        self.assertTrue(result.success)
        self.assertEqual((title.status, title.expected_duration_source, title.expectation.ifo.status),
                         ("completed", "ifo", "used"))

    def test_control_successful_probes_record_actual_counts(self):
        cases = (("video", "audio", "audio"), "completed", 1, 2), \
                (("video",), "completed", 1, 0), \
                (("audio",), "failed", 0, 1)
        for n, (streams, status, video, audio) in enumerate(cases, 1):
            with self.subTest(streams=streams):
                disc = self.make_disc({1: [4]}, stem=f"JPC_AV_0000{n}")
                self.vob_durations(1, 600.0)
                self.output(disc, 1, 600.0, streams=streams)
                result = self.convert(disc)
                title = result.titles[0]
                self.assertEqual((title.status, title.mapped_video_streams,
                                  title.mapped_audio_streams), (status, video, audio))
                if status == "completed":
                    manifest = json.loads((disc.out / f"{disc.stem}_access_manifest.json").read_text())
                    log_text = (disc.out / f"{disc.stem}.mp4.log.txt").read_text()
                else:
                    manifest, log_text = self.tokened_manifest(disc, "failed")
                    self.assertIn("no video", title.error)
                record = manifest["titlesets"]["outputs"][0]
                self.assertEqual((record["mapped_video_streams"], record["mapped_audio_streams"]),
                                 (video, audio))
                self.assertIn(f"Audio:    {audio} of 1 source track(s) mapped", log_text)

    # --- program and cell counts ---

    def test_more_programs_than_cells_is_invalid(self):
        for programs in (3, 255):
            with self.subTest(programs=programs):
                self.assert_invalid(f"{programs} programs but only 2 cells", programs=programs)

    # --- program map ---

    def test_missing_program_map_is_invalid(self):
        self.assert_invalid("program map is missing", program_map_offset=0)

    def test_program_map_inside_the_header_is_invalid(self):
        self.assert_invalid("program map is missing or out of bounds", program_map_offset=0x20)

    def test_program_map_past_the_table_is_invalid(self):
        self.assert_invalid("program map is missing or out of bounds", program_map_offset=0xFFFF)

    def test_program_map_entries_must_be_increasing_cells_from_1(self):
        for entries in ([2], [0], [1, 1], [2, 1], [1, 3]):
            with self.subTest(entries=entries):
                self.assert_invalid("program map is not increasing cell numbers",
                                    programs=len(entries), program_map=entries)

    def test_program_map_overlapping_the_cell_table_is_invalid(self):
        self.assert_invalid("tables overlap", program_map_offset=0xEE)  # cell table starts at 0xEE

    # --- cell position table ---

    def test_missing_cell_position_table_is_invalid(self):
        self.assert_invalid("cell position table is missing", cell_position_offset=0)

    def test_cell_position_table_inside_the_header_is_invalid(self):
        self.assert_invalid("cell position table is missing or out of bounds",
                            cell_position_offset=0x40)

    def test_cell_position_table_past_the_table_is_invalid(self):
        self.assert_invalid("cell position table is missing or out of bounds",
                            cell_position_offset=0xFFFF)

    def test_cell_position_table_overlapping_the_cell_table_is_invalid(self):
        self.assert_invalid("tables overlap", cell_position_offset=0xEE + 24)

    def test_cell_position_entries_must_be_valid_ids(self):
        for bad in ((0, 0, 2), (1, 7, 2), (1, 0, 0)):
            with self.subTest(entry=bad):
                self.assert_invalid("cell position 2 is not a valid VOB/cell ID",
                                    positions=[(1, 0, 1), bad])

    # --- the optional command table ---

    def test_control_command_table_before_the_program_map_is_supported(self):
        for table in (commands(post=1), commands(pre=1, post=1, cell=2)):
            with self.subTest(table=table[:8].hex()):
                parsed = self.parse(straight_ifo(*self.CELLS, command_table=table))
                self.assertEqual((parsed.status, parsed.seconds), ("supported", 600), parsed.reason)

    def test_command_table_past_the_table_is_invalid(self):
        self.assert_invalid("command table is out of bounds", command_table_offset=0xFFFF)

    def test_command_table_inside_the_header_is_invalid(self):
        self.assert_invalid("command table is out of bounds", command_table_offset=0x10)

    def test_command_table_aliasing_the_program_map_is_invalid(self):
        # The program map's bytes read as a header claiming 256 pre-commands
        self.assert_invalid("command table is inconsistent", command_table_offset=0xEC)

    def test_command_table_shorter_than_its_commands_is_invalid(self):
        short = (2).to_bytes(2, "big") + bytes(4) + (7).to_bytes(2, "big")  # 2 commands, 8 bytes
        self.assert_invalid("command table is inconsistent", command_table=short)

    def test_command_table_overlapping_the_program_map_is_invalid(self):
        self.assert_invalid("tables overlap", command_table=commands(post=1),
                            program_map_offset=0xEC + 8)

    def test_repeated_cell_position_is_advisory(self):
        parsed = self.parse(straight_ifo(*self.CELLS, positions=[(1, 0, 1), (1, 0, 1)]))
        self.assertEqual(parsed.status, "advisory", parsed.reason)
        self.assertIn("repeat a VOB/cell ID", parsed.reason)

    # --- end to end: a malformed IFO never authorizes promotion ---

    MALFORMED = {"255 programs, 2 cells": {"programs": 255},
                 "program map past the file": {"program_map_offset": 0xFFFF},
                 "cell position table past the file": {"cell_position_offset": 0xFFFF}}

    def test_malformed_ifo_yields_to_a_usable_vob_fallback(self):
        # The IFO claims 600 s; the complete VOB probes say 1200 s. Only the
        # fallback may decide, so a 600 s encode is a truncation.
        for n, (case, options) in enumerate(self.MALFORMED.items(), 1):
            with self.subTest(case=case):
                disc = self.make_disc({1: [3, 5]}, stem=f"JPC_AV_0000{n}",
                                      ifos={1: straight_ifo(*self.CELLS, **options)})
                seeded = self.seed_canonical(disc)
                self.vob_durations(1, 600.0, 600.0)
                self.output(disc, 1, 600.0)
                result = self.convert(disc)
                title = result.titles[0]
                self.assertEqual(title.expectation.ifo.status, "invalid")
                self.assertEqual((title.expected_duration_source, title.expected_duration),
                                 ("vob_probe", 1200.0))
                self.assertEqual(title.status, "failed")
                self.assert_unchanged(seeded)

    def test_malformed_ifo_does_not_poison_an_agreeing_vob_fallback(self):
        disc = self.make_disc({1: [3, 5]}, ifos={1: straight_ifo(*self.CELLS, cell_position_offset=0)})
        self.vob_durations(1, 200.0, 400.0)
        self.output(disc, 1, 600.0)
        result = self.convert(disc)
        title = result.titles[0]
        self.assertEqual((title.expectation.ifo.status, title.expected_duration_source),
                         ("invalid", "vob_probe"))
        self.assertEqual(title.status, "completed")
        self.assertTrue(result.success)

    def test_malformed_ifo_without_a_fallback_is_unverified(self):
        disc = self.make_disc({1: [3, 5]}, ifos={1: straight_ifo(*self.CELLS, programs=255)})
        seeded = self.seed_canonical(disc)
        self.vob_durations(1, None, None)
        self.output(disc, 1, 600.0)
        result = self.convert(disc)
        title = result.titles[0]
        self.assertEqual(title.expectation.ifo.status, "invalid")
        self.assertIsNone(title.expected_duration)
        self.assertEqual(title.status, "unverified")
        self.assertTrue(result.unverified)
        self.assert_unchanged(seeded)
        self.assertEqual(len(self.review_copies(disc)), 1)

    # --- unknown stream counts ---

    def test_failed_output_stream_probe_records_unknown_counts(self):
        disc = self.make_disc({1: [4]})
        self.vob_durations(1, 600.0)
        self.output(disc, 1, 600.0, streams=None)
        result = self.convert(disc)
        title = result.titles[0]
        self.assertEqual(title.status, "unverified")
        self.assertEqual((title.mapped_video_streams, title.mapped_audio_streams), (None, None))
        manifest, log_text = self.tokened_manifest(disc, "unverified")
        record = manifest["titlesets"]["outputs"][0]
        self.assertEqual((record["mapped_video_streams"], record["mapped_audio_streams"]),
                         (None, None))
        self.assertIn("Audio:    unknown of 1 source track(s) mapped", log_text)
        self.assertTrue(any("unknown audio track(s)" in line for line in self.lines))

    def test_titlesets_never_probed_record_unknown_counts(self):
        disc = self.make_disc({1: [4], 2: [4], 3: [4]})
        for vts in (1, 2, 3):
            self.vob_durations(vts, 600.0)
            self.output(disc, vts, 600.0, interrupt=(vts == 2))
        self.convert(disc)
        manifest, log_text = self.tokened_manifest(disc, "failed")
        counts = [(o["status"], o["mapped_video_streams"], o["mapped_audio_streams"])
                  for o in manifest["titlesets"]["outputs"]]
        self.assertEqual(counts, [("held", 1, 1), ("failed", None, None),
                                  ("not_attempted", None, None)])
        self.assertIn("Audio:    unknown of unknown source track(s) mapped", log_text)

    def test_dry_run_records_unknown_counts(self):
        disc = self.make_disc({1: [4]})
        config = mv.ConversionConfig(iso_path=disc.iso, output_dir=disc.out,
                                     operator="T071", dry_run=True)
        result = mv.ISOToMP4Converter(config).convert()
        self.assertTrue(result.skipped)
        manifest, _ = self.tokened_manifest(disc, "dryrun")
        record = manifest["titlesets"]["outputs"][0]
        self.assertEqual((record["status"], record["mapped_video_streams"],
                          record["mapped_audio_streams"]), ("planned", None, None))


class SkipRecordTests(Scenario):
    """T-135: a skip or dry run keeps its log and manifest under its own
    token, created new and naming each other; it never writes over an
    earlier conversion's records or an earlier skip's, and a later success
    names skip records left under the old fixed names as earlier ones.
    Control: a plain success names none"""

    TOKEN = "20261024T120000-abcdef"

    def seed(self, disc, names):
        disc.out.mkdir(parents=True, exist_ok=True)
        seeded = {}
        for name in names:
            path = disc.out / name
            path.write_bytes(b"EARLIER RECORD " + name.encode())
            seeded[path] = path.read_bytes()
        return seeded

    def legacy(self, disc):
        """Skip and dry-run records under the old fixed names"""
        return self.seed(disc, [f"{disc.stem}_skip_manifest.json", f"{disc.stem}.mp4.dryrun.log.txt"])

    def dry_run(self, disc):
        config = mv.ConversionConfig(iso_path=disc.iso, output_dir=disc.out,
                                     operator="T071", dry_run=True)
        return mv.ISOToMP4Converter(config).convert()

    def assert_own_records(self, disc, kind, status):
        manifest, log_text = self.tokened_manifest(disc, kind)
        (log_path,), (manifest_path,) = self.records(disc, kind)
        self.assertEqual(manifest["conversion_status"]["overall_status"], status)
        self.assertEqual((manifest["output_files"]["log_file"], manifest["output_files"]["manifest_file"]),
                         (str(log_path), str(manifest_path)))
        self.assertIn(f"Manifest:         {manifest_path.name} (this attempt only", log_text)
        return manifest, log_text

    def test_a_skip_never_writes_over_an_earlier_conversions_records(self):
        disc = self.make_disc({})  # VIDEO_TS with no VOBs: skipped
        seeded = self.seed_canonical(disc)
        self.answers.clear()  # a skip asks nothing
        result = self.convert(disc)
        self.assertTrue(result.success and result.skipped, result.error_message)
        self.assert_unchanged(seeded)
        self.assert_own_records(disc, "skipped", "skipped")
        self.assertEqual(self.glob(disc, "*_skip_manifest.json") + self.glob(disc, "*dryrun*"), [])

    def test_a_dry_run_never_writes_over_earlier_records(self):
        disc = self.make_disc({1: [4]})
        seeded = {**self.seed_canonical(disc), **self.legacy(disc)}
        result = self.dry_run(disc)
        self.assertTrue(result.success and result.skipped, result.error_message)
        self.assert_unchanged(seeded)
        self.assert_own_records(disc, "dryrun", "dry_run")

    def test_repeated_skips_and_dry_runs_keep_every_record(self):
        disc = self.make_disc({})
        for run in (self.convert, self.convert, self.dry_run):
            kept = {p: p.read_bytes() for p in self.glob(disc, "*")}
            result = run(disc)
            self.assertTrue(result.success and result.skipped, result.error_message)
            self.assert_unchanged(kept)
        self.assertEqual([len(found) for found in self.records(disc, "skipped")], [2, 2])
        self.assertEqual([len(found) for found in self.records(disc, "dryrun")], [1, 1])

    def test_a_taken_record_name_is_never_written_over(self):
        for taken in ("_manifest.json", ".log.txt"):
            with self.subTest(taken):
                disc = self.make_disc({}, stem=f"JPC_AV_0000{2 if taken == '.log.txt' else 3}")
                path = disc.out / f"{disc.stem}_skipped-{self.TOKEN}{taken}"
                seeded = self.seed(disc, [path.name])
                with mock.patch.object(mv.ISOToMP4Converter, "_run_token",
                                       lambda obj, kind="failed": f"{kind}-{self.TOKEN}"):
                    result = self.convert(disc)
                self.assert_unchanged(seeded)
                self.assertFalse(result.success or result.skipped)
                self.assertIn("Run records could not be written", result.error_message)

    def test_a_later_success_names_earlier_skip_records(self):
        disc = self.make_disc({1: [4]})
        legacy = self.legacy(disc)
        self.vob_durations(1, 600.0)
        self.output(disc, 1, 600.0)
        result = self.convert(disc)
        self.assertTrue(result.success and not result.skipped, result.error_message)
        self.assert_unchanged(legacy)
        manifest = json.loads((disc.out / f"{disc.stem}_access_manifest.json").read_text())
        self.assertEqual(manifest["output_files"]["earlier_skip_records"], [str(p) for p in legacy])
        self.assertEqual(manifest["conversion_status"]["overall_status"], "success")
        log_text = (disc.out / f"{disc.stem}.mp4.log.txt").read_text()
        for path in legacy:
            self.assertIn(f"Earlier record:   {path} (from an earlier skipped or dry run; "
                          f"it does not describe this conversion)", log_text)

    def test_control_a_plain_success_names_no_earlier_records(self):
        disc = self.make_disc({1: [4]})
        self.vob_durations(1, 600.0)
        self.output(disc, 1, 600.0)
        result = self.convert(disc)
        self.assertTrue(result.success and not result.skipped, result.error_message)
        manifest = json.loads((disc.out / f"{disc.stem}_access_manifest.json").read_text())
        self.assertEqual(manifest["output_files"].get("earlier_skip_records", []), [])
        self.assertNotIn("Earlier record:", (disc.out / f"{disc.stem}.mp4.log.txt").read_text())

    def test_a_half_written_skip_or_dry_run_record_leaves_no_orphan(self):
        # N16 (T-145): a skip or dry-run record is all or nothing; when it
        # cannot be completed, failure records describe the run instead
        enospc = OSError(errno.ENOSPC, "synthetic: No space left on device")
        cases = (("skip log", False, "log"), ("skip manifest", False, "manifest"),
                 ("dry-run manifest", True, "manifest"))
        for n, (label, dry, fault) in enumerate(cases, 4):
            with self.subTest(label):
                disc = self.make_disc({1: [4]} if dry else {}, stem=f"JPC_AV_0000{n}")
                fired = []
                if fault == "log":
                    real_log = mv.ISOToMP4Converter._create_log_file

                    def create_log(converter, result, *args, **kwargs):
                        if not fired and str(kwargs.get("token")).startswith("skipped"):
                            fired.append("log")
                            raise enospc
                        return real_log(converter, result, *args, **kwargs)

                    patch = mock.patch.object(mv.ISOToMP4Converter, "_create_log_file", create_log)
                else:
                    real_dump = json.dump

                    def dump(obj, fp, *args, **kwargs):
                        if not fired:
                            fired.append("manifest")
                            fp.write('{"conversion_metadata": {')
                            raise enospc
                        return real_dump(obj, fp, *args, **kwargs)

                    patch = mock.patch.object(mv.json, "dump", dump)
                with patch:
                    result = self.dry_run(disc) if dry else self.convert(disc)
                kind = "dryrun" if dry else "skipped"
                self.assertEqual(self.glob(disc, f"*_{kind}-*"), [], "half a record pair was left")
                self.assertFalse(result.success or result.skipped)
                self.assertTrue(result.error_message.startswith("Run records could not be written"),
                                result.error_message)
                self.assertIn("No space left on device", result.error_message)
                manifest, _ = self.tokened_manifest(disc, "failed")
                self.assertEqual(manifest["conversion_status"]["overall_status"], "error")
                self.assertTrue(fired)


class SourceMountTests(ScenarioRunner, unittest.TestCase):
    """C1: a source image kept attached is never detached - no forced
    detach, device fallback or leak sweep. Controls: an ordinary exit still
    detaches. hdiutil is faked at run_cmd and only recorded. A Ctrl-C during
    a detach is recorded, not raised (T-144), and a mount point that cannot
    be checked is still detached."""

    def test_ctrl_c_during_a_detach_is_recorded_not_raised(self):
        root = Path(tempfile.mkdtemp(prefix="t144-mount-"))
        self.addCleanup(shutil.rmtree, root, True)
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome = self.run_scenario("detach_command", mode,
                                            root / ("real" if mode == REAL else "raised"))
                self.assertNotIn("escaped", outcome)
                self.assertEqual(len(outcome["calls"]), 1, "detaching went on after the Ctrl-C")
                self.assertTrue(outcome["detach_interrupted"])
                errors = outcome["detach_errors"]
                self.assertEqual(len(errors), 1, errors)
                for part in ("JPC_AV_00131.iso", "interrupted by operator (Ctrl-C)",
                             "may still be attached", "check with hdiutil info", "/dev/disk42"):
                    self.assertIn(part, errors[0])

    def test_a_mount_point_that_cannot_be_checked_is_still_detached(self):
        root = Path(tempfile.mkdtemp(prefix="t144-mount-"))
        self.addCleanup(shutil.rmtree, root, True)
        volume = root / "volume"
        volume.mkdir()
        calls, fired = [], []
        error = OSError(errno.EIO, "synthetic: Input/output error")

        def run_cmd(cmd, *args, **kwargs):
            calls.append([str(c) for c in cmd])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def failing(real):
            def call(path, *args, **kwargs):
                if not isinstance(path, int) and os.fsdecode(path) == str(volume):
                    fired.append(real.__name__)
                    raise error
                return real(path, *args, **kwargs)
            return call

        mount = mv.ISOMount(root / "JPC_AV_00001.iso")
        mount.mount_points, mount.devices = [volume], ["/dev/disk42"]
        with mock.patch.object(mv, "run_cmd", run_cmd), \
                mock.patch.object(mv, "log", lambda message: None), \
                mock.patch.object(mv.os, "lstat", failing(os.lstat)), \
                mock.patch.object(mv.os, "stat", failing(os.stat)), \
                mock.patch.object(Path, "stat", failing(Path.stat)):
            try:
                mount.__exit__(None, None, None)
            except OSError as e:
                self.fail(f"the check of the mount point escaped: {e}")
        self.assertEqual(calls, [["hdiutil", "detach", str(volume), "-force"]])
        self.assertEqual(mount.detach_errors, [])
        self.assertTrue(fired)

    def test_a_source_kept_attached_is_never_detached(self):
        root = Path(tempfile.mkdtemp(prefix="t129-mount-"))
        self.addCleanup(shutil.rmtree, root, True)
        volume = root / "volume"
        volume.mkdir()
        cases = (("kept, with a mount point", True, [volume], []),
                 ("kept, devices only", True, [], []),
                 ("control: detach fails, device fallback", False, [volume],
                  [["hdiutil", "detach", str(volume), "-force"],
                   ["hdiutil", "detach", "/dev/disk42", "-force"]]),
                 ("control: devices only, leak sweep", False, [],
                  [["hdiutil", "detach", "/dev/disk42", "-force"]]))
        for label, kept, mount_points, expected in cases:
            with self.subTest(label):
                calls = []

                def run_cmd(cmd, *args, **kwargs):
                    calls.append([str(c) for c in cmd])
                    if cmd[2] == str(volume):
                        raise subprocess.CalledProcessError(1, cmd)
                    return subprocess.CompletedProcess(cmd, 0, "", "")

                mount = mv.ISOMount(root / "JPC_AV_00001.iso")
                mount.mount_points, mount.devices = list(mount_points), ["/dev/disk42"]
                if kept:
                    mount.keep_attached = "ffmpeg (pid 4242) could not be confirmed stopped"
                with mock.patch.object(mv, "run_cmd", run_cmd), \
                        mock.patch.object(mv, "log", lambda message: None):
                    mount.__exit__(None, None, None)
                self.assertEqual(calls, expected)

### === RUN-ENDING SCENARIOS (T-144, T-145, T-146) ===


def seeded_disc(case, stem, titlesets=1, **encode):
    """A disc whose titlesets 1..n pass (or end as encode says), over an
    earlier canonical MP4 (titleset 1), log and manifest; answers y"""
    disc = case.make_disc({vts: [4] for vts in range(1, titlesets + 1)}, stem=stem)
    for vts in range(1, titlesets + 1):
        case.vob_durations(vts, 600.0)
        case.output(disc, vts, 600.0, **encode)
    case.seed_canonical(disc)
    return disc


def new_disc(case, stem, seconds=600.0, **encode):
    """A one-titleset disc with no earlier outputs"""
    disc = case.make_disc({1: [4]}, stem=stem)
    case.vob_durations(1, 600.0)
    case.output(disc, 1, seconds, **encode)
    return disc


def mount_interrupting(interrupt, fired, on):
    """The harness's fake mount, interrupted as it attaches ("enter") or
    once it has detached ("exit")"""
    base = mv.ISOMount

    class Interrupting(base):
        def __enter__(self):
            if on == "enter":
                fired.append("attach")
                interrupt()
            return super().__enter__()

        def __exit__(self, *exc):
            super().__exit__(*exc)
            if on == "exit":
                fired.append("detach")
                interrupt()
            return False

    return mock.patch.object(mv, "ISOMount", Interrupting)


def first_call_interrupting(obj, name, interrupt, fired, when=lambda *args, **kwargs: True):
    """obj.name delivering the interrupt on its first call that `when`
    selects, then (if that returns) doing what it does"""
    real = getattr(obj, name)

    def call(*args, **kwargs):
        if not fired and when(*args, **kwargs):
            fired.append(name)
            interrupt()
        return real(*args, **kwargs)

    return mock.patch.object(obj, name, call)


def log_interrupting(case, interrupt, fired, phrase):
    """The harness's log, delivering the interrupt once the first message
    containing phrase is shown"""
    def log(message):
        case.lines.append(str(message))
        if not fired and phrase in str(message):
            fired.append(phrase)
            interrupt()

    return mock.patch.object(mv, "log", log)


def running(case, run, *patches, **extra):
    """capture(run) with the patches applied; the outcome with extra"""
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        outcome = capture(case, run)
    outcome.update(extra)
    return outcome


@child_scenario
def detach_after_promotion(case, interrupt):
    """N3: Ctrl-C while the image is detached, once every titleset is promoted"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00101")
    return running(case, lambda: case.convert(disc), mount_interrupting(interrupt, fired, "exit"),
                   fired=fired, stem=disc.stem)


@child_scenario
def canonical_log_body(case, interrupt):
    """N3: Ctrl-C while the canonical log is written (python_version runs in its body)"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00102")
    return running(case, lambda: case.convert(disc),
                   first_call_interrupting(mv.platform, "python_version", interrupt, fired),
                   fired=fired, stem=disc.stem)


@child_scenario
def record_details(case, interrupt):
    """N3: Ctrl-C while ffprobe reads the promoted MP4 for the manifest"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00103")
    final = str(case.mp4(disc, 1))
    return running(case, lambda: case.convert(disc),
                   first_call_interrupting(mv, "get_video_info", interrupt, fired,
                                           lambda target: str(target) == final),
                   fired=fired, stem=disc.stem)


@child_scenario
def between_record_renames(case, interrupt):
    """Ctrl-C once the canonical log is in place, as the manifest is renamed"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00104")
    manifest = disc.out / f"{disc.stem}_access_manifest.json"
    return running(case, lambda: case.convert(disc),
                   first_call_interrupting(mv.os, "replace", interrupt, fired,
                                           lambda src, dst, *args, **kwargs: Path(dst) == manifest),
                   fired=fired, stem=disc.stem)


@child_scenario
def after_records(case, interrupt):
    """Ctrl-C while the success summary is shown"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00105")
    return running(case, lambda: case.convert(disc),
                   log_interrupting(case, interrupt, fired, "Source ISO:"),
                   fired=fired, stem=disc.stem)


@child_scenario
def titleset_outcomes(case, interrupt):
    """Ctrl-C while the titleset outcomes are shown, before the detach"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00106")
    return running(case, lambda: case.convert(disc),
                   log_interrupting(case, interrupt, fired, "duration check passed; promoted"),
                   fired=fired, stem=disc.stem)


@child_scenario
def failed_detach(case, interrupt):
    """N7: a failed run; Ctrl-C while the image is detached"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00107", returncode=1)
    return running(case, lambda: case.convert(disc), mount_interrupting(interrupt, fired, "exit"),
                   fired=fired, stem=disc.stem)


@child_scenario
def failed_record_log(case, interrupt):
    """N7: a failed run; Ctrl-C while its failure log is written"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00108", returncode=1)
    return running(case, lambda: case.convert(disc),
                   first_call_interrupting(mv.platform, "python_version", interrupt, fired),
                   fired=fired, stem=disc.stem)


@child_scenario
def ffmpeg_error_output(case, interrupt):
    """N17: Ctrl-C while ffmpeg's error output is shown"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00109", returncode=1)
    return running(case, lambda: case.convert(disc),
                   log_interrupting(case, interrupt, fired, "ffmpeg error:"),
                   fired=fired, stem=disc.stem)


@child_scenario
def truncation_reason_output(case, interrupt):
    """N17: Ctrl-C while a truncated encode's reason is shown"""
    fired, disc = [], new_disc(case, "JPC_AV_00110", 300.0)
    return running(case, lambda: case.convert(disc),
                   log_interrupting(case, interrupt, fired, "truncated encode"),
                   fired=fired, stem=disc.stem)


@child_scenario
def overwrite_prompt(case, interrupt):
    """N15: a batch of two; Ctrl-C at the first ISO's overwrite prompt"""
    fired = []
    first, second = seeded_disc(case, "JPC_AV_00111"), new_disc(case, "JPC_AV_00112")
    case.answers.clear()

    def answer(prompt=""):
        fired.append(prompt)
        interrupt()
        return "y"

    return running(case, lambda: case.main_exit("--batch", str(case.root / "isos")),
                   mock.patch("builtins.input", answer),
                   fired=fired, stems=[first.stem, second.stem], launched=case.tools.launched)


@child_scenario
def batch_records(case, interrupt):
    """A batch of two new ISOs; Ctrl-C while the first one's log is written"""
    fired = []
    first, second = new_disc(case, "JPC_AV_00113"), new_disc(case, "JPC_AV_00114")
    return running(case, lambda: case.main_exit("--batch", str(case.root / "isos")),
                   first_call_interrupting(mv.platform, "python_version", interrupt, fired),
                   fired=fired, stems=[first.stem, second.stem], launched=case.tools.launched)


@child_scenario
def single_records(case, interrupt):
    """One new ISO through main(); Ctrl-C while its log is written"""
    fired, disc = [], new_disc(case, "JPC_AV_00115")
    return running(case, lambda: case.main_exit("-i", str(disc.iso)),
                   first_call_interrupting(mv.platform, "python_version", interrupt, fired),
                   fired=fired, stem=disc.stem)


@child_scenario
def dependency_check(case, interrupt):
    """Ctrl-C while ffprobe is checked, before the ISO is attached"""
    fired, disc = [], new_disc(case, "JPC_AV_00116")
    return running(case, lambda: case.main_exit("-i", str(disc.iso)),
                   first_call_interrupting(mv, "check_ffprobe", interrupt, fired),
                   fired=fired, stem=disc.stem, launched=case.tools.launched)


@child_scenario
def attach(case, interrupt):
    """Ctrl-C while the ISO is attached"""
    fired, disc = [], new_disc(case, "JPC_AV_00117")
    return running(case, lambda: case.main_exit("-i", str(disc.iso)),
                   mount_interrupting(interrupt, fired, "enter"), fired=fired, stem=disc.stem,
                   launched=case.tools.launched)


@child_scenario
def analysis(case, interrupt):
    """Ctrl-C while the disc is analysed"""
    fired, disc = [], new_disc(case, "JPC_AV_00118")
    return running(case, lambda: case.main_exit("-i", str(disc.iso)),
                   first_call_interrupting(mv, "analyze_iso_content", interrupt, fired),
                   fired=fired, stem=disc.stem, launched=case.tools.launched)


@child_scenario
def real_detach_batch(case, interrupt):
    """A batch of two through main(), the real ISOMount detaching (attach
    skipped: the harness's folder; hdiutil faked at run_cmd and recorded):
    Ctrl-C during the first ISO's hdiutil detach"""
    fired, detaches = [], []
    first, second = new_disc(case, "JPC_AV_00191"), new_disc(case, "JPC_AV_00192")
    mounts, real_run_cmd = case.mounts, mv.run_cmd

    class AttachedMount(REAL_ISOMOUNT):
        def __enter__(self):
            mount_point = mounts.get(Path(self.iso_path))
            self.mount_points, self.devices = [mount_point], ["/dev/disk42"]
            self.mount_point = mount_point
            return mount_point

    def run_cmd(cmd, *args, **kwargs):
        if [str(c) for c in cmd[:2]] == ["hdiutil", "detach"]:
            detaches.append([str(c) for c in cmd])
            if not fired:
                fired.append("hdiutil detach")
                interrupt()
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run_cmd(cmd, *args, **kwargs)

    return running(case, lambda: case.main_exit("--batch", str(case.root / "isos")),
                   mock.patch.object(mv, "ISOMount", AttachedMount),
                   mock.patch.object(mv, "run_cmd", run_cmd),
                   fired=fired, stems=[first.stem, second.stem], launched=case.tools.launched,
                   detaches=detaches)


@child_scenario
def batch_accounting(case, interrupt):
    """A batch of two new ISOs; Ctrl-C while the first one's outcome is
    counted (bookkeeping the batch holds Ctrl-C over)"""
    fired = []
    first, second = new_disc(case, "JPC_AV_00193"), new_disc(case, "JPC_AV_00194")
    patches = []
    if hasattr(mv, "_count_batch_result"):  # the earlier version has no such step
        patches.append(first_call_interrupting(mv, "_count_batch_result", interrupt, fired))
    return running(case, lambda: case.main_exit("--batch", str(case.root / "isos")), *patches,
                   fired=fired, stems=[first.stem, second.stem], launched=case.tools.launched)


@child_scenario
def reservation_unremovable(case, interrupt):
    """MV-13: Ctrl-C while a failed attempt's kept name is reserved, and
    the empty placeholder then cannot be removed"""
    fired, disc = [], new_disc(case, "JPC_AV_00142", 300.0)
    code = mv.ISOToMP4Converter._retain_attempt.__code__
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if fired and os.fsdecode(path).endswith(".mp4.partial"):
            fired.append("unlink refused")
            raise PermissionError(errno.EACCES, "synthetic: Permission denied", os.fsdecode(path))
        return real_unlink(path, *args, **kwargs)

    return running(case, lambda: case.convert(disc),
                   first_call_interrupting(mv.os, "fstat", interrupt, fired,
                                           lambda fd: sys._getframe(2).f_code is code),
                   mock.patch.object(mv.os, "unlink", unlink),
                   fired=fired, stem=disc.stem)


@child_scenario
def between_titlesets(case, interrupt):
    """N7 (MV-3): Ctrl-C as the second titleset's encode is announced,
    outside any per-titleset handling, after the first one passed"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00119", titlesets=2)
    return running(case, lambda: case.convert(disc),
                   log_interrupting(case, interrupt, fired, "Encoding title 02"),
                   fired=fired, stem=disc.stem)


@child_scenario
def after_the_record_hold(case, interrupt):
    """Ctrl-C the moment the record write's hold has ended, between its
    held steps (found by the code object of the step that holds it)"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00120")
    code = getattr(getattr(mv.ISOToMP4Converter, "_write_run_records", None), "__code__", None)
    real_exit = mv.DeferredInterrupt.__exit__

    def exit_then_interrupt(hold, *exc):
        outcome = real_exit(hold, *exc)
        if not fired and code is not None and sys._getframe(1).f_code is code:
            fired.append("after the record hold")
            interrupt()
        return outcome

    return running(case, lambda: case.convert(disc),
                   mock.patch.object(mv.DeferredInterrupt, "__exit__", exit_then_interrupt),
                   fired=fired, stem=disc.stem)


@child_scenario
def promotion_after_rename(case, interrupt):
    """K2(a): Ctrl-C at the first line event in _promote_titlesets' frame
    once its first rename returned (found by code object and a pass-through
    os.replace, not by line numbers)"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00121", titlesets=2)
    code = mv.ISOToMP4Converter._promote_titlesets.__code__
    real_replace, state = os.replace, {"returned": False}

    def replace(src, dst, *args, **kwargs):
        mine = sys._getframe(1).f_code is code
        real_replace(src, dst, *args, **kwargs)
        if mine:
            state["returned"] = True

    def local(frame, event, arg):
        if state["returned"] and not fired and event == "line":
            fired.append("after the rename")
            interrupt()
        return local

    def tracer(frame, event, arg):
        return local if frame.f_code is code else None

    previous = sys.gettrace()
    with mock.patch.object(mv.os, "replace", replace):
        sys.settrace(tracer)
        try:
            outcome = capture(case, lambda: case.convert(disc))
        finally:
            sys.settrace(previous)
    return dict(outcome, fired=fired, stem=disc.stem)


@child_scenario
def promotion_in_rename(case, interrupt):
    """Ctrl-C inside the first title's promotion rename, once it is done"""
    fired, disc = [], seeded_disc(case, "JPC_AV_00122", titlesets=2)
    final, real_replace = case.mp4(disc, 1), os.replace

    def replace(src, dst, *args, **kwargs):
        real_replace(src, dst, *args, **kwargs)
        if not fired and Path(dst) == final:
            fired.append("in the rename")
            interrupt()

    return running(case, lambda: case.convert(disc), mock.patch.object(mv.os, "replace", replace),
                   fired=fired, stem=disc.stem)


@child_scenario
def reservation(case, interrupt):
    """MV-13: Ctrl-C while a failed attempt's kept name is reserved"""
    fired, disc = [], new_disc(case, "JPC_AV_00141", 300.0)
    code = mv.ISOToMP4Converter._retain_attempt.__code__
    return running(case, lambda: case.convert(disc),
                   first_call_interrupting(mv.os, "fstat", interrupt, fired,
                                           lambda fd: sys._getframe(2).f_code is code),
                   fired=fired, stem=disc.stem)


@child_scenario
def detach_command(case, interrupt):
    """The real ISOMount detaching two volumes; Ctrl-C during the first
    hdiutil detach (faked at run_cmd and only recorded)"""
    volumes = [case.root / "volume1", case.root / "volume2"]
    for volume in volumes:
        volume.mkdir()
    fired, calls = [], []

    def run_cmd(cmd, *args, **kwargs):
        calls.append([str(c) for c in cmd])
        if not fired:
            fired.append("hdiutil detach")
            interrupt()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    mount = REAL_ISOMOUNT(case.root / "JPC_AV_00131.iso")
    mount.mount_points, mount.devices = list(volumes), ["/dev/disk42"]
    outcome = running(case, lambda: mount.__exit__(None, None, None),
                      mock.patch.object(mv, "run_cmd", run_cmd), fired=fired, calls=calls)
    outcome.update(detach_interrupted=getattr(mount, "detach_interrupted", None),
                   detach_errors=list(mount.detach_errors))
    return outcome


class FinalizationTests(ScenarioRunner, Scenario):
    """T-144, T-145: convert() is the one place a run ends. Wherever a
    Ctrl-C lands, none leaves convert() and the ISO ends with truthful
    records; the canonical log and manifest are written to temporary files
    and renamed into place, log first, so neither is left half-written; a
    failure's cause is never replaced; a batch stops with a summary and
    exits 130. Each boundary runs with a real SIGINT in a child and with
    KeyboardInterrupt raised directly, and asserts that its fault fired."""

    def scenario(self, name, mode):
        folder = self.root / f"{name}-{'real' if mode == REAL else 'raised'}"
        outcome = self.run_scenario(name, mode, folder)
        self.assertNotIn("escaped", outcome, f"{outcome.get('escaped')} escaped")
        return outcome, folder

    def run_convert(self, disc):
        try:
            return self.convert(disc)
        except KeyboardInterrupt:
            self.fail("Ctrl-C escaped convert()")

    def run_main(self, *args):
        try:
            return self.main_exit(*args)
        except KeyboardInterrupt:
            self.fail("Ctrl-C escaped main()")

    @staticmethod
    def canonical_records(disc):
        return disc.out / f"{disc.stem}.mp4.log.txt", disc.out / f"{disc.stem}_access_manifest.json"

    def assert_earlier_records(self, disc):
        for path in self.canonical_records(disc):
            self.assertEqual(path.read_bytes(), b"EARLIER CANONICAL " + path.name.encode(),
                             f"{path.name} was changed")

    def assert_no_staging_left(self, disc):
        self.assertEqual(self.glob(disc, "*.writing-*"), [], "a temporary record was left")

    def assert_records_are_this_runs(self, disc):
        """The canonical log and manifest are whole, this run's, and match the MP4"""
        log_path, manifest_path = self.canonical_records(disc)
        manifest, log_text = json.loads(manifest_path.read_text()), log_path.read_text()
        self.assertIn(f"Run ID:           {manifest['conversion_metadata']['run_id']}", log_text)
        self.assertIn("Log generated:", log_text)
        self.assertTrue(log_text.endswith("=" * 70 + "\n"), "the log is not complete")
        self.assertEqual(self.mp4(disc, 1).read_bytes(), self.encoded_bytes(disc, 1))
        self.assertEqual(manifest["output_mp4"]["sha256_checksum"],
                         hashlib.sha256(self.mp4(disc, 1).read_bytes()).hexdigest())
        self.assert_no_staging_left(disc)
        return manifest, log_text

    def assert_stale_warning(self, warnings, disc, *phrases):
        stale = [w for w in warnings if w.startswith(f"this run replaced or created {disc.stem}.mp4")]
        self.assertEqual(len(stale), 1, warnings)
        for phrase in phrases:
            self.assertIn(phrase, stale[0])
        return stale[0]

    @staticmethod
    def summary_line(stem, of):
        return (f"Interrupted by operator (Ctrl-C) at {stem}.iso (1 of {of}); "
                f"{of - 1} ISO(s) not started")

    @contextlib.contextmanager
    def json_dump_failing_once(self):
        """json.dump writes part of the first record it is given, then fails"""
        real, fired = json.dump, []

        def dump(obj, fp, *args, **kwargs):
            if not fired:
                fired.append("json.dump")
                fp.write('{"conversion_metadata": {')
                raise OSError(errno.ENOSPC, "synthetic: No space left on device")
            return real(obj, fp, *args, **kwargs)

        with mock.patch.object(mv.json, "dump", dump):
            yield fired

    # --- N3: a Ctrl-C once every titleset is promoted keeps the outcome ---

    def test_ctrl_c_during_the_detach_after_promotion_keeps_the_outcome(self):
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("detach_after_promotion", mode)
                disc = self.disc_at(folder, outcome["stem"])
                self.assertTrue(outcome["success"] and outcome["interrupted"], outcome)
                manifest, _ = self.assert_records_are_this_runs(disc)
                status = manifest["conversion_status"]
                self.assertEqual(status["overall_status"], "success_with_warnings")
                self.assertIn("Interrupted by operator (Ctrl-C) while the ISO was being detached; "
                              "it may still be attached: check with hdiutil info", status["warnings"])
                self.assertEqual(self.records(disc, "failed"), ([], []))
                self.assertTrue(outcome["handler_restored"])

    def test_ctrl_c_while_the_canonical_log_is_written(self):
        # A real one is held until both records are in place. Raised
        # directly, it fails the write: the earlier records stay whole, and
        # the run's failure records say they no longer match the new MP4
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("canonical_log_body", mode)
                disc = self.disc_at(folder, outcome["stem"])
                self.assertTrue(outcome["interrupted"])
                self.assertEqual(self.mp4(disc, 1).read_bytes(), self.encoded_bytes(disc, 1))
                self.assert_no_staging_left(disc)
                self.assertTrue(outcome["handler_restored"])
                if mode == REAL:
                    self.assertTrue(outcome["success"], outcome)
                    manifest, _ = self.assert_records_are_this_runs(disc)
                    self.assertEqual(manifest["conversion_status"]["overall_status"], "success")
                    self.assertEqual(self.records(disc, "failed"), ([], []))
                    self.assertTrue(any("held until they were finished" in line
                                        for line in outcome["lines"]), outcome["lines"])
                    continue
                self.assertFalse(outcome["success"])
                self.assertTrue(outcome["error"].startswith(
                    "Run records could not be written: interrupted by operator (Ctrl-C)"), outcome)
                self.assert_earlier_records(disc)
                manifest, log_text = self.tokened_manifest(disc, "failed")
                self.assertIn("Log generated:", log_text)
                status = manifest["conversion_status"]
                self.assertEqual(status["canonical_files_changed"], [str(self.mp4(disc, 1))])
                self.assert_stale_warning(status["warnings"], disc,
                                          f"{disc.stem}_access_manifest.json and {disc.stem}.mp4.log.txt "
                                          f"describe an earlier run and no longer match it")

    def test_ctrl_c_while_record_details_are_gathered(self):
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("record_details", mode)
                disc = self.disc_at(folder, outcome["stem"])
                self.assertTrue(outcome["success"] and outcome["interrupted"], outcome)
                manifest, _ = self.assert_records_are_this_runs(disc)
                status = manifest["conversion_status"]
                self.assertEqual(status["overall_status"], "success_with_warnings")
                self.assertIn("video technical information and processor not gathered for the "
                              "records: interrupted by operator (Ctrl-C)", status["warnings"])
                self.assertEqual(manifest["video_technical_info"], {})
                self.assertIsNone(manifest["system_environment"]["processor"])
                self.assertEqual(self.records(disc, "failed"), ([], []))

    def test_ctrl_c_between_the_canonical_record_renames(self):
        # The pair is not one transaction: raised directly between the two
        # renames, the new log is in place beside the earlier manifest, and
        # the failure records say exactly that
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("between_record_renames", mode)
                disc = self.disc_at(folder, outcome["stem"])
                self.assertTrue(outcome["interrupted"])
                self.assert_no_staging_left(disc)
                if mode == REAL:
                    self.assertTrue(outcome["success"], outcome)
                    self.assert_records_are_this_runs(disc)
                    self.assertEqual(self.records(disc, "failed"), ([], []))
                    continue
                self.assertFalse(outcome["success"])
                log_path, manifest_path = self.canonical_records(disc)
                self.assertEqual(manifest_path.read_bytes(),
                                 b"EARLIER CANONICAL " + manifest_path.name.encode())
                manifest, _ = self.tokened_manifest(disc, "failed")
                log_text = log_path.read_text()
                self.assertIn(f"Run ID:           {manifest['conversion_metadata']['run_id']}", log_text)
                self.assertTrue(log_text.endswith("=" * 70 + "\n"))
                self.assert_stale_warning(manifest["conversion_status"]["warnings"], disc,
                                          f"{log_path.name} was written by this run",
                                          f"the existing {manifest_path.name} describes an earlier "
                                          f"run and no longer matches it")

    def test_ctrl_c_once_the_outcome_is_decided_keeps_it(self):
        for name, warning in (("titleset_outcomes", "Interrupted by operator (Ctrl-C) while the "
                                                    "titleset outcomes were being shown"),
                              ("after_records", None)):
            for mode in (REAL, RAISED):
                with self.subTest(name, mode=mode):
                    outcome, folder = self.scenario(name, mode)
                    disc = self.disc_at(folder, outcome["stem"])
                    self.assertTrue(outcome["success"] and outcome["interrupted"], outcome)
                    manifest, _ = self.assert_records_are_this_runs(disc)
                    status = manifest["conversion_status"]
                    if warning:
                        self.assertIn(warning, status["warnings"])
                    else:  # the records were final before the Ctrl-C
                        self.assertEqual(status["overall_status"], "success")
                    self.assertEqual(self.records(disc, "failed"), ([], []))

    def test_ctrl_c_between_the_held_record_steps_is_not_lost(self):
        # Landing just as the record write's hold ends (outside every hold),
        # it is noted and the remaining steps still run
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("after_the_record_hold", mode)
                disc = self.disc_at(folder, outcome["stem"])
                self.assertTrue(outcome["success"] and outcome["interrupted"], outcome)
                self.assert_records_are_this_runs(disc)
                self.assertEqual(self.records(disc, "failed"), ([], []))
                self.assertIn("Conversion completed successfully!", outcome["lines"])

    # --- N7: a failed run keeps its failure records ---

    def test_ctrl_c_between_titlesets_keeps_the_checked_output(self):
        # The run had its result but no outcome yet: it is settled as an
        # interrupted encode is (nothing promoted, the passed first titleset
        # kept as a review copy, the second not attempted)
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("between_titlesets", mode)
                disc = self.disc_at(folder, outcome["stem"])
                self.assertFalse(outcome["success"])
                self.assertTrue(outcome["interrupted"])
                self.assertEqual([t["status"] for t in outcome["titles"]], ["held", "not_attempted"])
                self.assertEqual([p.read_bytes() for p in self.review_copies(disc)],
                                 [self.encoded_bytes(disc, 1)])
                self.assertEqual(self.temps(disc), [])
                self.assertEqual(self.mp4(disc, 1).read_bytes(),
                                 b"EARLIER CANONICAL " + self.mp4(disc, 1).name.encode())
                self.assertFalse(self.mp4(disc, 2).exists())
                self.assert_earlier_records(disc)
                manifest, _ = self.tokened_manifest(disc, "failed")
                status = manifest["conversion_status"]
                self.assertTrue(status["interrupted"])
                self.assertIn("Interrupted by operator (Ctrl-C)", status["error_message"])
                self.assertEqual(status["canonical_files_changed"], [])

    def assert_failure_kept(self, disc, outcome):
        self.assertFalse(outcome["success"])
        self.assertTrue(outcome["interrupted"])
        self.assertIn("ffmpeg failed with return code 1", outcome["error"])
        partials = self.partials(disc)
        self.assertEqual(len(partials), 1)
        self.assertEqual(outcome["titles"][0]["retained_path"], str(partials[0]))
        self.assertEqual(self.mp4(disc, 1).read_bytes(), b"EARLIER CANONICAL " + self.mp4(disc, 1).name.encode())
        self.assert_earlier_records(disc)
        return partials[0]

    def test_failed_run_ctrl_c_during_the_detach_keeps_its_failure_records(self):
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("failed_detach", mode)
                disc = self.disc_at(folder, outcome["stem"])
                partial = self.assert_failure_kept(disc, outcome)
                manifest, log_text = self.tokened_manifest(disc, "failed")
                status = manifest["conversion_status"]
                self.assertTrue(status["interrupted"])
                self.assertIn("ffmpeg failed with return code 1", status["error_message"])
                self.assertIn("Interrupted by operator (Ctrl-C) while the ISO was being detached; "
                              "it may still be attached: check with hdiutil info", status["warnings"])
                self.assertEqual(manifest["titlesets"]["outputs"][0]["partial_renamed_to"], str(partial))
                self.assertTrue(log_text.endswith("=" * 70 + "\n"))

    def test_failed_run_ctrl_c_while_its_records_are_written(self):
        # A real one is held until the failure records are complete. Raised
        # directly, the half-written log is removed and, as nothing else can
        # record a failure, the screen says what the records would have
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("failed_record_log", mode)
                disc = self.disc_at(folder, outcome["stem"])
                partial = self.assert_failure_kept(disc, outcome)
                if mode == REAL:
                    manifest, log_text = self.tokened_manifest(disc, "failed")
                    self.assertIn("ffmpeg failed with return code 1",
                                  manifest["conversion_status"]["error_message"])
                    self.assertTrue(log_text.endswith("=" * 70 + "\n"))
                    self.assertEqual(manifest["titlesets"]["outputs"][0]["partial_renamed_to"],
                                     str(partial))
                    continue
                self.assertEqual(self.records(disc, "failed"), ([], []))
                lines = outcome["lines"]
                self.assertIn("Could not write run records: interrupted by operator (Ctrl-C)", lines)
                self.assertTrue(any(line.startswith("Not recorded: ") and "return code 1" in line
                                    for line in lines), lines)
                self.assertTrue(any(str(partial) in line for line in lines), lines)

    # --- N17: a recorded cause is never replaced ---

    def test_ctrl_c_while_a_failure_is_shown_keeps_its_cause(self):
        cases = (("ffmpeg_error_output", "ffmpeg failed with return code 1",
                  "; then Interrupted by operator (Ctrl-C)"),
                 ("truncation_reason_output", "output is shorter than expected",
                  "; then Interrupted by operator (Ctrl-C) during post-encode verification"))
        for name, cause, then in cases:
            for mode in (REAL, RAISED):
                with self.subTest(name, mode=mode):
                    outcome, folder = self.scenario(name, mode)
                    disc = self.disc_at(folder, outcome["stem"])
                    self.assertTrue(outcome["interrupted"])
                    error = outcome["titles"][0]["error"]
                    self.assertTrue(error.startswith(cause), error)
                    self.assertIn(then, error)
                    manifest, log_text = self.tokened_manifest(disc, "failed")
                    self.assertIn(cause, manifest["conversion_status"]["error_message"])
                    self.assertIn(cause, log_text)
                    self.assertEqual(len(self.partials(disc)), 1)

    # --- N15: the overwrite prompt ---

    def test_end_of_input_at_the_overwrite_prompt_counts_as_no(self):
        first = self.make_disc({1: [4]}, stem="JPC_AV_00151")
        second = self.make_disc({1: [4]}, stem="JPC_AV_00152")
        self.vob_durations(1, 600.0)
        for disc in (first, second):
            self.output(disc, 1, 600.0)
        seeded = self.seed_canonical(first)
        self.answers.clear()
        with mock.patch("builtins.input", side_effect=EOFError) as answer:
            results = mv.process_batch(self.root / "isos", self.root / "isos", "T071")
        self.assertEqual(answer.call_count, 1)
        self.assertEqual((results["failed"], results["success"]), (1, 1))
        self.assertIsNone(results["interrupted"])
        self.assertEqual(self.tools.launched, [self.mp4(second, 1).name], "the batch did not go on")
        self.assert_unchanged(seeded)
        detail = results["details"][0]
        self.assertTrue(detail["aborted"])
        self.assertFalse(detail["interrupted"])
        manifest, _ = self.tokened_manifest(first, "aborted")
        status = manifest["conversion_status"]
        self.assertEqual(status["overall_status"], "aborted")
        self.assertFalse(status["interrupted"])
        self.assertEqual(status["error_message"], "No answer to the overwrite prompt (end of input); "
                                                  "nothing was overwritten")
        self.assertEqual(manifest["archival_notes"]["note"], "No answer to the overwrite prompt "
                         "(end of input), so nothing was encoded or overwritten")
        with mock.patch("builtins.input", side_effect=EOFError) as answer:
            self.assertEqual(self.main_exit("-i", str(first.iso)), 1)
        self.assertEqual(answer.call_count, 1)
        self.assert_unchanged(seeded)

    def test_ctrl_c_at_the_overwrite_prompt_stops_the_batch(self):
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("overwrite_prompt", mode)
                first, second = (self.disc_at(folder, stem) for stem in outcome["stems"])
                self.assertEqual(outcome["exit"], 130)
                self.assertEqual(outcome["launched"], [])
                self.assertFalse(second.out.exists(), "the next ISO was started")
                self.assert_earlier_records(first)
                manifest, _ = self.tokened_manifest(first, "aborted")
                status = manifest["conversion_status"]
                self.assertEqual(status["overall_status"], "aborted")
                self.assertTrue(status["interrupted"])
                self.assertEqual(status["error_message"],
                                 "Cancelled at the overwrite prompt (Ctrl-C); nothing was overwritten")
                self.assertIn("Batch Summary", [line.strip() for line in outcome["lines"]])
                self.assertIn(self.summary_line(first.stem, 2), outcome["lines"])

    # --- the batch and main() ---

    def test_ctrl_c_while_records_are_written_stops_the_batch_with_a_summary(self):
        for mode in (REAL, RAISED):
            with self.subTest("batch", mode=mode):
                outcome, folder = self.scenario("batch_records", mode)
                first, second = (self.disc_at(folder, stem) for stem in outcome["stems"])
                self.assertEqual(outcome["exit"], 130)
                self.assertEqual(outcome["launched"], [self.mp4(first, 1).name])
                self.assertFalse(second.out.exists(), "the next ISO was started")
                self.assertIn(self.summary_line(first.stem, 2), outcome["lines"])
                if mode == REAL:
                    self.assert_records_are_this_runs(first)
                else:
                    manifest, _ = self.tokened_manifest(first, "failed")
                    self.assert_stale_warning(manifest["conversion_status"]["warnings"], first,
                                              "no canonical manifest describes it")
            with self.subTest("single ISO", mode=mode):
                outcome, folder = self.scenario("single_records", mode)
                self.assertEqual(outcome["exit"], 130)

    def test_a_ctrl_c_that_reaches_the_batch_or_main_stops_it(self):
        # A Ctrl-C can leave convert() only before its boundary (between
        # ISOs) or as a further one while an interruption is being settled;
        # simulated here by convert() raising it, before or after entering.
        # The batch stops there with its summary (an ISO that never started
        # is counted as not started, never as failed), and main() exits 130
        first = new_disc(self, "JPC_AV_00181")
        new_disc(self, "JPC_AV_00182")
        isos = self.root / "isos"
        for entered in (False, True):
            with self.subTest("further Ctrl-C after entering" if entered else "between ISOs"):
                calls, self.lines[:] = [], []

                def convert(converter):
                    calls.append(converter.config.iso_name)
                    converter._entered = entered
                    raise KeyboardInterrupt

                with mock.patch.object(mv.ISOToMP4Converter, "convert", convert):
                    try:
                        results = mv.process_batch(isos, isos, "T071")
                        self.assertEqual(self.main_exit("--batch", str(isos)), 130)
                        self.assertEqual(self.main_exit("-i", str(first.iso)), 130)
                    except KeyboardInterrupt:
                        self.fail("Ctrl-C escaped the batch or main()")
                self.assertEqual(calls, [first.stem] * 3, "the batch went on to the next ISO")
                self.assertEqual(results["interrupted"], first.iso.name)
                if entered:
                    self.assertEqual((results["processed"], results["failed"], results["not_started"]),
                                     (1, 1, 1))
                    self.assertTrue(results["details"][0]["interrupted"])
                    self.assertIn(self.summary_line(first.stem, 2), self.lines)
                else:
                    self.assertEqual((results["processed"], results["failed"], results["not_started"],
                                      results["details"]), (0, 0, 2, []))
                    self.assertIn(f"Interrupted by operator (Ctrl-C) before {first.iso.name} started "
                                  f"(0 of 2 processed); 2 ISO(s) not started", self.lines)
        self.assertEqual(self.tools.launched, [])

    def test_help_gives_the_ctrl_c_and_end_of_input_rules(self):
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            mv.print_help()
        text = " ".join(shown.getvalue().split())
        for phrase in ("130 a Ctrl-C stopped the run or batch", 'no answer (end of input) counts as "n"',
                       "CTRL-C:", "renamed into place, log first", "*_failed-<token> records instead",
                       "held until they finish", "takes precedence over 0 and 1",
                       "An outcome already decided (a skip, dry run, unverified result or failure) "
                       "stands", "at the overwrite prompt the ISO is recorded as aborted",
                       "being attached or detached", "an attach cut short is not detached",
                       "never leaves either half-written under its final name",
                       "a power loss, or a volume that does not rename atomically, is not covered"):
            self.assertIn(phrase, text)
        self.assertNotIn("so neither is left half-written", text)

    def test_ctrl_c_before_the_outcome_is_decided_fails_the_run_with_records(self):
        phases = (("dependency_check", "while ffmpeg and ffprobe were being checked"),
                  ("attach", "while the ISO was being attached; it may be attached: "
                             "check with hdiutil info"),
                  ("analysis", "during the disc analysis"))
        for name, phase in phases:
            for mode in (REAL, RAISED):
                with self.subTest(name, mode=mode):
                    outcome, folder = self.scenario(name, mode)
                    disc = self.disc_at(folder, outcome["stem"])
                    self.assertEqual(outcome["exit"], 130)
                    manifest, log_text = self.tokened_manifest(disc, "failed")
                    status = manifest["conversion_status"]
                    self.assertEqual(status["error_message"], f"Interrupted by operator (Ctrl-C) {phase}")
                    self.assertTrue(status["interrupted"])
                    self.assertTrue(log_text.endswith("=" * 70 + "\n"))
                    self.assertEqual(outcome["launched"], [])

    # --- records that cannot be written (decision G2-D3) ---

    def test_records_that_cannot_be_written_after_promotion_stay_whole(self):
        for stem, seed in (("JPC_AV_00161", True), ("JPC_AV_00162", False)):
            with self.subTest("over an earlier bundle" if seed else "first conversion"):
                disc = new_disc(self, stem)
                if seed:
                    self.seed_canonical(disc)
                with self.json_dump_failing_once() as fired:
                    result = self.run_convert(disc)
                self.assertFalse(result.success)
                self.assertTrue(result.error_message.startswith("Run records could not be written: "),
                                result.error_message)
                self.assertIn("No space left on device", result.error_message)
                self.assertEqual(self.mp4(disc, 1).read_bytes(), self.encoded_bytes(disc, 1))
                if seed:
                    self.assert_earlier_records(disc)
                else:
                    self.assertEqual([p.exists() for p in self.canonical_records(disc)], [False, False])
                self.assert_no_staging_left(disc)
                manifest, _ = self.tokened_manifest(disc, "failed")
                status = manifest["conversion_status"]
                self.assertEqual(status["canonical_files_changed"], [str(self.mp4(disc, 1))])
                self.assertTrue(status["error_message"].startswith("Run records could not be written"))
                self.assert_stale_warning(
                    status["warnings"], disc,
                    f"{disc.stem}_access_manifest.json and {disc.stem}.mp4.log.txt describe an "
                    f"earlier run and no longer match it" if seed else
                    "no canonical manifest describes it")
                self.assertTrue(fired)

    def test_a_stale_record_that_cannot_be_checked_is_named_as_such(self):
        disc = new_disc(self, "JPC_AV_00163")
        self.seed_canonical(disc)
        log_path, manifest_path = self.canonical_records(disc)
        error = OSError(errno.EIO, "synthetic: Input/output error")
        real_lstat, checked = os.lstat, []

        def lstat(path, *args, **kwargs):
            # Armed once the manifest write has failed (after promotion): a
            # planned output that cannot be checked before the run is
            # refused there instead (T-150)
            if fired and not isinstance(path, int) and Path(os.fsdecode(path)) == manifest_path:
                checked.append(path)
                raise error
            return real_lstat(path, *args, **kwargs)

        with self.json_dump_failing_once() as fired, mock.patch.object(mv.os, "lstat", lstat):
            result = self.run_convert(disc)
        self.assertFalse(result.success)
        stale = [w for w in result.warnings if w.startswith("this run replaced or created")]
        self.assertEqual(len(stale), 1, result.warnings)
        self.assertIn(f"{manifest_path.name} could not be checked ({error})", stale[0])
        self.assertNotIn("no canonical manifest describes", stale[0])
        self.assertIn(f"the existing {log_path.name} describes an earlier run and no longer "
                      f"matches it", stale[0])
        manifest, _ = self.tokened_manifest(disc, "failed")
        self.assertIn(stale[0], manifest["conversion_status"]["warnings"])
        self.assertTrue(fired)
        self.assertTrue(checked, "the canonical manifest was never checked")

    def test_a_temporary_record_that_cannot_be_removed_is_named(self):
        disc = new_disc(self, "JPC_AV_00164")
        self.seed_canonical(disc)
        real_unlink, refused = os.unlink, []

        def unlink(path, *args, **kwargs):
            if ".writing-" in os.fsdecode(path):
                refused.append(Path(os.fsdecode(path)))
                raise PermissionError(errno.EACCES, "synthetic: Permission denied")
            return real_unlink(path, *args, **kwargs)

        with self.json_dump_failing_once() as fired, mock.patch.object(mv.os, "unlink", unlink):
            result = self.run_convert(disc)
        self.assertFalse(result.success)
        self.assert_earlier_records(disc)
        left = self.glob(disc, "*.writing-*")
        self.assertEqual(len(left), 2, "both temporary records should be left (removal refused)")
        manifest, _ = self.tokened_manifest(disc, "failed")
        warnings = manifest["conversion_status"]["warnings"]
        for path in left:
            self.assertTrue(any(str(path) in w and "could not be removed" in w and "not a record" in w
                                for w in warnings), warnings)
        self.assertTrue(fired and refused)

    def test_failure_records_that_cannot_be_completed_keep_the_complete_log(self):
        # Nothing else can record a failure: the complete failure log is
        # kept, its unfinished manifest removed, and the screen says so
        disc = seeded_disc(self, "JPC_AV_00165", returncode=1)
        with self.json_dump_failing_once() as fired:
            result = self.run_convert(disc)
        self.assertFalse(result.success)
        self.assertIn("ffmpeg failed with return code 1", result.error_message)
        logs, manifests = self.records(disc, "failed")
        self.assertEqual((len(logs), manifests), (1, []))
        self.assertTrue(logs[0].read_text().endswith("=" * 70 + "\n"))
        self.assertIn(f"Written, without the rest of its records: {logs[0]}", self.lines)
        self.assertTrue(any(line.startswith("Not recorded: ") and "return code 1" in line
                            for line in self.lines), self.lines)
        self.assertTrue(fired)

    def test_record_details_that_cannot_be_gathered_are_left_out(self):
        # Optional details: a tool that fails (not only a Ctrl-C) leaves its
        # detail out with a warning naming the cause; the records are
        # written and a batch goes on
        first, second = new_disc(self, "JPC_AV_00166"), new_disc(self, "JPC_AV_00167")
        final = str(self.mp4(first, 1))
        error = BlockingIOError(errno.EAGAIN, "synthetic: Resource temporarily unavailable")
        real_info, fired = mv.get_video_info, []

        def info(target):
            if str(target) == final:
                fired.append(target)
                raise error
            return real_info(target)

        with mock.patch.object(mv, "get_video_info", info):
            results = mv.process_batch(self.root / "isos", self.root / "isos", "T071")
        self.assertEqual((results["success"], results["failed"]), (2, 0))
        self.assertEqual(self.tools.launched, [self.mp4(first, 1).name, self.mp4(second, 1).name])
        manifest, _ = self.assert_records_are_this_runs(first)
        status = manifest["conversion_status"]
        self.assertEqual(status["overall_status"], "success_with_warnings")
        self.assertIn(f"video technical information not gathered for the records: {error}",
                      status["warnings"])
        self.assertEqual(manifest["video_technical_info"], {})
        self.assertTrue(fired)
        # The ffmpeg version, for a run that never checked it (no such ISO)
        missing = self.root / "isos" / "JPC_AV_00168.iso"
        config = mv.ConversionConfig(iso_path=missing, output_dir=self.root / "isos" / "access_JPC_AV_00168",
                                     operator="T071")
        with mock.patch.object(mv, "check_ffmpeg",
                               side_effect=ValueError("synthetic: undecodable version output")) as check:
            result = mv.ISOToMP4Converter(config).convert()
        self.assertTrue(result.error_message.startswith("ISO file not found"), result.error_message)
        disc = self.disc_at(self.root, "JPC_AV_00168")
        manifest, log_text = self.tokened_manifest(disc, "failed")
        self.assertIn("ffmpeg version not gathered for the records: synthetic: undecodable version "
                      "output", manifest["conversion_status"]["warnings"])
        self.assertIn("FFmpeg Version:   None", log_text)
        self.assertEqual(check.call_count, 1)

    def test_end_of_input_at_the_operator_prompt_exits_1(self):
        disc = new_disc(self, "JPC_AV_00169")
        with mock.patch.object(sys, "argv", ["makeiso-video.py", "-i", str(disc.iso)]), \
                mock.patch("builtins.input", side_effect=EOFError) as answer:
            with self.assertRaises(SystemExit) as caught:
                mv.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("\nNo answer (end of input): cancelled.", self.lines)
        self.assertEqual(self.tools.launched, [])
        self.assertFalse(disc.out.exists())
        self.assertEqual(answer.call_count, 1)

    # --- the exit rule: 130 after a Ctrl-C, unless an ffmpeg was not confirmed stopped ---

    def test_ctrl_c_during_the_real_detach_stops_the_batch(self):
        # The real ISOMount records a Ctrl-C during hdiutil detach instead of
        # raising it; the run takes it up (interrupted), so the batch stops,
        # exits 130, and the ISO's records name the detach
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("real_detach_batch", mode)
                first, second = (self.disc_at(folder, stem) for stem in outcome["stems"])
                self.assertEqual(outcome["exit"], 130)
                self.assertEqual(outcome["launched"], [self.mp4(first, 1).name])
                self.assertFalse(second.out.exists(), "the next ISO was started")
                self.assertEqual(len(outcome["detaches"]), 1, "detaching went on after the Ctrl-C")
                self.assertIn(self.summary_line(first.stem, 2), outcome["lines"])
                manifest, _ = self.assert_records_are_this_runs(first)
                status = manifest["conversion_status"]
                self.assertEqual(status["overall_status"], "success_with_warnings")
                self.assertTrue(any(w.startswith(f"unmount of {first.iso.name}: interrupted by operator "
                                                 f"(Ctrl-C); it may still be attached")
                                    and "check with hdiutil info" in w for w in status["warnings"]),
                                status["warnings"])

    def test_ctrl_c_while_an_iso_is_counted_stops_the_batch_after_it(self):
        # The batch's own bookkeeping holds Ctrl-C: the ISO is counted, the
        # summary still prints, and no further ISO is started
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, folder = self.scenario("batch_accounting", mode)
                first, second = (self.disc_at(folder, stem) for stem in outcome["stems"])
                self.assertEqual(outcome["exit"], 130)
                self.assertEqual(outcome["launched"], [self.mp4(first, 1).name])
                self.assertFalse(second.out.exists(), "the next ISO was started")
                for line in (self.summary_line(first.stem, 2), "Total processed: 1",
                             "Successfully converted: 1"):
                    self.assertIn(line, outcome["lines"])
                self.assert_records_are_this_runs(first)

    def test_an_interrupted_run_whose_ffmpeg_was_not_confirmed_stopped_exits_1(self):
        unstoppable = dict(live=True, data=b"LAUNCH", never_exits=True, interrupt=True)
        first = new_disc(self, "JPC_AV_00183", **unstoppable)
        second = new_disc(self, "JPC_AV_00184")
        with self.subTest("batch"):
            self.assertEqual(self.run_main("--batch", str(self.root / "isos")), 1)
            self.assertEqual(self.tools.launched, [self.mp4(first, 1).name])
            self.assertFalse(second.out.exists(), "the next ISO was started")
            status = self.tokened_manifest(first, "failed")[0]["conversion_status"]
            self.assertTrue(status["interrupted"] and status["cleanup_incomplete"], status)
            self.assertIn("Batch interrupted by operator — stopping.", self.lines)
            self.assertTrue(any(line.startswith("Batch stopped: an ffmpeg could not be confirmed")
                                for line in self.lines), self.lines)
        with self.subTest("single ISO"):
            third = new_disc(self, "JPC_AV_00185", **unstoppable)
            self.assertEqual(self.run_main("-i", str(third.iso)), 1)
            status = self.tokened_manifest(third, "failed")[0]["conversion_status"]
            self.assertTrue(status["interrupted"] and status["cleanup_incomplete"], status)
        self.assertFalse(any("exit status 130" in line for line in self.lines), self.lines)

    def test_the_ctrl_c_backstops_keep_exit_1_for_an_ffmpeg_not_confirmed_stopped(self):
        unstoppable = dict(live=True, data=b"LAUNCH", never_exits=True, interrupt=True)
        first = new_disc(self, "JPC_AV_00186", **unstoppable)
        new_disc(self, "JPC_AV_00187")
        real_convert, calls = mv.ISOToMP4Converter.convert, []

        def convert_then_interrupt(converter):
            calls.append(converter.config.iso_name)
            real_convert(converter)
            raise KeyboardInterrupt  # a further Ctrl-C once the run is recorded

        with mock.patch.object(mv.ISOToMP4Converter, "convert", convert_then_interrupt):
            for args in (("--batch", str(self.root / "isos")), ("-i", str(first.iso))):
                with self.subTest(args[0]):
                    self.assertEqual(self.run_main(*args), 1)
        self.assertEqual(calls, [first.stem] * 2, "the batch went on to the next ISO")
        self.assertIn(self.summary_line(first.stem, 2), self.lines)
        self.assertEqual(sum("An ffmpeg could not be confirmed stopped (see above): exit status 1."
                             in line for line in self.lines), 1, self.lines)
        with self.subTest("Ctrl-C while the batch says the ffmpeg was not confirmed stopped"):
            self.root = self.root / "stopped"
            self.root.mkdir()
            third = new_disc(self, "JPC_AV_00188", live=True, data=b"LAUNCH", never_exits=True,
                             stderr_error=EncoderLifetimeTests.READ_ERROR)
            new_disc(self, "JPC_AV_00189")
            fired, lines = [], self.lines

            def log(message):
                lines.append(str(message))
                if not fired and str(message).startswith("Batch stopped:"):
                    fired.append(message)
                    raise KeyboardInterrupt

            with mock.patch.object(mv, "log", log):
                self.assertEqual(self.run_main("--batch", str(self.root / "isos")), 1)
            self.assertIn("Batch Summary", [line.strip() for line in lines])
            self.assertIn(self.summary_line(third.stem, 2), lines)
            self.assertTrue(fired)

    # --- healthy controls ---

    def test_control_a_healthy_run_over_an_earlier_bundle(self):
        disc = seeded_disc(self, "JPC_AV_00171")
        handler = signal.getsignal(signal.SIGINT)
        result = self.run_convert(disc)
        self.assertTrue(result.success, result.error_message)
        self.assertFalse(result.interrupted)
        manifest, _ = self.assert_records_are_this_runs(disc)
        self.assertEqual(manifest["conversion_status"]["overall_status"], "success")
        self.assertEqual(self.records(disc, "failed"), ([], []))
        self.assertIs(signal.getsignal(signal.SIGINT), handler, "the SIGINT handler was not restored")
        other = new_disc(self, "JPC_AV_00172")
        self.assertEqual(self.main_exit("-i", str(other.iso)), 0)
        self.assert_records_are_this_runs(other)

    def test_control_a_first_conversion_leaves_no_staging(self):
        disc = new_disc(self, "JPC_AV_00173")
        self.assertEqual(self.main_exit("-i", str(disc.iso)), 0)
        self.assert_records_are_this_runs(disc)
        self.assertEqual(sorted(p.name for p in disc.out.iterdir()),
                         sorted([f"{disc.stem}.mp4", f"{disc.stem}.mp4.log.txt",
                                 f"{disc.stem}_access_manifest.json"]))


class PromotionWindowTests(ScenarioRunner, Scenario):
    """T-146 (decision G2-D1): Ctrl-C is held across the promotion renames,
    so a real one never stops promotion partway: the run keeps its outcome,
    with the interruption as a warning. One raised directly (as off the main
    thread) still stops promotion, once where the output is has been
    settled by device and inode; a failed check is never taken as absence.
    The earlier version let the Ctrl-C escape, stopped promotion partway,
    or recorded a title as promoted that was not."""

    def scenario(self, name, mode):
        folder = self.root / f"{name}-{'real' if mode == REAL else 'raised'}"
        outcome = self.run_scenario(name, mode, folder)
        self.assertNotIn("escaped", outcome, f"{outcome.get('escaped')} escaped")
        return outcome, self.disc_at(folder, outcome["stem"])

    def run_convert(self, disc):
        try:
            return self.convert(disc)
        except KeyboardInterrupt:
            self.fail("Ctrl-C escaped convert()")

    def assert_promotion_finished(self, outcome, disc):
        self.assertTrue(outcome["success"] and outcome["interrupted"], outcome)
        self.assertEqual([(t["status"], t["promoted"]) for t in outcome["titles"]],
                         [("completed", True), ("completed", True)])
        for vts in (1, 2):
            self.assertEqual(self.mp4(disc, vts).read_bytes(), self.encoded_bytes(disc, vts))
        manifest = json.loads((disc.out / f"{disc.stem}_access_manifest.json").read_text())
        status = manifest["conversion_status"]
        self.assertEqual(status["canonical_files_changed"],
                         [str(self.mp4(disc, 1)), str(self.mp4(disc, 2))])
        self.assertIn("Interrupted by operator (Ctrl-C) during promotion; it was held until "
                      "every titleset was promoted", status["warnings"])
        self.assertEqual(self.temps(disc) + self.review_copies(disc), [])
        self.assertEqual(self.records(disc, "failed"), ([], []))

    def assert_stopped_after_the_first(self, outcome, disc):
        self.assertFalse(outcome["success"])
        self.assertTrue(outcome["interrupted"])
        self.assertEqual([(t["status"], t["promoted"]) for t in outcome["titles"]],
                         [("completed", True), ("held", False)])
        self.assertEqual(self.mp4(disc, 1).read_bytes(), self.encoded_bytes(disc, 1))
        self.assertFalse(self.mp4(disc, 2).exists())
        self.assertEqual([p.read_bytes() for p in self.review_copies(disc)], [self.encoded_bytes(disc, 2)])
        manifest, _ = self.tokened_manifest(disc, "failed")
        status = manifest["conversion_status"]
        self.assertEqual(status["promotion_error"], "Interrupted by operator (Ctrl-C) during promotion")
        self.assertEqual(status["canonical_files_changed"], [str(self.mp4(disc, 1))])
        self.assertTrue(any("describe an earlier run and no longer match it" in w
                            for w in status["warnings"]), status["warnings"])
        self.assertEqual(self.temps(disc), [])

    def test_a_ctrl_c_just_after_a_rename_is_held_until_promotion_finishes(self):
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, disc = self.scenario("promotion_after_rename", mode)
                if mode == REAL:
                    self.assert_promotion_finished(outcome, disc)
                else:  # reconciled by device and inode: the first title is promoted
                    self.assert_stopped_after_the_first(outcome, disc)

    def test_a_ctrl_c_inside_a_rename_is_held_until_promotion_finishes(self):
        for mode in (REAL, RAISED):
            with self.subTest(mode):
                outcome, disc = self.scenario("promotion_in_rename", mode)
                if mode == REAL:
                    self.assert_promotion_finished(outcome, disc)
                else:
                    self.assert_stopped_after_the_first(outcome, disc)

    def test_an_interrupted_rename_with_an_unreadable_temp_is_not_promoted(self):
        # K2(c): the rename is interrupted before it happens, and the temp
        # then cannot be checked once: never recorded as promoted
        disc = new_disc(self, "JPC_AV_00123")
        seeded = self.seed_canonical(disc)
        state = {"armed": False, "fired": 0, "raised": False}
        real_replace, real_lstat = os.replace, os.lstat

        def replace(src, dst, *args, **kwargs):
            if not state["raised"] and Path(dst) == self.mp4(disc, 1):
                state["raised"] = state["armed"] = True
                raise KeyboardInterrupt  # before any rename
            return real_replace(src, dst, *args, **kwargs)

        def lstat(path, *args, **kwargs):
            if state["armed"] and not isinstance(path, int) and TEMP_NAME.search(os.fsdecode(path)):
                state["armed"], state["fired"] = False, state["fired"] + 1
                raise OSError(errno.EIO, "synthetic: Input/output error", os.fsdecode(path))
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(mv.os, "replace", replace), mock.patch.object(mv.os, "lstat", lstat):
            result = self.run_convert(disc)
        title = result.titles[0]
        self.assertFalse(title.promoted_this_run)
        self.assertEqual(result.canonical_files_changed, [])
        self.assert_unchanged(seeded)
        self.assertTrue(state["raised"] and state["fired"], state)
        self.assertTrue(result.interrupted)
        self.assertFalse(result.success)
        # The record names where the output really is: kept as a review copy
        self.assertEqual(title.retained_as, "review_copy")
        self.assertEqual(title.retained_path.read_bytes(), self.encoded_bytes(disc, 1))
        self.assertEqual(self.temps(disc), [])
        manifest, _ = self.tokened_manifest(disc, "failed")
        record = manifest["titlesets"]["outputs"][0]
        self.assertEqual((record["promoted_this_run"], record["retained_as"]), (False, "review_copy"))
        self.assertFalse(any("no longer match" in w for w in manifest["conversion_status"]["warnings"]))

    def test_an_interrupted_rename_whose_final_name_cannot_be_checked_is_not_kept_at_its_temp(self):
        # The rename happened, then KeyboardInterrupt is raised directly and
        # the final name cannot be checked: the location is unverified, the
        # final MP4 is named as possibly replaced, and the temp name (found
        # not to hold the output) is never recorded as where it is
        disc = new_disc(self, "JPC_AV_00126")
        self.seed_canonical(disc)
        final = self.mp4(disc, 1)
        state = {"raised": False, "armed": False, "fired": 0, "temp": None}
        real_replace, real_lstat = os.replace, os.lstat

        def replace(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if not state["raised"] and Path(dst) == final:
                state.update(raised=True, armed=True, temp=Path(src))
                raise KeyboardInterrupt  # after the rename itself

        def lstat(path, *args, **kwargs):
            if state["armed"] and not isinstance(path, int) and Path(os.fsdecode(path)) == final:
                state["armed"], state["fired"] = False, state["fired"] + 1
                raise OSError(errno.EIO, "synthetic: Input/output error", os.fsdecode(path))
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(mv.os, "replace", replace), mock.patch.object(mv.os, "lstat", lstat):
            result = self.run_convert(disc)
        title = result.titles[0]
        self.assertEqual(final.read_bytes(), self.encoded_bytes(disc, 1), "the rename did happen")
        self.assertTrue(title.location_unverified)
        self.assertIsNone(title.retained_path, "a name found not to hold the output was recorded")
        for part in (str(state["temp"]), str(final), "synthetic: Input/output error"):
            self.assertIn(part, title.retention_error)
        self.assertTrue(any(w.startswith(f"{final.name} may have been replaced by this run")
                            for w in result.warnings), result.warnings)
        manifest, log_text = self.tokened_manifest(disc, "failed")
        record = manifest["titlesets"]["outputs"][0]
        self.assertEqual((record["retained_path"], record["retained_as"], record["location_unverified"]),
                         (None, None, True))
        self.assertNotIn(f"Kept as:  {state['temp']}", log_text)
        # Never "nothing promoted" when the final name may hold this run's output
        self.assertIn(f"Final MP4s:       none confirmed promoted or changed by this run; "
                      f"{final.name} may have been replaced (could not be checked)", log_text)
        self.assertNotIn("none promoted or changed by this run", log_text)
        self.assertIn(f"Final MP4s this run may have replaced (could not be checked): {final.name}",
                      self.lines)
        self.assertFalse(any(line.startswith("Nothing was promoted") for line in self.lines), self.lines)
        self.assertTrue(state["fired"])

    def test_a_temp_that_cannot_be_checked_is_not_renamed(self):
        disc = seeded_disc(self, "JPC_AV_00124", titlesets=2)
        seeded = {self.mp4(disc, 1): b"EARLIER CANONICAL " + self.mp4(disc, 1).name.encode()}
        state = {"armed": False, "fired": 0}
        real_promote, real_lstat = mv.ISOToMP4Converter._promote_titlesets, os.lstat

        def promote(converter, result):
            state["armed"] = True
            return real_promote(converter, result)

        def lstat(path, *args, **kwargs):
            if state["armed"] and not state["fired"] and not isinstance(path, int) \
                    and TEMP_NAME.search(os.fsdecode(path)):
                state["fired"] += 1
                raise OSError(errno.EIO, "synthetic: Input/output error", os.fsdecode(path))
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(mv.ISOToMP4Converter, "_promote_titlesets", promote), \
                mock.patch.object(mv.os, "lstat", lstat):
            result = self.run_convert(disc)
        self.assertEqual(result.canonical_files_changed, [])
        self.assert_unchanged(seeded)
        self.assertFalse(self.mp4(disc, 2).exists())
        self.assertEqual([t.status for t in result.titles], ["held", "held"])
        self.assertIn("could not check", result.promotion_error or "")
        self.assertIn("not promoted", result.promotion_error)
        self.assertEqual(len(self.review_copies(disc)), 2)
        self.assertTrue(state["fired"])
        manifest, _ = self.tokened_manifest(disc, "failed")
        self.assertEqual(manifest["conversion_status"]["promotion_error"], result.promotion_error)

### === INSPECTION, CLASSIFICATION AND INVOCATION (T-150, T-151, T-155) ===


@contextlib.contextmanager
def stat_failing(wanted, code, calls=("os.stat", "os.lstat", "Path.stat", "Path.lstat")):
    """The named stat calls fail with errno code (an OSError naming the
    path, as a real one does) for the paths wanted(path) selects, and
    delegate everything else: os.stat and os.lstat at module level (what
    the script's own checks call, on every interpreter) and Path.stat and
    Path.lstat at class level (what pathlib's predicates call; on 3.9 they
    never reach os.stat, which pathlib bound at import). Yields the
    (call, path) pairs it failed."""
    fired = []

    def failing(label, real):
        def call(path, *args, **kwargs):
            if not isinstance(path, int) and wanted(Path(os.fsdecode(path))):
                fired.append((label, Path(os.fsdecode(path))))
                raise OSError(code, f"synthetic: {os.strerror(code)}", os.fsdecode(path))
            return real(path, *args, **kwargs)
        return call

    owners = {"os.stat": (os, "stat"), "os.lstat": (os, "lstat"),
              "Path.stat": (Path, "stat"), "Path.lstat": (Path, "lstat")}
    with contextlib.ExitStack() as stack:
        for label in calls:
            owner, name = owners[label]
            stack.enter_context(mock.patch.object(owner, name, failing(label, getattr(owner, name))))
        yield fired


class InspectionTests(Scenario):
    """T-150: only "no such file" means absent. A check of the source ISO, a
    VOB/IFO/BUP file, a volume's VIDEO_TS, a planned output, an encoded
    output or a final name that fails otherwise stops that step with a
    recorded cause, the same on every interpreter: no content is silently
    left out and no existing access copy is replaced without the prompt.
    T-155: a VIDEO_TS without VOB files is a truthful skip (N21) and a gap
    in content VOB numbering is an advisory warning (A1). Faults go through
    the combined stat seam (stat_failing), and each asserts that it fired."""

    def run_convert(self, disc):
        try:
            return self.convert(disc)
        except OSError as e:
            self.fail(f"an inspection error escaped convert(): {e!r}")

    def assert_refused_before_encoding(self, result, disc, prefix, *phrases):
        """Failed with the cause recorded, nothing encoded, failure records written"""
        self.assertFalse(result.success or result.skipped or result.aborted, result.error_message)
        self.assertTrue((result.error_message or "").startswith(prefix), result.error_message)
        for phrase in phrases:
            self.assertIn(phrase, result.error_message)
        self.assertEqual(self.tools.launched, [], "something was encoded")
        manifest, log_text = self.tokened_manifest(disc, "failed")
        self.assertEqual(manifest["conversion_status"]["overall_status"], "error")
        self.assertEqual(manifest["conversion_status"]["error_message"], result.error_message)
        self.assertIn(result.error_message, log_text)
        return manifest

    # --- the C2 helper ---

    def test_only_no_such_file_counts_as_absent(self):
        target = self.root / "checked"
        target.write_bytes(b"synthetic")
        for follow in (False, True):
            for code in (errno.ENOENT, errno.EACCES, errno.EIO, errno.ENOTDIR, errno.ELOOP):
                with self.subTest(errno.errorcode[code], follow_symlinks=follow):
                    with stat_failing(lambda p: p == target, code, ("os.stat", "os.lstat")) as fired:
                        found, problem = (mv.inspect_path(target, follow_symlinks=True) if follow
                                          else mv.inspect_path(target))
                    self.assertEqual(fired, [("os.stat" if follow else "os.lstat", target)])
                    self.assertIsNone(found)
                    if code == errno.ENOENT:
                        self.assertIsNone(problem)
                    else:
                        self.assertIn(os.strerror(code), problem or "")
            with self.subTest("control: present", follow_symlinks=follow):
                found, problem = (mv.inspect_path(target, follow_symlinks=True) if follow
                                  else mv.inspect_path(target))
                self.assertEqual((found.st_size, problem), (9, None))

    # --- N4: the disc inventory ---

    def test_a_vob_that_cannot_be_checked_stops_the_analysis(self):
        # mv2_vob: the VIDEO_TS listing works, one VOB's check fails
        disc = self.make_disc({1: [3, 2, 3]}, ifos={1: straight_ifo([200, 100, 200], [3, 2, 3])})
        self.vob_durations(1, 200.0, 100.0, 200.0)
        self.output(disc, 1, 400.0)
        vob = disc.video_ts / "VTS_01_2.VOB"
        with stat_failing(lambda p: p == vob, errno.EIO) as fired:
            result = self.run_convert(disc)
        self.assertTrue(fired, "the fault never fired")
        manifest = self.assert_refused_before_encoding(result, disc, "Analysis error", str(vob),
                                                       "Input/output error")
        self.assertIn(str(vob), manifest["disc_analysis"]["error"])
        self.assertEqual(self.temps(disc) + self.partials(disc), [])
        self.assertFalse(self.mp4(disc, 1).exists())

    def test_video_ts_entries_that_cannot_be_checked_stop_the_analysis(self):
        disc = self.make_disc({1: [4, 4]}, ifos={1: straight_ifo([200, 400], [4, 4])})
        with stat_failing(lambda p: p.parent == disc.video_ts, errno.EACCES) as fired:
            result = self.run_convert(disc)
        self.assertTrue(fired, "the fault never fired")
        self.assert_refused_before_encoding(result, disc, "Analysis error", "Permission denied")
        named = [p for p in disc.video_ts.iterdir() if str(p) in result.error_message]
        self.assertTrue(named, f"no VIDEO_TS entry named: {result.error_message}")

    def test_a_vob_listed_and_then_not_found_stops_the_analysis(self):
        # Strict C2 (G4-D2): a listed VOB that is then "not found" means the
        # inventory cannot be trusted; it is never silently left out
        disc = self.make_disc({1: [3, 2, 3]})
        self.vob_durations(1, 200.0, 100.0, 200.0)
        self.output(disc, 1, 400.0)
        vob = disc.video_ts / "VTS_01_2.VOB"
        with stat_failing(lambda p: p == vob, errno.ENOENT) as fired:
            result = self.run_convert(disc)
        self.assertTrue(fired, "the fault never fired")
        self.assert_refused_before_encoding(result, disc, "Analysis error", str(vob), "not found")

    def test_a_volume_that_cannot_be_checked_for_video_ts_fails_the_run(self):
        # The real ISOMount, attaching two volumes (hdiutil faked at run_cmd
        # and only recorded): the second's VIDEO_TS cannot be checked, so
        # the right volume is unknown; never the data volume by default
        disc = new_disc(self, "JPC_AV_00151")
        data_volume, video_volume = self.root / "volumes" / "data", self.mounts[disc.iso]
        data_volume.mkdir(parents=True)
        (data_volume / "README.TXT").write_text("synthetic data volume\n")
        plist = plistlib.dumps({"system-entities": [
            {"mount-point": str(data_volume), "dev-entry": "/dev/disk42s1"},
            {"mount-point": str(video_volume), "dev-entry": "/dev/disk42s2"}]}).decode()
        real_run_cmd, hdiutil = mv.run_cmd, []

        def run_cmd(cmd, *args, **kwargs):
            if cmd[0] == "hdiutil":
                hdiutil.append([str(c) for c in cmd])
                return subprocess.CompletedProcess(cmd, 0, plist if cmd[1] == "attach" else "", "")
            return real_run_cmd(cmd, *args, **kwargs)

        target = video_volume / "VIDEO_TS"
        with mock.patch.object(mv, "ISOMount", REAL_ISOMOUNT), mock.patch.object(mv, "run_cmd", run_cmd), \
                stat_failing(lambda p: p == target, errno.EIO) as fired:
            result = self.run_convert(disc)
        self.assertTrue(fired, "the fault never fired")
        self.assert_refused_before_encoding(result, disc, "Analysis error: could not check volume",
                                            str(video_volume), "Input/output error")
        self.assertEqual([c[1:3] for c in hdiutil if c[1] == "detach"],
                         [["detach", str(data_volume)], ["detach", str(video_volume)]])

    def test_vob_sizes_that_cannot_be_read_are_never_left_out(self):
        # The expected-duration bitrate check sums every content VOB: one
        # that cannot be read raises (the titleset then fails) - it is never
        # silently left out of the sum
        vobs = []
        for index in (1, 2):
            vob = self.root / f"VTS_01_{index}.VOB"
            with open(vob, "wb") as f:
                f.truncate(4 * SECTOR)
            vobs.append(vob)
        with stat_failing(lambda p: p == vobs[1], errno.EIO, ("os.stat", "Path.stat")) as fired:
            with self.assertRaises(OSError):
                mv.expected_titleset_duration(vobs, None)
        self.assertTrue(fired, "the fault never fired")

    # --- N4: the source ISO and the planned outputs ---

    def test_a_source_iso_that_cannot_be_checked_is_an_inspection_error(self):
        disc = new_disc(self, "JPC_AV_00152")
        with stat_failing(lambda p: p == disc.iso, errno.EIO) as fired:
            result = self.run_convert(disc)
        self.assertTrue(fired, "the fault never fired")
        self.assert_refused_before_encoding(result, disc, f"Inspection error: could not check ISO {disc.iso}",
                                            "Input/output error")
        self.assertEqual(self.mount_objects, [], "the ISO was attached")
        # Control: a symlink to the ISO is followed and converts
        link = disc.iso.with_name("JPC_AV_00153.iso")
        os.symlink(disc.iso, link)
        self.mounts[link] = self.mounts[disc.iso]
        linked = types.SimpleNamespace(iso=link, video_ts=disc.video_ts, stem="JPC_AV_00153",
                                       out=link.parent / "access_JPC_AV_00153")
        self.output(linked, 1, 600.0)
        result = self.run_convert(linked)
        self.assertTrue(result.success, result.error_message)

    def test_an_iso_given_with_i_that_cannot_be_checked_stops_main(self):
        # main()'s own checks of -i: the path and the error are named (never
        # "not found", never a traceback) before anything is started or
        # recorded. Control: a missing ISO is still "not found"
        disc = new_disc(self, "JPC_AV_00158")
        folder = self.root / "given"
        folder.mkdir()
        inner = folder / "JPC_AV_00159.iso"
        inner.write_bytes(b"SYNTHETIC ISO PLACEHOLDER")
        cases = (("ISO, EIO", disc.iso, disc.iso, errno.EIO),
                 ("ISO, EACCES", disc.iso, disc.iso, errno.EACCES),
                 ("the ISO in a folder given with -i", folder, inner, errno.EIO))
        for label, given, target, code in cases:
            with self.subTest(label):
                self.lines.clear()
                with stat_failing(lambda p, t=target: p == t, code) as fired:
                    try:
                        status = self.main_exit("-i", str(given))
                    except OSError as e:
                        self.fail(f"an inspection error escaped main(): {e!r}")
                self.assertTrue(fired, "the fault never fired")
                self.assertEqual(status, 1)
                errors = [line for line in self.lines if line.startswith("Error: ")]
                self.assertEqual(len(errors), 1, self.lines)
                self.assertTrue(errors[0].startswith(f"Error: could not check {target}: "), errors[0])
                self.assertIn(os.strerror(code), errors[0])
                self.assertNotIn("not found", errors[0])
                self.assertEqual(self.mount_objects, [], "the ISO was attached")
                self.assertEqual(sorted(self.root.rglob("*_manifest.json")), [], "records were written")
        with self.subTest("control: a missing ISO"):
            self.lines.clear()
            missing = self.root / "isos" / "JPC_AV_00160.iso"
            self.assertEqual(self.main_exit("-i", str(missing)), 1)
            self.assertIn(f"Error: ISO file not found: {missing}", self.lines)

    def test_an_output_that_cannot_be_checked_fails_before_the_prompt(self):
        # mv2_canon and the other planned outputs: an existing access copy
        # is never replaced without the prompt, and nothing escapes
        targets = (("canonical MP4", (1,), lambda d: self.mp4(d, 1)),
                   ("canonical log", (1,), lambda d: d.out / f"{d.stem}.mp4.log.txt"),
                   ("canonical manifest", (1,), lambda d: d.out / f"{d.stem}_access_manifest.json"),
                   ("second titleset's MP4", (1, 2), lambda d: self.mp4(d, 2)))
        for n, (label, titlesets, target) in enumerate(targets):
            with self.subTest(label):
                disc = self.make_disc({vts: [4] for vts in titlesets}, stem=f"JPC_AV_0016{n}")
                seeded = self.seed_canonical(disc, vts_list=titlesets)
                for vts in titlesets:
                    self.vob_durations(vts, 600.0)
                    self.output(disc, vts, 600.0)
                self.answers[:] = ["n"]  # were the prompt shown, it would be declined
                self.tools.launched.clear()
                path = target(disc)
                with stat_failing(lambda p: p == path, errno.EIO) as fired:
                    result = self.run_convert(disc)
                self.assertTrue(fired, "the fault never fired")
                self.assertEqual(self.answers, ["n"], "the overwrite prompt was shown")
                self.assert_refused_before_encoding(
                    result, disc, f"Inspection error: could not check planned output {path.name}",
                    "Input/output error")
                self.assert_unchanged(seeded)

    # --- N4: after ffmpeg, and at promotion ---

    def test_a_temporary_name_that_cannot_be_checked_is_never_taken_as_free(self):
        disc = new_disc(self, "JPC_AV_00154")
        with stat_failing(lambda p: TEMP_NAME.search(p.name) is not None, errno.EIO,
                          ("os.stat", "Path.stat")) as fired:
            result = self.run_convert(disc)
        self.assertTrue(fired, "the fault never fired")
        title = result.titles[0]
        self.assertEqual(title.status, "failed")
        self.assertIn("could not prepare the encode", title.error)
        self.assertIn("Input/output error", title.error)
        self.assertEqual(self.tools.launched, [], "ffmpeg was started on a name that could not be checked")
        self.assertEqual(self.temps(disc), [])

    def test_an_output_that_cannot_be_checked_after_ffmpeg_is_failed_and_located(self):
        # Observation 12: never recorded as "not created", and the record
        # names where the output may be
        disc = new_disc(self, "JPC_AV_00155")
        armed, real_popen = [], self.tools.popen

        def popen(cmd, **kwargs):
            process = real_popen(cmd, **kwargs)
            armed.append(cmd[-1])
            return process

        with mock.patch.object(subprocess, "Popen", popen), \
                stat_failing(lambda p: bool(armed) and TEMP_NAME.search(p.name) is not None,
                             errno.EIO) as fired:
            result = self.run_convert(disc)
        self.assertTrue(armed and fired, "the fault never fired")
        title = result.titles[0]
        self.assertEqual(title.status, "failed")
        self.assertIn("Input/output error", title.error)
        self.assertNotIn("not created", title.error)
        self.assertTrue(title.location_unverified)
        temps = self.temps(disc)
        self.assertEqual([p.read_bytes() for p in temps], [self.encoded_bytes(disc, 1)])
        self.assertIn(str(temps[0]), title.retention_error)
        manifest, log_text = self.tokened_manifest(disc, "failed")
        record = manifest["titlesets"]["outputs"][0]
        self.assertTrue(record["location_unverified"])
        self.assertIn(str(temps[0]), record["retention_error"])
        self.assertFalse(self.mp4(disc, 1).exists())
        # Control: an unknown location before promotion never touched the
        # final name, so nothing is named as possibly replaced
        self.assertIn("Final MP4s:       none promoted or changed by this run\n", log_text)
        self.assertNotIn("may have been replaced", log_text)

    def test_a_final_name_that_cannot_be_checked_stops_promotion_before_its_rename(self):
        # G4-D3: title 1 is promoted; title 2's final name then cannot be
        # checked, so promotion stops before its rename (T-071's stop path)
        disc = self.make_disc({1: [4], 2: [4]}, stem="JPC_AV_00156")
        seeded = self.seed_canonical(disc, vts_list=(1, 2))
        for vts in (1, 2):
            self.vob_durations(vts, 600.0)
            self.output(disc, vts, 600.0)
        title02, real_replace, renamed = self.mp4(disc, 2), os.replace, []

        def replace(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if Path(dst) == self.mp4(disc, 1):
                renamed.append(dst)

        with mock.patch.object(mv.os, "replace", replace), \
                stat_failing(lambda p: bool(renamed) and p == title02, errno.EIO,
                             ("os.lstat", "Path.lstat")) as fired:
            result = self.run_convert(disc)
        self.assertTrue(renamed and fired, "the fault never fired")
        self.assertFalse(result.success)
        self.assertIn(title02.name, result.promotion_error)
        self.assertIn("Input/output error", result.promotion_error)
        first, second = result.titles
        self.assertEqual((first.status, first.promoted_this_run), ("completed", True))
        self.assertEqual((second.status, second.promoted_this_run, second.retained_as),
                         ("held", False, "review_copy"))
        self.assertEqual(title02.read_bytes(), seeded[title02], "title02 was replaced")
        self.assertEqual(second.retained_path.read_bytes(), self.encoded_bytes(disc, 2))
        self.assertEqual(result.canonical_files_changed, [self.mp4(disc, 1)])
        self.assertTrue(any("no longer match" in w for w in result.warnings), result.warnings)
        manifest, _ = self.tokened_manifest(disc, "failed")
        self.assertEqual(manifest["conversion_status"]["promotion_error"], result.promotion_error)

    # --- N21 and A1 (T-155) ---

    def test_a_video_ts_without_vob_files_is_a_truthful_skip(self):
        cases = (("IFO only", {1: straight_ifo([600], [4])}, [], "1 IFO, 0 BUP"),
                 ("IFO and BUP", {1: straight_ifo([600], [4])}, ["VTS_01_0.BUP"], "1 IFO, 1 BUP"),
                 ("empty", {}, [], "0 IFO, 0 BUP"))
        for n, (label, ifos, bups, counts) in enumerate(cases):
            with self.subTest(label):
                disc = self.make_disc({}, stem=f"JPC_AV_0017{n}", ifos=ifos)
                for name in bups:
                    (disc.video_ts / name).write_bytes(b"SYNTHETIC BUP")
                self.lines.clear()  # this subTest's console only
                result = self.run_convert(disc)
                self.assertTrue(result.success and result.skipped, result.error_message)
                self.assertEqual(result.skip_reason,
                                 f"No VOB files found in VIDEO_TS ({counts}); nothing to convert")
                manifest, log_text = self.tokened_manifest(disc, "skipped")
                self.assertEqual((manifest["disc_analysis"]["disc_type"],
                                  manifest["disc_analysis"]["has_video_ts"]),
                                 ("video_ts_without_vobs", True))
                self.assertIn("verify the ISO", manifest["disc_analysis"]["recommendation"])
                self.assertIn(result.skip_reason, log_text)
                self.assertTrue(any("verify the ISO" in line for line in self.lines))
                self.assertEqual(self.tools.launched, [])
                self.assertEqual(self.main_exit("-i", str(disc.iso)), 0)

    def test_a_dvd_audio_skip_says_its_video_ts_has_no_vob_files(self):
        # N21 with AUDIO_TS (VB.json: still a DVD-Audio skip, whose reason
        # says the VIDEO_TS has no VOB files). DVD-Video discs often carry
        # an empty AUDIO_TS, so the "verify the image" guidance is kept too
        disc = self.make_disc({}, stem="JPC_AV_00175", ifos={1: straight_ifo([600], [4])})
        (disc.video_ts.parent / "AUDIO_TS").mkdir()
        result = self.run_convert(disc)
        self.assertTrue(result.success and result.skipped, result.error_message)
        self.assertEqual(result.skip_reason,
                         "DVD-Audio disc (AUDIO_TS; its VIDEO_TS has no VOB files: 1 IFO, 0 BUP)")
        manifest, log_text = self.tokened_manifest(disc, "skipped")
        analysis = manifest["disc_analysis"]
        self.assertEqual((analysis["disc_type"], analysis["has_video_ts"], analysis["has_audio_ts"]),
                         ("dvd_audio", True, True))
        for phrase in ("incomplete DVD-Video image", "verify the ISO", "DVD-Audio"):
            self.assertIn(phrase, analysis["recommendation"])
        self.assertIn(result.skip_reason, log_text)
        self.assertTrue(any("verify the ISO" in line for line in self.lines), self.lines)
        self.assertEqual(self.tools.launched, [])

    def test_a_vob_numbering_gap_is_an_advisory_warning(self):
        # A1 / MV-12: VTS_01_2.VOB is missing; the outcome does not change
        disc = self.make_disc({1: [3, 3]}, stem="JPC_AV_00176",
                              ifos={1: straight_ifo([200, 100, 200], [3, 2, 3])})
        (disc.video_ts / "VTS_01_2.VOB").rename(disc.video_ts / "VTS_01_3.VOB")
        self.vob_durations(1, 200.0, 100.0, 200.0)
        self.output(disc, 1, 400.0)
        gap = "VTS_01: content VOB numbering has a gap (found 1, 3; missing 2)"
        result = self.run_convert(disc)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual([t.status for t in result.titles], ["completed"])
        self.assertIn(gap, result.warnings)
        manifest = json.loads((disc.out / f"{disc.stem}_access_manifest.json").read_text())
        self.assertEqual(manifest["conversion_status"]["overall_status"], "success_with_warnings")
        self.assertIn(gap, manifest["conversion_status"]["warnings"])
        self.assertTrue(any(gap in line for line in self.lines))

    # --- healthy controls ---

    def test_control_an_irrelevant_root_entry_does_not_fail_the_analysis(self):
        # A deliberate narrowing: only VIDEO_TS/AUDIO_TS decide the disc
        # type, so a root entry that cannot be checked no longer fails it
        # (it is not checked at all)
        disc = new_disc(self, "JPC_AV_00177")
        readme = disc.video_ts.parent / "README.TXT"
        readme.write_text("synthetic notes\n")
        with stat_failing(lambda p: p == readme, errno.EIO) as fired:
            with self.assertRaises(OSError):
                os.stat(readme)
            self.assertTrue(fired, "the fault is not live")
            result = self.run_convert(disc)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual([t.status for t in result.titles], ["completed"])

    def test_control_other_disc_types_keep_their_reasons(self):
        cases = (("data disc", "Data disc (no VIDEO_TS folder)"),
                 ("DVD-Audio only", "DVD-Audio disc (AUDIO_TS only, no VIDEO_TS)"),
                 ("empty image", "Empty or unreadable disc"),
                 ("menu-only VIDEO_TS", "Menu-only VIDEO_TS structure (no content VOBs)"))
        for n, (label, reason) in enumerate(cases):
            with self.subTest(label):
                disc = self.make_disc({}, stem=f"JPC_AV_0018{n}")
                root = disc.video_ts.parent
                if label == "menu-only VIDEO_TS":
                    for name in ("VIDEO_TS.VOB", "VTS_01_0.VOB"):
                        (disc.video_ts / name).write_bytes(b"SYNTHETIC MENU")
                else:
                    shutil.rmtree(disc.video_ts)
                    if label == "data disc":
                        (root / "photo.jpg").write_bytes(b"SYNTHETIC DATA FILE")
                    elif label == "DVD-Audio only":
                        (root / "AUDIO_TS").mkdir()
                result = self.run_convert(disc)
                self.assertTrue(result.success and result.skipped, result.error_message)
                self.assertEqual(result.skip_reason, reason)
        disc = self.make_disc({1: [4, 4]}, stem="JPC_AV_00189")
        self.vob_durations(1, 300.0, 300.0)
        self.output(disc, 1, 600.0)
        result = self.run_convert(disc)
        self.assertTrue(result.success, result.error_message)
        self.assertFalse(any("numbering has a gap" in w for w in result.warnings), result.warnings)
        manifest = json.loads((disc.out / f"{disc.stem}_access_manifest.json").read_text())
        self.assertEqual(manifest["conversion_status"]["overall_status"], "success")


class InvocationTests(Scenario):
    """T-155: ffmpeg starts with -nostdin and its standard input on
    /dev/null (N19, hardening only: no terminal behaviour is claimed or
    tested), and the command shown is shell-quoted (MV-14). The help gives
    the T-150/T-151 rules"""

    def ffmpeg_argv(self):
        encodes = [c for c in self.tools.calls if c[0] == "ffmpeg" and "-i" in c]
        self.assertEqual(len(encodes), 1, encodes)
        return encodes[0]

    def test_ffmpeg_takes_no_terminal_input(self):
        disc = new_disc(self, "JPC_AV_00191")
        result = self.convert(disc)
        self.assertTrue(result.success, result.error_message)
        kwargs = self.tools.popen_kwargs[0]
        self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertIs(kwargs.get("stdout"), subprocess.DEVNULL)
        argv = self.ffmpeg_argv()
        self.assertIn("-nostdin", argv)
        self.assertLess(argv.index("-nostdin"), argv.index("-i"))

    def test_the_shown_ffmpeg_command_is_quoted(self):
        disc = new_disc(self, "JPC AV 00192")
        result = self.convert(disc)
        self.assertTrue(result.success, result.error_message)
        shown = [line[len("Command: "):] for line in self.lines if line.startswith("Command: ")]
        self.assertEqual(len(shown), 1, shown)
        self.assertEqual(shlex.split(shown[0]), self.ffmpeg_argv())

    def test_help_gives_the_inspection_and_batch_rules(self):
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            mv.print_help()
        text = " ".join(shown.getvalue().split())
        for phrase in ('any error other than "no such file"', "VIDEO_TS without VOB files:",
                       "a batch search that was incomplete or refused an ISO",
                       "AppleDouble ._*.iso", "*_attempt-*.iso",
                       "Symlinked folders inside the batch folder are not followed (they are "
                       "listed); an .iso symlink to a regular file is converted",
                       "the ISOs found are still converted, and the batch exits 1 unless a Ctrl-C "
                       "stops it", "130 a Ctrl-C stopped the run or batch"):
            self.assertIn(phrase, text)
        self.assertNotIn("Symlinks inside the batch folder are not followed", text)
        self.assertIn("a batch that has started converting starts no further ISO and shows its "
                      "summary; during the search for ISOs it stops the batch with no summary",
                      text)

    def operator_prompt(self, args, answer):
        """main() without --operator, the operator-name prompt answered by
        answer (an exception it raises); returns the exit status"""
        with mock.patch.object(sys, "argv", ["makeiso-video.py", *args]), \
                mock.patch("builtins.input", side_effect=answer) as prompt:
            try:
                with self.assertRaises(SystemExit) as caught:
                    mv.main()
            except KeyboardInterrupt:
                self.fail("Ctrl-C escaped main()")
        self.assertEqual(prompt.call_count, 1, "the operator prompt was not shown once")
        return caught.exception.code

    def test_ctrl_c_at_the_operator_prompt_exits_130_with_nothing_started(self):
        # The adopted rule: a Ctrl-C that stops a run exits 130. End of
        # input there still exits 1: nothing can start without an operator
        disc = new_disc(self, "JPC_AV_00196")
        for label, args, answer, status in (
                ("Ctrl-C, single ISO", ("-i", str(disc.iso)), KeyboardInterrupt, 130),
                ("Ctrl-C, batch", ("--batch", str(self.root / "isos")), KeyboardInterrupt, 130),
                ("end of input, batch", ("--batch", str(self.root / "isos")), EOFError, 1)):
            with self.subTest(label):
                self.lines.clear()
                self.assertEqual(self.operator_prompt(args, answer), status)
                self.assertEqual((self.tools.launched, self.mount_objects), ([], []))
                self.assertFalse(disc.out.exists(), "records were written")
                self.assertFalse(any("Batch" in line for line in self.lines), "the batch started")

    def interrupting_enumerate(self, after):
        """enumerate for process_batch's loop over the ISOs only: a Ctrl-C as
        the loop moves on once `after` ISOs were handed out (the boundary
        between ISOs); every other call is the builtin. Yields the fired list"""
        code, real, fired = mv.process_batch.__code__, enumerate, []

        def patched(iterable, start=0):
            if sys._getframe(1).f_code is not code:
                return real(iterable, start)

            def items():
                for n, item in real(iterable, start):
                    if n > after:
                        break
                    yield n, item
                fired.append(after)
                raise KeyboardInterrupt

            return items()

        return mock.patch.object(mv, "enumerate", patched, create=True), fired

    def test_a_ctrl_c_between_isos_counts_the_rest_as_not_started(self):
        discs = [new_disc(self, f"JPC_AV_0019{n}") for n in (7, 8, 9)]
        patch, fired = self.interrupting_enumerate(after=1)
        with patch:
            try:
                status = self.main_exit("--batch", str(self.root / "isos"))
            except KeyboardInterrupt:
                self.fail("Ctrl-C escaped main()")
        self.assertTrue(fired, "the Ctrl-C never came")
        self.assertEqual(status, 130)
        self.assertEqual(self.tools.launched, [self.mp4(discs[0], 1).name])
        self.assertIn(f"Interrupted by operator (Ctrl-C) before {discs[1].stem}.iso started "
                      f"(1 of 3 processed); 2 ISO(s) not started", self.lines)

    def test_a_ctrl_c_after_the_last_iso_counts_none_as_not_started(self):
        discs = [new_disc(self, f"JPC_AV_0020{n}") for n in (1, 2)]
        patch, fired = self.interrupting_enumerate(after=2)
        with patch:
            try:
                status = self.main_exit("--batch", str(self.root / "isos"))
            except KeyboardInterrupt:
                self.fail("Ctrl-C escaped main()")
        self.assertTrue(fired, "the Ctrl-C never came")
        self.assertEqual(status, 130)
        self.assertEqual(self.tools.launched, [self.mp4(d, 1).name for d in discs])
        self.assertIn("Interrupted by operator (Ctrl-C) at the end of the batch (2 of 2); "
                      "0 ISO(s) not started", self.lines)


class GuardTests(unittest.TestCase):
    def test_only_tripwires_on_path(self):
        self.assertEqual(os.environ["PATH"], str(TRIPWIRE_DIR))
        for name in NATIVE_TOOLS:
            self.assertEqual(shutil.which(name), str(TRIPWIRE_DIR / name))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--sigint-child":
        sys.exit(_child_main(sys.argv[2], Path(sys.argv[3])))
    unittest.main()
