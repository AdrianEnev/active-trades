from __future__ import annotations

import os
import select
import sys
import time


class DashboardPager:
    """Read navigation keys without blocking the live dashboard or consuming signals."""

    # The live loop polls every 250 ms; retain split key sequences across a poll.
    _ESCAPE_TIMEOUT = 0.5
    _PASTE_END = "\x1b[201~"

    def __init__(self, hide_amounts: bool = False, email_alerts: bool = False) -> None:
        self.page = 0
        self.hide_amounts = hide_amounts
        self.email_alerts = email_alerts
        self.quit_requested = False
        # Cancel prompt: "select" collects a limit-order row number; "confirm" waits for y/n.
        self.cancel_stage: str | None = None
        self.cancel_digits = ""
        self.cancel_row: int | None = None
        self.cancel_confirmed = False
        self.active = False
        self._fd: int | None = None
        self._original = None
        self._bracketed_paste = False
        self._state = "normal"
        self._sequence = ""
        self._escape_updated_at: float | None = None
        self._string_escape = False

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
        if self.active and sys.stdout.isatty():
            self._bracketed_paste = True
            try:
                sys.stdout.write("\x1b[?2004h")
                sys.stdout.flush()
            except (OSError, ValueError):
                self.__exit__()
                raise
        return self

    def __exit__(self, *_args) -> None:
        try:
            if self._bracketed_paste:
                sys.stdout.write("\x1b[?2004l")
                sys.stdout.flush()
        finally:
            self._bracketed_paste = False
            try:
                if self.active and self._fd is not None:
                    import termios

                    termios.tcsetattr(self._fd, termios.TCSADRAIN, self._original)
            finally:
                self.active = False

    def poll(self) -> bool:
        before = self._controls()
        if self._fd is not None and select.select([self._fd], [], [], 0)[0]:
            self._consume(os.read(self._fd, 1024).decode("utf-8", errors="ignore"))
        else:
            self._expire_escape()
        return before != self._controls()

    def close_cancel(self) -> None:
        self.cancel_stage = None
        self.cancel_digits = ""
        self.cancel_row = None

    def reopen_cancel(self) -> None:
        """Return to row entry, for example after an unknown row number."""
        self.close_cancel()
        self.cancel_stage = "select"

    def _controls(self) -> tuple:
        return (
            self.page, self.hide_amounts, self.email_alerts, self.quit_requested,
            self.cancel_stage, self.cancel_digits, self.cancel_row, self.cancel_confirmed,
        )

    def _consume(self, keys: str) -> None:
        for key in keys:
            if self._state == "paste":
                # Paste contents are data, including navigation keys and escapes.
                self._sequence += key
                if self._sequence == self._PASTE_END:
                    self._reset_escape()
                elif not self._PASTE_END.startswith(self._sequence):
                    self._sequence = "\x1b" if key == "\x1b" else ""
                continue

            if self._state == "string":
                if (self._sequence == "]" and key == "\x07") or (self._string_escape and key == "\\"):
                    self._reset_escape()
                else:
                    self._string_escape = key == "\x1b"
                    self._escape_updated_at = time.monotonic()
                continue

            if key == "\x1b":
                self._state = "escape"
                self._sequence = ""
                self._escape_updated_at = time.monotonic()
                continue

            if self._state != "normal":
                self._escape_updated_at = time.monotonic()
                if self._state == "escape":
                    if key in {"[", "O"}:
                        self._state = "csi" if key == "[" else "ss3"
                    elif key in {"]", "P", "X", "^", "_"}:
                        self._state = "string"
                        self._sequence = key
                        self._string_escape = False
                    elif " " <= key <= "/":
                        self._state = "intermediate"
                    else:
                        # An Alt key or unknown escape consumes its own final byte.
                        self._reset_escape()
                elif self._state == "intermediate":
                    if not " " <= key <= "/":
                        self._reset_escape()
                elif "@" <= key <= "~":
                    state, parameters = self._state, self._sequence
                    self._reset_escape()
                    self._consume_sequence(state, parameters, key)
                elif " " <= key <= "?":
                    # Keep malformed or excessive parameters inert until the final byte.
                    if len(self._sequence) < 64:
                        self._sequence += key
                    else:
                        self._sequence = "!"
                else:
                    self._reset_escape()
                continue

            if self.cancel_stage is not None and self._consume_cancel_key(key):
                continue
            if key.lower() == "c":
                self.reopen_cancel()
            elif key.lower() == "n" or key == "\t":
                self.page += 1
            elif key.lower() == "p":
                self.page -= 1
            elif key.lower() == "q":
                self.quit_requested = True
            elif key.lower() == "m":
                self.hide_amounts = not self.hide_amounts
            elif key == "\x05":  # Ctrl+E
                self.email_alerts = not self.email_alerts

    def _consume_cancel_key(self, key: str) -> bool:
        """Handle a key while the cancel prompt is open; False lets q, m and Ctrl+E act as usual."""
        lowered = key.lower()
        if lowered in {"q", "m", "\x05"}:
            return False
        if self.cancel_stage == "select":
            if key in "0123456789" and len(self.cancel_digits) < 4:
                self.cancel_digits += key
            elif key in {"\x7f", "\b"}:
                self.cancel_digits = self.cancel_digits[:-1]
            elif key in {"\r", "\n"} and self.cancel_digits.strip("0"):
                self.cancel_row = int(self.cancel_digits)
                self.cancel_stage = "confirm"
            elif lowered == "c":
                self.close_cancel()
        elif lowered == "y":
            self.cancel_confirmed = True
            self.close_cancel()
        elif lowered in {"n", "c"} or key in {"\x7f", "\b"}:
            self.close_cancel()
        # Other keys are inert while the prompt is open, so a typo cannot page or confirm.
        return True

    def _consume_sequence(self, state: str, parameters: str, final: str) -> None:
        if state == "csi" and parameters == "200" and final == "~":
            self._state = "paste"
            return
        if parameters and not all(character in "0123456789;" for character in parameters):
            return
        if final in {"C", "B"}:
            self.page += 1
        elif final in {"D", "A"}:
            self.page -= 1
        elif state == "csi" and final == "Z" and not parameters:
            self.page -= 1
        elif state == "csi" and final == "~":
            key = parameters.partition(";")[0]
            if key == "6":
                self.page += 1
            elif key == "5":
                self.page -= 1

    def _expire_escape(self) -> None:
        if self._escape_updated_at is not None and time.monotonic() - self._escape_updated_at >= self._ESCAPE_TIMEOUT:
            # A lone Escape that nothing followed is the Esc key itself.
            if self._state == "escape":
                self.close_cancel()
            self._reset_escape()

    def _reset_escape(self) -> None:
        self._state = "normal"
        self._sequence = ""
        self._escape_updated_at = None
        self._string_escape = False
