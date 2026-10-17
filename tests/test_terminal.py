from io import StringIO
from types import SimpleNamespace

import pytest

from active_trades import terminal
from active_trades.terminal import DashboardPager


def test_navigation_handles_split_escape_sequences():
    pager = DashboardPager()
    pager._consume("\x1b")
    assert pager.page == 0
    pager._consume("[C")
    assert pager.page == 1
    pager._consume("n\t\x1b[D")
    assert pager.page == 2
    pager._consume("p\x1b[A")
    assert pager.page == 0
    pager._consume("q")
    assert pager.quit_requested


@pytest.mark.parametrize(
    ("sequence", "direction"),
    [
        ("\x1b[C", 1), ("\x1b[B", 1), ("\x1b[D", -1), ("\x1b[A", -1),
        ("\x1bOC", 1), ("\x1bOB", 1), ("\x1bOD", -1), ("\x1bOA", -1),
        ("\x1b[1;5C", 1), ("\x1b[1;2D", -1), ("\x1bO1;2B", 1),
        ("\x1b[Z", -1), ("\x1b[5~", -1), ("\x1b[6~", 1),
        ("\x1b[5;2~", -1), ("\x1b[6;5~", 1),
    ],
)
def test_standard_navigation_sequences_work_at_every_read_boundary(sequence, direction):
    for boundary in range(1, len(sequence)):
        pager = DashboardPager()
        pager._consume(sequence[:boundary])
        assert pager.page == 0
        pager._consume(sequence[boundary:])
        assert pager.page == direction
        assert not pager.hide_amounts
        assert not pager.quit_requested


def test_letter_aliases_are_case_insensitive_and_amount_toggle_is_reversible():
    pager = DashboardPager()
    pager._consume("N\tP")
    assert pager.page == 1
    pager._consume("m")
    assert pager.hide_amounts
    pager._consume("M")
    assert not pager.hide_amounts
    assert pager.page == 1
    pager._consume("Q")
    assert pager.quit_requested


def test_amounts_can_start_hidden():
    pager = DashboardPager(hide_amounts=True)
    assert pager.hide_amounts
    pager._consume("m")
    assert not pager.hide_amounts


@pytest.mark.parametrize(
    "sequence",
    [
        "\x1b[31m", "\x1b[1q", "\x1b[?25l", "\x1b[?1C", "\x1bOP",
        "\x1bm", "\x1bq", "\x1b(m", "\x1b]title mqnp\x07",
        "\x1b]title mqnp\x1b\\", "\x1bPpayload mqnp\x1b\\",
        "\x1b[" + "1" * 100 + "C",
    ],
)
def test_unknown_sequences_never_activate_embedded_shortcuts(sequence):
    pager = DashboardPager()
    for character in sequence:
        pager._consume(character)
    assert pager.page == 0
    assert not pager.hide_amounts
    assert not pager.quit_requested
    pager._consume("mq")
    assert pager.hide_amounts
    assert pager.quit_requested


@pytest.mark.parametrize("prefix", ["\x1b", "\x1b[", "\x1b[1;", "\x1bO", "\x1b]unfinished"])
def test_stale_escape_sequences_expire_without_swallowing_the_next_shortcut(monkeypatch, prefix):
    now = [0.0]
    monkeypatch.setattr(terminal.time, "monotonic", lambda: now[0])
    pager = DashboardPager()
    pager._consume(prefix)
    now[0] += 1
    assert not pager.poll()
    pager._consume("mq")
    assert pager.hide_amounts
    assert pager.quit_requested


def test_new_escape_sequence_recovers_from_an_unfinished_sequence():
    pager = DashboardPager()
    pager._consume("\x1b[1;\x1b[Cm")
    assert pager.page == 1
    assert pager.hide_amounts


def test_bracketed_paste_contents_are_ignored_even_when_markers_are_split():
    pager = DashboardPager()
    for character in "\x1b[200~mqnp\t\x1b[C\x1b[6~\x1b[201~":
        pager._consume(character)
    assert pager.page == 0
    assert not pager.hide_amounts
    assert not pager.quit_requested
    pager._consume("mN")
    assert pager.hide_amounts
    assert pager.page == 1


def test_bracketed_paste_does_not_expire_into_shortcuts_between_reads(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(terminal.time, "monotonic", lambda: now[0])
    pager = DashboardPager()
    pager._consume("\x1b[200~mq")
    now[0] += 1
    assert not pager.poll()
    pager._consume("mq\x1b[201~m")
    assert pager.hide_amounts
    assert not pager.quit_requested


@pytest.mark.parametrize(
    ("keys", "changed"),
    [(b"m", True), (b"M", True), (b"N", True), (b"P", True), (b"Q", True),
     (b"\x1b[31m", False), (b"x", False), (b"mm", False)],
)
def test_poll_reports_only_changes_to_dashboard_controls(monkeypatch, keys, changed):
    pager = DashboardPager()
    pager._fd = 123
    monkeypatch.setattr(terminal.select, "select", lambda *_args: ([123], [], []))
    monkeypatch.setattr(terminal.os, "read", lambda *_args: keys)
    assert pager.poll() is changed


def test_poll_without_input_reports_no_change(monkeypatch):
    pager = DashboardPager()
    pager._fd = 123
    monkeypatch.setattr(terminal.select, "select", lambda *_args: ([], [], []))
    assert not pager.poll()


@pytest.mark.parametrize(("start", "end", "changed", "page"), [
    (b"\x1b", b"[C", True, 1), (b"\x1b[31", b"m", False, 0),
])
def test_ready_escape_continuations_are_preserved_after_a_delayed_poll(monkeypatch, start, end, changed, page):
    now = [0.0]
    reads = iter((start, end))
    monkeypatch.setattr(terminal.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(terminal.select, "select", lambda *_args: ([123], [], []))
    monkeypatch.setattr(terminal.os, "read", lambda *_args: next(reads))
    pager = DashboardPager()
    pager._fd = 123
    assert not pager.poll()
    now[0] += 1
    assert pager.poll() is changed
    assert pager.page == page
    assert not pager.hide_amounts
    assert not pager.quit_requested


def _mock_terminal(monkeypatch, *, input_tty=True, output_tty=True):
    import termios
    import tty

    output = StringIO()
    output.isatty = lambda: output_tty
    monkeypatch.setattr(terminal.sys, "stdin", SimpleNamespace(isatty=lambda: input_tty, fileno=lambda: 123))
    monkeypatch.setattr(terminal.sys, "stdout", output)
    monkeypatch.setattr(termios, "tcgetattr", lambda _fd: ["original"])
    monkeypatch.setattr(tty, "setcbreak", lambda _fd: None)
    restored = []
    monkeypatch.setattr(termios, "tcsetattr", lambda *args: restored.append(args))
    return output, restored


@pytest.mark.skipif(terminal.os.name != "posix", reason="Terminal setup uses POSIX cbreak mode")
@pytest.mark.parametrize(("input_tty", "output_tty"), [(True, True), (False, True), (True, False)])
def test_bracketed_paste_mode_is_enabled_and_restored_only_for_interactive_terminals(
    monkeypatch, input_tty, output_tty,
):
    output, restored = _mock_terminal(monkeypatch, input_tty=input_tty, output_tty=output_tty)
    with DashboardPager() as pager:
        assert pager.active is input_tty
        assert output.getvalue() == ("\x1b[?2004h" if input_tty and output_tty else "")
    assert not pager.active
    assert output.getvalue() == ("\x1b[?2004h\x1b[?2004l" if input_tty and output_tty else "")
    assert bool(restored) is input_tty


@pytest.mark.skipif(terminal.os.name != "posix", reason="Terminal setup uses POSIX cbreak mode")
def test_terminal_input_is_restored_even_when_paste_mode_cleanup_fails(monkeypatch):
    output, restored = _mock_terminal(monkeypatch)
    pager = DashboardPager().__enter__()

    def fail_write(_text):
        raise OSError("output closed")

    monkeypatch.setattr(output, "write", fail_write)
    with pytest.raises(OSError, match="output closed"):
        pager.__exit__()
    assert restored
    assert not pager.active
