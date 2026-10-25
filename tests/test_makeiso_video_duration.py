#!/usr/bin/env python3
"""Synthetic tests for makeiso-video.py's duration check, review copies and
deferred promotion (T-071), and for ffmpeg's lifetime and truthful retention
records (T-129, T-130, T-132): ffmpeg is always stopped and reaped before its
output is kept, an ffmpeg that cannot be confirmed stopped stops the run and
batch and leaves the source attached, and an interrupted or repeated
retention records where the output really is.

Standard library only, and synthetic throughout: placeholder IFO, VOB and MP4
bytes, a fake encoder and controlled ffprobe answers patched in at the
subprocess boundary, and a fake mount over a plain directory. Real ffmpeg,
ffprobe, hdiutil, diskutil, sudo, mounts, devices and network access are
blocked: tripwire executables are the only things on PATH and outgoing
connections are refused.

The shared guards in synthetic_support.py are installed before the script
is loaded. EncoderLifetimeTests send real SIGINTs to this process at the fake
boundaries (no child process); the script holds or handles each one, and the
tests stop rather than let one escape.

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
import importlib.util
import io
import json
import os
import re
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
        self.assertEqual(self.run_main("--batch", str(self.root / "isos")), 1)
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


class RetentionTests(Scenario):
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
        manifest = json.loads((disc.out / f"{STEM}_skip_manifest.json").read_text())
        record = manifest["titlesets"]["outputs"][0]
        self.assertEqual((record["status"], record["mapped_video_streams"],
                          record["mapped_audio_streams"]), ("planned", None, None))


class SourceMountTests(unittest.TestCase):
    """C1: a source image kept attached is never detached - no forced
    detach, device fallback or leak sweep. Controls: an ordinary exit still
    detaches. hdiutil is faked at run_cmd and only recorded."""

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

class GuardTests(unittest.TestCase):
    def test_only_tripwires_on_path(self):
        self.assertEqual(os.environ["PATH"], str(TRIPWIRE_DIR))
        for name in NATIVE_TOOLS:
            self.assertEqual(shutil.which(name), str(TRIPWIRE_DIR / name))


if __name__ == "__main__":
    unittest.main()
