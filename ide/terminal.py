"""Integrated terminal, cross-platform.

Two backends, chosen at runtime:

* POSIX  - a real pty via `pty.fork()`. Full interactive behaviour.
* Windows - ConPTY through `pywinpty` when it is installed, otherwise a
  pipe-backed shell session (PowerShell or cmd) driven line by line. The pipe
  backend cannot host full-screen TUIs (vim, htop), but ordinary commands,
  git, build tools and REPLs work fine.

Output is pushed to the browser over SSE, keystrokes are POSTed back. One reader
thread per terminal; nothing survives closing the panel.
"""

from __future__ import annotations

import os
import queue
import shutil
import signal
import subprocess
import threading
import uuid
from typing import Dict, List, Optional

IS_WINDOWS = os.name == "nt"


def default_shell() -> str:
    if IS_WINDOWS:
        for candidate in ("pwsh.exe", "powershell.exe", "cmd.exe"):
            found = shutil.which(candidate)
            if found:
                return found
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL") or "/bin/bash"


class BaseTerminal:
    """Shared queue/lifecycle plumbing for both backends."""

    def __init__(self, cwd: str, shell: Optional[str], cols: int, rows: int):
        self.id = uuid.uuid4().hex[:8]
        self.queue: "queue.Queue[str]" = queue.Queue(maxsize=4096)
        self.alive = True
        self.cwd = cwd
        self.shell = shell or default_shell()
        self.cols = cols
        self.rows = rows

    # -- helpers -----------------------------------------------------------
    def push(self, text: str) -> None:
        try:
            self.queue.put_nowait(text)
        except queue.Full:
            pass

    def finish(self) -> None:
        self.alive = False
        self.push("\r\n[process exited]\r\n")

    # -- interface ---------------------------------------------------------
    def write(self, data: str) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows

    def close(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class PosixPtyTerminal(BaseTerminal):
    def __init__(self, cwd: str, shell: Optional[str] = None, cols: int = 100, rows: int = 28):
        super().__init__(cwd, shell, cols, rows)
        import pty

        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.cwd)
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLUMNS"] = str(cols)
            os.environ["LINES"] = str(rows)
            os.execvp(self.shell, [self.shell, "-i"])
            os._exit(1)
        self.pid, self.fd = pid, fd
        self.resize(cols, rows)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        while self.alive:
            try:
                data = os.read(self.fd, 8192)
            except OSError:
                break
            if not data:
                break
            self.push(data.decode("utf-8", "replace"))
        self.finish()

    def write(self, data: str) -> None:
        if self.alive and self.fd is not None:
            os.write(self.fd, data.encode("utf-8"))

    def resize(self, cols: int, rows: int) -> None:
        super().resize(cols, rows)
        try:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def close(self) -> None:
        self.alive = False
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except Exception:
                pass
        try:
            os.close(self.fd)
        except Exception:
            pass


class WinPtyTerminal(BaseTerminal):
    """ConPTY backend. Used only when the optional `pywinpty` wheel is present."""

    def __init__(self, cwd: str, shell: Optional[str] = None, cols: int = 100, rows: int = 28):
        super().__init__(cwd, shell, cols, rows)
        import winpty  # type: ignore

        self.pty = winpty.PtyProcess.spawn(self.shell, cwd=self.cwd, dimensions=(rows, cols))
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        while self.alive and self.pty.isalive():
            try:
                data = self.pty.read(8192)
            except EOFError:
                break
            except Exception:
                break
            if data:
                self.push(data)
        self.finish()

    def write(self, data: str) -> None:
        if self.alive:
            try:
                self.pty.write(data)
            except Exception:
                self.finish()

    def resize(self, cols: int, rows: int) -> None:
        super().resize(cols, rows)
        try:
            self.pty.setwinsize(rows, cols)
        except Exception:
            pass

    def close(self) -> None:
        self.alive = False
        try:
            self.pty.terminate(force=True)
        except Exception:
            pass


class PipeTerminal(BaseTerminal):
    """Dependency-free Windows fallback: shell driven through stdin/stdout pipes.

    Line oriented. Interactive full-screen programs are not supported, which the
    UI states plainly instead of pretending to be a pty.
    """

    def __init__(self, cwd: str, shell: Optional[str] = None, cols: int = 100, rows: int = 28):
        super().__init__(cwd, shell, cols, rows)
        argv: List[str]
        lowered = os.path.basename(self.shell).lower()
        if "powershell" in lowered or "pwsh" in lowered:
            argv = [self.shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"]
        elif "cmd" in lowered:
            argv = [self.shell, "/Q", "/K"]
        else:
            argv = [self.shell]

        creationflags = 0
        if IS_WINDOWS:
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        self.proc = subprocess.Popen(
            argv,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self.push(f"[{os.path.basename(self.shell)} - line mode, no full-screen apps]\r\n")
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        stream = self.proc.stdout
        if stream is None:
            self.finish()
            return
        while self.alive:
            chunk = stream.readline()
            if not chunk:
                break
            self.push(chunk)
        self.finish()

    def write(self, data: str) -> None:
        if not self.alive or self.proc.stdin is None:
            return
        # the browser sends \n; a pipe-backed shell needs a real line ending
        if data == "\x03":
            self.interrupt()
            return
        payload = data.replace("\r", "\n")
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
        except Exception:
            self.finish()

    def interrupt(self) -> None:
        try:
            if IS_WINDOWS:
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                self.proc.send_signal(signal.SIGINT)
        except Exception:
            pass

    def close(self) -> None:
        self.alive = False
        try:
            self.proc.terminate()
        except Exception:
            pass


def make_terminal(cwd: str, cols: int, rows: int, shell: Optional[str] = None) -> BaseTerminal:
    """Pick the best backend available on this machine."""
    if not IS_WINDOWS:
        return PosixPtyTerminal(cwd, shell, cols, rows)
    try:
        import winpty  # noqa: F401

        return WinPtyTerminal(cwd, shell, cols, rows)
    except Exception:
        return PipeTerminal(cwd, shell, cols, rows)


class TerminalPool:
    def __init__(self, cwd: str, shell: Optional[str] = None):
        self.cwd = cwd
        self.shell = shell
        self.terminals: Dict[str, BaseTerminal] = {}

    def create(self, cols: int = 100, rows: int = 28) -> BaseTerminal:
        terminal = make_terminal(self.cwd, cols, rows, self.shell)
        self.terminals[terminal.id] = terminal
        return terminal

    def get(self, terminal_id: str) -> Optional[BaseTerminal]:
        return self.terminals.get(terminal_id)

    def close(self, terminal_id: str) -> None:
        terminal = self.terminals.pop(terminal_id, None)
        if terminal:
            terminal.close()

    def shutdown(self) -> None:
        for terminal_id in list(self.terminals):
            self.close(terminal_id)
