from __future__ import annotations

import os
import select
import sys


class DashboardPager:
    """Read navigation keys without blocking the live dashboard or consuming signals."""

    def __init__(self) -> None:
        self.page = 0
        self.quit_requested = False
        self.active = False
        self._fd: int | None = None
        self._original = None
        self._buffer = ""

    def __enter__(self) -> DashboardPager:
        if os.name != "posix" or not sys.stdin.isatty():
            return self
        import termios
        import tty

        try:
            self._fd = sys.stdin.fileno()
            self._original = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self.active = True
        except (OSError, ValueError, termios.error):
            self._fd = None
        return self

    def __exit__(self, *_args) -> None:
        if self.active and self._fd is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._original)
        self.active = False

    def poll(self) -> None:
        if self._fd is not None and select.select([self._fd], [], [], 0)[0]:
            self._consume(os.read(self._fd, 1024).decode("utf-8", errors="ignore"))

    def _consume(self, keys: str) -> None:
        self._buffer += keys
        while self._buffer:
            if self._buffer[0] == "\x1b":
                if len(self._buffer) < 3:
                    return
                key, self._buffer = self._buffer[:3], self._buffer[3:]
                if key in {"\x1b[C", "\x1b[B"}:
                    self.page += 1
                elif key in {"\x1b[D", "\x1b[A"}:
                    self.page -= 1
                continue
            key, self._buffer = self._buffer[0], self._buffer[1:]
            if key.lower() == "n" or key == "\t":
                self.page += 1
            elif key.lower() == "p":
                self.page -= 1
            elif key.lower() == "q":
                self.quit_requested = True
