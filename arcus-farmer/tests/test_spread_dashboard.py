"""看板盈亏口径：总 / 已实现 / 未实现。

ADL 复位会把一条腿平掉再开，未实现瞬间归零；只看未实现就分不出「刚刚落袋的
浮盈」和「本来就没赚」。所以看板要有账户口径的累计 PnL。
"""
from __future__ import annotations

import pytest

from tools.spread_dashboard import pnl_breakdown


def test_total_pnl_is_equity_minus_net_deposits():
    got = pnl_breakdown(equity=1617.36, net_deposits=1717.91, upnl_total=-2.6)
    assert got["total_pnl"] == pytest.approx(-100.55)
    assert got["total_pnl_pct"] == pytest.approx(-5.853, abs=1e-3)
    # 已实现 = 总 − 未实现：费用与资金费都落在这里，复位后也不会归零
    assert got["realized_pnl"] == pytest.approx(-97.95)


def test_a_profitable_leg_that_gets_reset_still_shows_up():
    """复位前那条腿贡献 +140 的浮盈，复位后未实现归零，但总/已实现要留住。"""
    before = pnl_breakdown(equity=1628.62, net_deposits=1717.91, upnl_total=140.47)
    after = pnl_breakdown(equity=1617.49, net_deposits=1717.91, upnl_total=-2.62)
    assert before["total_pnl"] == pytest.approx(-89.29)
    assert after["total_pnl"] == pytest.approx(-100.42)
    assert after["realized_pnl"] == pytest.approx(-97.80)
    # 未实现从 +140 掉到 ~0，总盈亏不会跟着消失（只是换成已实现 + 价差移动）
    assert abs(after["total_pnl"] - before["total_pnl"]) < 12


def test_missing_net_deposits_never_reads_as_a_total_loss():
    got = pnl_breakdown(equity=1617.36, net_deposits=0.0, upnl_total=-2.6)
    assert got["total_pnl"] is None and got["realized_pnl"] is None
    assert got["total_pnl_pct"] is None
