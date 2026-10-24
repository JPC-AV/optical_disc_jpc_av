"""Shared guards and loaders for the synthetic optical-disc cleanup tests.

Import this (by path) before loading any product module. It installs:
  - an audit hook that blocks child processes, sockets, device access and
    any file change outside the private temp directory; the only child
    allowed is this interpreter re-running a test module with --sigint-child
  - tripwire executables (ffmpeg, ffprobe, dd, diskutil, hdiutil, isolyzer,
    tree, sudo, chown, mount, umount) as the only entries on PATH
  - a synthetic platform.processor(), which would otherwise run `uname -p`

Product modules are loaded from OPTICAL_UNDER_TEST (a directory holding
makeiso.py, verifyiso.py, makeiso-video.py and tests/make_verify_fixtures.py),
default the repository root, so the same tests can run against another copy.
Nothing here touches real media, devices, mounts or the network.
"""

import fcntl
import importlib.util
import os
import platform
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("OPTICAL_UNDER_TEST") or Path(__file__).resolve().parents[1])
PRIVATE = Path(tempfile.gettempdir()).resolve()
VIOLATIONS = []
TRIPWIRES = ("ffmpeg", "ffprobe", "dd", "diskutil", "hdiutil", "isolyzer", "tree",
             "sudo", "chown", "mount", "umount", "drutil")


def _inside(path, dir_fd=None) -> bool:
    if isinstance(path, int):
        return True
    name = os.fsdecode(path)
    if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(name):
        # A name relative to a directory descriptor (shutil.rmtree does this)
        try:
            base = fcntl.fcntl(dir_fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0")
        except (OSError, AttributeError):
            return False
        name = os.path.join(os.fsdecode(base), name)
    try:
        Path(os.path.abspath(name)).resolve().relative_to(PRIVATE)
        return True
    except ValueError:
        return False


def _allowed_child(args) -> bool:
    argv = args[1] if len(args) > 1 else None
    if not isinstance(argv, (list, tuple)) or not argv:
        return False
    return os.fsdecode(argv[0]) == sys.executable and "--sigint-child" in [os.fsdecode(a) for a in argv]


def _guard(event, args):
    blocked = event in ("os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
                        "os.forkpty", "pty.spawn")
    if event == "subprocess.Popen":
        blocked = not _allowed_child(args)
    if event.startswith("socket.") and event != "socket.__new__":
        blocked = True
    if event == "open" and not isinstance(args[0], int):
        name = os.fsdecode(args[0])
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        exclusive_on_existing = bool(flags & os.O_EXCL) and os.path.lexists(name)
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        harmless = exclusive_on_existing or name == os.devnull
        if name.startswith("/dev/") and not harmless:
            blocked = True
        if writing and not _inside(name) and not harmless:
            blocked = True
    if event in ("os.remove", "os.rmdir"):
        blocked = blocked or not _inside(args[0], args[1] if len(args) > 1 else None)
    if event in ("os.mkdir", "os.chmod"):
        blocked = blocked or not _inside(args[0], args[2] if len(args) > 2 else None)
    if event == "os.chown":
        blocked = blocked or not _inside(args[0], args[3] if len(args) > 3 else None)
    if event == "os.truncate":
        blocked = blocked or not _inside(args[0])
    if event in ("os.rename", "os.link"):
        blocked = blocked or not _inside(args[0], args[2] if len(args) > 2 else None) \
            or not _inside(args[1], args[3] if len(args) > 3 else None)
    if event == "os.symlink":
        blocked = blocked or not _inside(args[1], args[2] if len(args) > 2 else None)
    if blocked:
        VIOLATIONS.append((event, repr(args)[:300]))
        raise RuntimeError(f"SYNTHETIC GUARD BLOCKED: {event}")


def install():
    """Guards first, then tripwires; idempotent"""
    if getattr(install, "done", False):
        return
    tripwire_dir = Path(tempfile.mkdtemp(prefix="optical-tripwires-"))
    log = tripwire_dir / "calls.log"
    for name in TRIPWIRES:
        path = tripwire_dir / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 97\n')
        path.chmod(0o755)
    os.environ["PATH"] = str(tripwire_dir)
    install.tripwire_log = log
    platform.processor = lambda: "synthetic-processor"
    sys.addaudithook(_guard)
    install.done = True


def tripwire_calls() -> str:
    log = getattr(install, "tripwire_log", None)
    return log.read_text() if log and log.exists() else ""


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
