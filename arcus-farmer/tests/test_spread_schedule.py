from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

import tools.spread_schedule as sched
from tools.spread_schedule import (adl_reset_cfg, adl_reset_step, adl_trigger_legs,
                                   leg_margin_notional, leg_pnl_margin_pct, run_sync)
from tools.spread_schedule import (diff_legs, hold_action, hold_mode,
                                   read_ladder_level, read_ladder_state,
                                   target_leverage)

# boost_multiplier 1.0 keeps the window out of the picture, so the numbers below
# depend on drawdown only.
V2 = {
    "leverage": {"base_per_side": 3.0, "boost_multiplier": 1.0},
    "stops": {"ladder": [
        {"trigger_pct": 12.0, "action": "de_lever", "target_per_side": 2.0},
        {"trigger_pct": 20.0, "action": "de_lever", "target_per_side": 1.5},
        {"trigger_pct": 30.0, "action": "flatten_and_halt"},
    ]},
    "mode": {"hold_rebalance": True},
}
PEAK = Decimal("1000")


def test_no_ladder_rung_while_drawdown_is_small():
    lev, why, level = target_leverage(V2, Decimal("970"), PEAK)
    assert lev == Decimal("3.0")
    assert level == 0
    assert "窗口" in why


def test_first_rung_caps_leverage_and_reports_level():
    lev, why, level = target_leverage(V2, Decimal("870"), PEAK)
    assert lev == Decimal("2.0")
    assert level == 1
    assert "12.0%" in why


def test_deepest_matching_rung_wins():
    lev, _why, level = target_leverage(V2, Decimal("790"), PEAK)
    assert lev == Decimal("1.5")
    assert level == 2


def test_flatten_rung_is_the_level_past_the_ladder():
    lev, why, level = target_leverage(V2, Decimal("690"), PEAK)
    assert lev == Decimal(0)
    assert level == len(V2["stops"]["ladder"]) + 1
    assert "熔断" in why


def test_hold_mode_defaults_to_off():
    assert hold_mode({"mode": {"hold_rebalance": True}}) is True
    assert hold_mode({"mode": {"hold_rebalance": False}}) is False
    assert hold_mode({}) is False


def test_hold_action_matrix():
    fresh = {}
    fired = {"level": 1, "targets": {"MU-USD": "1.0"}}
    # 不在 hold 模式 -> 保持原来的调仓行为
    assert hold_action(False, 0, fresh, cycle=1) == "rebalance"
    # hold + 没档位 -> 只撤遗留挂单
    assert hold_action(True, 0, fresh, cycle=1) == "cancel"
    # hold + 新档位 -> 立刻降杠杆
    assert hold_action(True, 1, fresh, cycle=1) == "fire"
    assert hold_action(True, 2, fired, cycle=1) == "fire"
    # hold + 同档位 -> 其余轮次不动手
    assert hold_action(True, 1, fired, cycle=1) == "idle"
    assert hold_action(True, 1, fired, cycle=2) == "idle"
    # hold + 同档位 + 每 4 轮 -> 只用冻结目标补没跑完的减仓
    assert hold_action(True, 1, fired, cycle=4) == "residual"
    # 没有冻结目标时连补单也不做
    assert hold_action(True, 1, {"level": 1}, cycle=4) == "idle"


def test_ladder_state_reads_level_and_frozen_targets(tmp_path):
    path = tmp_path / "spread_ladder.json"
    path.write_text(json.dumps({"level": 2, "targets": {"MU-USD": "-1.0"}}))
    assert read_ladder_level(path) == 2
    assert read_ladder_state(path)["targets"] == {"MU-USD": "-1.0"}


def test_ladder_state_survives_a_missing_or_broken_file(tmp_path):
    missing = tmp_path / "nope.json"
    assert read_ladder_level(missing) == 0
    broken = tmp_path / "broken.json"
    broken.write_text("")
    assert read_ladder_level(broken) == 0
    assert read_ladder_state(broken) == {}


def _market(step: str = "0.01", mark: str = "100"):
    return SimpleNamespace(step_size=Decimal(step), mark_price=Decimal(mark),
                           oracle_price=Decimal(mark), tick_size=Decimal(step),
                           market_id=1)


def test_frozen_target_ignores_small_drift():
    """hold 模式靠这个：目标冻结后，小额漂移必须不产生订单。"""
    markets = {"MU-USD": _market("0.001", "1000"), "DRAM-USD": _market("0.01", "58")}
    mids = {"MU-USD": Decimal("1000"), "DRAM-USD": Decimal("58")}
    targets = {"MU-USD": Decimal("1.0"), "DRAM-USD": Decimal("-40.0")}
    pos = {"MU-USD": SimpleNamespace(size=Decimal("1.0")),
           "DRAM-USD": SimpleNamespace(size=Decimal("-39.5"))}      # 只差 $29
    assert diff_legs(targets, pos, markets, mids, Decimal("50")) == []


def test_frozen_target_still_reduces_a_real_gap():
    markets = {"MU-USD": _market("0.001", "1000"), "DRAM-USD": _market("0.01", "58")}
    mids = {"MU-USD": Decimal("1000"), "DRAM-USD": Decimal("58")}
    targets = {"MU-USD": Decimal("1.0"), "DRAM-USD": Decimal("-40.0")}
    pos = {"MU-USD": SimpleNamespace(size=Decimal("1.0")),
           "DRAM-USD": SimpleNamespace(size=Decimal("-41.5"))}      # 差 $87，且是超配
    legs = diff_legs(targets, pos, markets, mids, Decimal("50"))
    assert [l["symbol"] for l in legs] == ["DRAM-USD"]
    assert legs[0]["reducing"] is True


# ------------------------------------------------------------------ ADL 复位

ADL_V2 = {
    "execution": {"min_order_notional": 5, "fill_timeout_s": 300, "through_bps": 40},
    "adl_reset": {"enabled": True, "leg_pnl_pct": 10.0, "fill_timeout_s": 300,
                  "cooldown_s": 1800, "notify": True},
}


def _adl_market(market_id: int, step: str = "0.01", mark: str = "100",
                tick: str = "0.01", imf: str = "0.10"):
    return SimpleNamespace(step_size=Decimal(step), mark_price=Decimal(mark),
                           oracle_price=Decimal(mark), tick_size=Decimal(tick),
                           market_id=market_id, active_initial_margin_fraction=Decimal(imf))


def _adl_markets():
    return {"MU-USD": _adl_market(1, "0.001", "1000"),
            "SKHY-USD": _adl_market(2, "0.001", "180"),
            "SNDK-USD": _adl_market(3, "0.001", "1700"),
            "DRAM-USD": _adl_market(4, "0.01", "58")}


def _adl_mids():
    return {sym: mk.mark_price for sym, mk in _adl_markets().items()}


def _pos(size: str, upnl: str, margin: str, notional: str):
    return SimpleNamespace(size=Decimal(size), unrealized_pnl=Decimal(upnl),
                           margin_used=Decimal(margin), notional=Decimal(notional))


class FakeRest:
    def __init__(self, positions: list | None = None, *, reject_alo: bool = False):
        self.placed: list[dict] = []
        self.cancelled: list[tuple] = []
        self._positions = positions or []
        self.reject_alo = reject_alo

    async def place_order(self, **kw):
        self.placed.append(kw)
        if self.reject_alo and kw.get("tif") == "ALO":
            return {"orderId": f"rej-{len(self.placed)}", "status": "REJECTED"}
        return {"orderId": f"oid-{len(self.placed)}", "status": "OPEN"}

    async def cancel_order(self, *, market_id, order_id):
        self.cancelled.append((market_id, order_id))
        return {"status": "CANCELED"}

    async def positions(self, account_index=None, address=None):
        return {"positions": self._positions}


@pytest.fixture
def adl_paths(tmp_path, monkeypatch):
    """把复位用的三个文件钉到 tmp_path，并吃掉飞书推送。"""
    monkeypatch.setattr(sched, "ADL_STATE", tmp_path / "spread_adl_reset.json")
    monkeypatch.setattr(sched, "ADL_LOG", tmp_path / "adl_reset_log.jsonl")
    monkeypatch.setattr(sched, "PENDING", tmp_path / "spread_pending.json")
    sent: list[str] = []
    monkeypatch.setattr(sched, "notify_adl", lambda text, idem: sent.append(text))
    return SimpleNamespace(state=sched.ADL_STATE, log=sched.ADL_LOG,
                           pending=sched.PENDING, sent=sent)


@pytest.fixture
def no_sleep(monkeypatch):
    """run_sync 里的节流 sleep 对断言没意义，去掉能把 9s 的套件压到 1s 内。"""
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(sched, "asyncio", SimpleNamespace(sleep=_sleep))


def test_adl_reset_cfg_defaults_and_override():
    empty = adl_reset_cfg({"execution": {"fill_timeout_s": 300}})
    assert empty["enabled"] is False
    assert empty["leg_pnl_pct"] == Decimal("10.0")
    assert empty["fill_timeout_s"] == 300          # 没写就沿用 execution 的
    got = adl_reset_cfg({"execution": {}, "adl_reset": {"enabled": True, "leg_pnl_pct": 7.5,
                                                       "fill_timeout_s": 60, "cooldown_s": 5,
                                                       "notify": False}})
    assert got["enabled"] is True and got["fill_timeout_s"] == 60
    assert got["cooldown_s"] == 5 and got["notify"] is False


def test_adl_pnl_pct_prefers_margin_used_then_falls_back_to_imf():
    mk = _adl_market(1, mark="1000", imf="0.10")
    with_margin = _pos("1", "33", "100", "1000")
    assert leg_pnl_margin_pct(with_margin, mk) == pytest.approx(33.0)
    no_margin = SimpleNamespace(size=Decimal("1"), unrealized_pnl=Decimal("10"),
                                margin_used=None, notional=Decimal("1000"))
    margin, notional = leg_margin_notional(no_margin, mk)
    assert (margin, notional) == (Decimal("100.0"), Decimal("1000"))
    assert leg_pnl_margin_pct(no_margin, mk) == pytest.approx(10.0)
    # 名义缺失时由 mark 反推
    bare = SimpleNamespace(size=Decimal("2"), unrealized_pnl=Decimal("20"),
                           margin_used=None, notional=None)
    assert leg_margin_notional(bare, mk) == (Decimal("200.00"), Decimal("2000"))


def test_adl_trigger_legs_threshold_dust_and_order():
    markets = _adl_markets()
    pos = {
        "MU-USD": _pos("1", "120", "1000", "1000"),        # +12% -> 触发
        "SKHY-USD": _pos("5", "9.9", "1000", "900"),       # +0.99% -> 不触发
        "DRAM-USD": _pos("-40", "300", "1000", "2320"),    # +30% -> 触发（空中第一）
        "SNDK-USD": _pos("1", "5", "2", "3"),              # 灰尘：名义 < $5
    }
    hits = adl_trigger_legs(ADL_V2, markets, pos)
    assert [h["symbol"] for h in hits] == ["DRAM-USD", "MU-USD"]
    assert hits[0]["pnl_margin_pct"] == pytest.approx(30.0)
    assert hits[0]["side"] == "空"
    assert adl_trigger_legs({**ADL_V2, "adl_reset": {"enabled": False}}, markets, pos) == []


def test_adl_reset_triggers_and_quotes_maker_closes(adl_paths):
    markets, mids = _adl_markets(), _adl_mids()
    pos = {"MU-USD": _pos("1", "150", "1000", "1000"),
           "DRAM-USD": _pos("-40", "80", "1000", "2320")}
    rest = FakeRest()
    acted = asyncio.run(adl_reset_step(rest, ADL_V2, markets, mids, pos, Decimal("1700")))
    assert acted is True
    # 两条腿都是减仓：方向必须是反向的，而且带 reduce_only
    sides = {p["market_id"]: p["side"] for p in rest.placed}
    assert sides == {1: "SELL", 4: "BUY"}
    assert all(p["reduce_only"] is True and p["tif"] == "ALO" for p in rest.placed)
    st = json.loads(adl_paths.state.read_text())
    assert st["stage"] == "closing"
    assert [h["symbol"] for h in st["trigger"]] == ["MU-USD"]      # +15%
    assert st["targets"] == {"MU-USD": "1", "DRAM-USD": "-40"}
    events = [json.loads(l)["event"] for l in adl_paths.log.read_text().splitlines()]
    assert events == ["trigger"]
    assert adl_paths.sent and "ADL 复位触发" in adl_paths.sent[0]


def test_adl_reset_closing_timeout_converts_remainder_to_taker(adl_paths):
    markets, mids = _adl_markets(), _adl_mids()
    adl_paths.state.write_text(json.dumps({"stage": "closing", "targets": {"MU-USD": "1.0"}}))
    adl_paths.pending.write_text(json.dumps({
        "MU-USD": {"order_id": "stale", "market_id": 1, "ts": time.time() - 999,
                   "side": "SELL", "size": "1.0"}}))
    pos = {"MU-USD": _pos("1", "150", "1000", "1000")}
    rest = FakeRest()
    acted = asyncio.run(adl_reset_step(rest, ADL_V2, markets, mids, pos, Decimal("1700")))
    assert acted is True
    assert rest.cancelled == [(1, "stale")]
    assert [(p["tif"], p["reduce_only"], p["side"]) for p in rest.placed] == [("IOC", True, "SELL")]


def test_adl_reset_maker_close_waits_before_switching(adl_paths):
    """5 分钟没到就必须继续等 —— 不许提前吃单。"""
    markets, mids = _adl_markets(), _adl_mids()
    adl_paths.state.write_text(json.dumps({"stage": "closing", "targets": {"MU-USD": "1.0"}}))
    adl_paths.pending.write_text(json.dumps({
        "MU-USD": {"order_id": "fresh", "market_id": 1, "ts": time.time() - 30,
                   "side": "SELL", "size": "1.0"}}))
    pos = {"MU-USD": _pos("1", "150", "1000", "1000")}
    rest = FakeRest()
    asyncio.run(adl_reset_step(rest, ADL_V2, markets, mids, pos, Decimal("1700")))
    assert rest.placed == [] and rest.cancelled == []


def test_adl_reset_reopens_after_flat(adl_paths):
    markets, mids = _adl_markets(), _adl_mids()
    adl_paths.state.write_text(json.dumps({
        "stage": "closing", "started_ts": time.time(),
        "targets": {"MU-USD": "1.0", "DRAM-USD": "-40"}}))
    rest = FakeRest()
    acted = asyncio.run(adl_reset_step(rest, ADL_V2, markets, mids, {}, Decimal("1700")))
    assert acted is True
    assert [p["market_id"] for p in rest.placed] == [4]         # 对冲腿先走
    assert rest.placed[0]["tif"] == "ALO" and rest.placed[0]["reduce_only"] is False
    st = json.loads(adl_paths.state.read_text())
    assert st["stage"] == "opening"
    assert [json.loads(l)["event"] for l in adl_paths.log.read_text().splitlines()] == ["flat"]


def test_adl_reset_completes_when_target_is_met(adl_paths):
    markets, mids = _adl_markets(), _adl_mids()
    adl_paths.state.write_text(json.dumps({
        "stage": "opening", "started_ts": time.time() - 42,
        "trigger": [{"symbol": "MU-USD", "pnl_margin_pct": 11.0}],
        "targets": {"MU-USD": "1.0"}}))
    pos = {"MU-USD": _pos("1", "0", "1000", "1000")}
    rest = FakeRest()
    acted = asyncio.run(adl_reset_step(rest, ADL_V2, markets, mids, pos, Decimal("1700")))
    assert acted is False and rest.placed == []
    st = json.loads(adl_paths.state.read_text())
    assert st["stage"] == "" and st["done_ts"] > 0 and st["targets"] == {"MU-USD": "1.0"}
    events = [json.loads(l)["event"] for l in adl_paths.log.read_text().splitlines()]
    assert events == ["done"]
    assert adl_paths.sent and "ADL 复位完成" in adl_paths.sent[0]


def test_adl_reset_waits_out_its_cooldown(adl_paths):
    markets, mids = _adl_markets(), _adl_mids()
    adl_paths.state.write_text(json.dumps({"stage": "", "done_ts": time.time() - 60}))
    pos = {"MU-USD": _pos("1", "150", "1000", "1000")}         # 会触发，但还在冷却
    rest = FakeRest()
    assert asyncio.run(adl_reset_step(rest, ADL_V2, markets, mids, pos,
                                      Decimal("1700"))) is False
    assert rest.placed == [] and adl_paths.sent == []
    adl_paths.state.write_text(json.dumps({"stage": "", "done_ts": time.time() - 3600}))
    assert asyncio.run(adl_reset_step(rest, ADL_V2, markets, mids, pos,
                                      Decimal("1700"))) is True
    assert len(rest.placed) == 1


def test_adl_reset_abandons_under_halt(adl_paths):
    """HALT 只允许减仓：在途的重开必须放弃，不能绕过去。"""
    markets, mids = _adl_markets(), _adl_mids()
    adl_paths.state.write_text(json.dumps({"stage": "opening", "started_ts": time.time(),
                                           "targets": {"MU-USD": "1.0"}}))
    rest = FakeRest()
    acted = asyncio.run(adl_reset_step(rest, ADL_V2, markets, mids, {}, Decimal("1700"),
                                       blocked=True))
    assert acted is False and rest.placed == []
    st = json.loads(adl_paths.state.read_text())
    assert st["stage"] == "" and "熔断/HALT" in st["note"]


def test_run_sync_reduce_is_taker_by_default_and_maker_when_asked(adl_paths):
    markets, mids = _adl_markets(), _adl_mids()
    leg = {"symbol": "MU-USD", "market_id": 1, "side": "SELL", "size": Decimal("1"),
           "price": Decimal("1000"), "want": Decimal(0), "have": Decimal("1"),
           "delta": Decimal("-1"), "reducing": True}
    v2 = {"execution": {"fill_timeout_s": 300, "through_bps": 40, "min_order_notional": 5}}
    rest = FakeRest()
    asyncio.run(run_sync(rest, v2, [dict(leg)], markets, mids))
    assert rest.placed[0]["tif"] == "IOC" and rest.placed[0]["reduce_only"] is True
    rest2 = FakeRest()
    asyncio.run(run_sync(rest2, v2, [dict(leg)], markets, mids, maker_first_reduce=True,
                         timeout=300))
    assert rest2.placed[0]["tif"] == "ALO" and rest2.placed[0]["reduce_only"] is True
    # 挂单记进 pending，下一轮才知道该等多久
    assert json.loads(adl_paths.pending.read_text())["MU-USD"]["order_id"] == "oid-1"


def test_refused_alo_still_starts_the_taker_clock(adl_paths, no_sleep):
    """两次都被拒时盘口上没有挂单，但必须记时 —— 否则这一腿永远走不到 taker。"""
    markets, mids = _adl_markets(), _adl_mids()
    v2 = {"execution": {"fill_timeout_s": 300, "through_bps": 40, "min_order_notional": 5}}
    leg = {"symbol": "MU-USD", "market_id": 1, "side": "SELL", "size": Decimal("1"),
           "price": Decimal("1000"), "want": Decimal(0), "have": Decimal("1"),
           "delta": Decimal("-1"), "reducing": True}
    rest = FakeRest(reject_alo=True)
    asyncio.run(run_sync(rest, v2, [dict(leg)], markets, mids, maker_first_reduce=True))
    assert [p["tif"] for p in rest.placed] == ["ALO", "ALO"]
    ent = json.loads(adl_paths.pending.read_text())["MU-USD"]
    assert ent["order_id"] == "" and ent["refused"] is True
    # 计时到期后：不撤单（没有单可撤），直接 taker
    ent["ts"] = time.time() - 999
    adl_paths.pending.write_text(json.dumps({"MU-USD": ent}))
    rest2 = FakeRest()
    asyncio.run(run_sync(rest2, v2, [dict(leg)], markets, mids, maker_first_reduce=True))
    assert [(p["tif"], p["reduce_only"], p["side"]) for p in rest2.placed] == [("IOC", True, "SELL")]
    assert rest2.cancelled == []
    assert json.loads(adl_paths.pending.read_text()) == {}


def test_hedge_leg_takes_after_two_rejections(adl_paths, no_sleep):
    """对冲腿是重开的闸门：两次 ALO 被拒就必须吃，不能让多腿无限等下去。"""
    markets, mids = _adl_markets(), _adl_mids()
    v2 = {"execution": {"fill_timeout_s": 300, "through_bps": 40, "min_order_notional": 5}}
    legs = [{"symbol": "DRAM-USD", "market_id": 4, "side": "SELL", "size": Decimal("40"),
             "price": Decimal("58"), "want": Decimal("-40"), "have": Decimal(0),
             "delta": Decimal("-40"), "reducing": False}]
    rest = FakeRest(reject_alo=True)
    asyncio.run(run_sync(rest, v2, legs, markets, mids))
    assert [(p["tif"], p["side"]) for p in rest.placed] == [("ALO", "SELL"), ("ALO", "SELL"),
                                                            ("IOC", "SELL")]
    assert rest.placed[-1]["reduce_only"] is False
    assert rest.placed[-1]["price"] < Decimal("58")      # 40 bps 穿越，不是原价


def test_adl_reset_close_method_taker_cuts_immediately(adl_paths, no_sleep):
    """close_method: taker 时平仓不等 5 分钟；maker_first（默认）才挂 ALO。"""
    markets, mids = _adl_markets(), _adl_mids()
    v2 = {**ADL_V2, "adl_reset": {**ADL_V2["adl_reset"], "close_method": "taker"}}
    adl_paths.state.write_text(json.dumps({"stage": "closing",
                                           "targets": {"MU-USD": "1.0"}}))
    pos = {"MU-USD": _pos("1", "150", "1000", "1000")}
    rest = FakeRest()
    asyncio.run(adl_reset_step(rest, v2, markets, mids, pos, Decimal("1700")))
    assert [(p["tif"], p["reduce_only"]) for p in rest.placed] == [("IOC", True)]
