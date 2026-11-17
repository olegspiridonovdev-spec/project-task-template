#!/usr/bin/env python3
"""The three things this board does that an operating system does differently.

Everything else here is `pathlib` and `subprocess`, which are the same
everywhere. These are not, and each of them was a `fcntl`, a `select` or an
`osascript` written straight into the code that needed it — which is how the
board became a macOS program without anyone deciding that it should be.

    locking     who owns this resource, and is that owner still alive
    processes   is that pid alive, and how do I end it
    reading     read two pipes at once without blocking on either
    notifying   tell the human something happened

Windows is the reason the module exists, but it is not the only beneficiary:
Linux got desktop notifications out of the same change, and the threaded reader
below behaves identically on all three rather than only being *tested* on one.

What this module does not paper over is the loop's scheduler. `bin/loopctl`
installs a launchd job or a systemd timer; Windows has neither, and pretending
otherwise with a Scheduled Task nobody here can test would be worse than saying
so. The board's CLI works everywhere; the unattended loop is POSIX.
"""

from __future__ import annotations

import errno
import os
import platform
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Callable, Iterator

WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")

if WINDOWS:                                             # pragma: no cover - platform
    import msvcrt
else:
    import fcntl
    import signal


def platform_name() -> str:
    """What `bin/task doctor` and `bin/loopctl doctor` print."""
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


# --------------------------------------------------------------------------
# locking
# --------------------------------------------------------------------------
#
# The board's whole concurrency model rests on one property: the lock is
# released by whatever ends the process, including SIGKILL and a power cut.
# That is why it is an advisory lock held by an open handle and never a lockfile
# with a pid in it — a pid file outlives the process that wrote it, and then the
# board has to guess whether the owner is alive, which is the guess this design
# exists to avoid.
#
# Both implementations have that property. `fcntl.flock` is released by the
# kernel when the last handle closes; `msvcrt.locking` is released when the
# handle closes or the process ends. What differs is only the call.

def lock_exclusive(handle: IO, blocking: bool = False) -> bool:
    """Take an exclusive lock on an open file. False when someone else holds it.

    `blocking` is honoured on POSIX by the kernel. Windows has no non-blocking
    wait that is also interruptible, so a blocking request there is a poll —
    same semantics, coarser granularity, and only ever used on the board's git
    lock where a fifth of a second does not matter.
    """
    if WINDOWS:                                         # pragma: no cover - platform
        deadline = time.monotonic() + 60 if blocking else 0.0
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if not blocking or time.monotonic() > deadline:
                    return False
                time.sleep(0.2)
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle, flags)
        return True
    except OSError:
        return False


def unlock(handle: IO) -> None:
    """Release a lock taken with `lock_exclusive`. Closing the handle also works."""
    try:
        if WINDOWS:                                     # pragma: no cover - platform
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass


def open_lock_file(path: Path) -> IO:
    """Open a lock file for locking, creating it and its directory if needed.

    Opened `r+` rather than `w`, because `w` truncates: on Windows the truncation
    happens before the lock is taken, so an unsuccessful attempt would blank the
    holder's own record of who it is.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    return open(path, "r+", encoding="utf-8")


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------

def process_alive(pid: int) -> bool:
    """Does this pid name a live process?

    `EPERM` counts as alive on POSIX: the process exists, it simply is not ours
    to signal. On Windows the same question is asked of the process list,
    because there is no signal 0 to send.
    """
    if not pid:
        return False
    if WINDOWS:                                         # pragma: no cover - platform
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True)
        return f'"{int(pid)}"' in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM


def terminate(pid: int) -> bool:
    """Ask a process to stop. False when it was already gone."""
    if not pid:
        return False
    if WINDOWS:                                         # pragma: no cover - platform
        return subprocess.run(["taskkill", "/PID", str(int(pid)), "/T"],
                              capture_output=True).returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def kill(pid: int) -> bool:
    """End a process that would not stop. False when it was already gone."""
    if not pid:
        return False
    if WINDOWS:                                         # pragma: no cover - platform
        return subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                              capture_output=True).returncode == 0
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        return False


def kill_gracefully(pid: int, grace_sec: float, alive: Callable[[int], bool] | None = None) -> None:
    """SIGTERM, then SIGKILL after the grace period. The agent's exit path."""
    check = alive or process_alive
    if not terminate(pid):
        return
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if not check(pid):
            return
        time.sleep(1)
    kill(pid)


# --------------------------------------------------------------------------
# reading two pipes at once
# --------------------------------------------------------------------------

class StreamReader:
    """Read a process's stdout and stderr concurrently, without blocking.

    `select.select` does this on POSIX and refuses anything but a socket on
    Windows, so the portable answer is a thread per pipe writing into a queue.
    That is not a concession: the previous loop woke every 5 seconds whether or
    not there was anything to read, and this one is woken by the line itself,
    which makes the heartbeat and the STOP check tighter rather than looser.

    Which stream a line came from still matters — stdout carries the agent's
    JSON events, stderr carries the CLI's own complaints, and the wrapper reads
    the two for different things — so a line is yielded as `(name, text)` and
    never merged.
    """

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.queue: queue.Queue = queue.Queue()
        self.threads: list[threading.Thread] = []
        for name in ("stdout", "stderr"):
            stream = getattr(process, name)
            if stream is None:
                continue
            thread = threading.Thread(target=self._pump, args=(name, stream), daemon=True)
            thread.start()
            self.threads.append(thread)
        self.open_streams = len(self.threads)

    def _pump(self, name: str, stream: IO) -> None:
        try:
            for line in iter(stream.readline, ""):
                self.queue.put((name, line))
        except (ValueError, OSError):
            # The pipe was closed under us - the process was killed. Not an
            # error: the reader is about to be told the stream ended anyway.
            pass
        finally:
            self.queue.put((name, None))

    def lines(self, poll_sec: float = 1.0) -> Iterator[tuple[str, str] | None]:
        """Yield `(stream, line)` as they arrive, and `None` on every idle tick.

        The `None` is what lets the caller do its periodic work — flush the
        heartbeat, look for STOP, check the wall clock — without a second timer
        and without waiting for a line that may never come. A wedged agent
        produces nothing but `None`s, which is exactly the signal the watchdog
        is looking for.
        """
        while self.open_streams:
            try:
                name, line = self.queue.get(timeout=poll_sec)
            except queue.Empty:
                yield None
                continue
            if line is None:
                self.open_streams -= 1
                continue
            yield (name, line)

    def drain(self, timeout: float = 5.0) -> None:
        """Let the pump threads finish, then close the pipes they were reading.

        The threads are daemons and would not hold the process open, but the
        pipe handles would stay open until the garbage collector got to them,
        which is a warning in a test run and a leaked descriptor in a loop that
        starts an agent every ten minutes.
        """
        for thread in self.threads:
            thread.join(timeout=timeout)
        for name in ("stdout", "stderr"):
            stream = getattr(self.process, name, None)
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass


# --------------------------------------------------------------------------
# notifying
# --------------------------------------------------------------------------

def notify_desktop(title: str, subtitle: str, message: str) -> bool:
    """Raise a desktop notification. False when the platform has no way to.

    Best-effort by design and never fatal: a board on a headless Linux box has
    no notification daemon, and failing a transition because a banner could not
    be drawn would be absurd. `NOTIFY.md` is the record that is always written.
    """
    try:
        if MACOS:
            body = message.replace('"', "'")
            head = title.replace('"', "'")
            sub = subtitle.replace('"', "'")
            script = f'display notification "{body}" with title "{head}" subtitle "{sub}"'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
            return True
        if LINUX:
            urgency = "critical" if "error" in subtitle.lower() else "normal"
            subprocess.run(["notify-send", "-u", urgency, f"{title}: {subtitle}", message],
                           capture_output=True, timeout=10)
            return True
        if WINDOWS:                                     # pragma: no cover - platform
            # BurntToast is not installed on a default Windows, so this uses the
            # WinRT toast API that ships with the OS. Single-quoted PowerShell
            # strings escape a quote by doubling it.
            def q(text: str) -> str:
                return text.replace("'", "''")
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
                " ContentType = WindowsRuntime] > $null;"
                "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{q(title)}: {q(subtitle)}')) > $null;"
                f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode('{q(message)}')) > $null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                f"'{q(title)}').Show([Windows.UI.Notifications.ToastNotification]::new($t))")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=15)
            return True
    except Exception:
        return False
    return False


# --------------------------------------------------------------------------
# scheduling
# --------------------------------------------------------------------------

def scheduler() -> str:
    """Which unattended scheduler this machine has: launchd, systemd or none."""
    if MACOS:
        return "launchd"
    if LINUX:
        return "systemd"
    return "none"
