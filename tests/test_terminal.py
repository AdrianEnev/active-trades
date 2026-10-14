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
