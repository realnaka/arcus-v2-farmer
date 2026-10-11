from __future__ import annotations

from pathlib import Path

from arcus.config import load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example.yaml"


def test_example_config_loads():
    cfg = load_config(EXAMPLE)
    assert cfg.account.env == "testnet"
    assert cfg.account.base_url == "https://api.testnet.arcus.xyz"
    assert cfg.account.ws_url.endswith("/v1/ws")
    assert cfg.engine.dry_run is True
    # The spread strategy declares no per-symbol ladder, so the list is empty
    # and the account/risk block still has to load.
    assert cfg.strategies == []
    assert cfg.risk.max_inventory_pct == 80
    assert cfg.risk.dead_man_switch.lead_s == 180


def test_strategy_lookup_tolerates_an_empty_ladder():
    cfg = load_config(EXAMPLE)
    assert cfg.strategy_for("NOPE-USD") is None


def test_dead_man_lead_is_within_the_documented_window():
    cfg = load_config(EXAMPLE)
    assert 5 <= cfg.risk.dead_man_switch.lead_s <= 300


def test_websocket_proxy_defaults_to_direct_connection():
    """Live incident: macOS system SOCKS settings made every WS connect fail.

    The default must be a direct connection; a proxy is opt-in via config.
    """
    cfg = load_config(EXAMPLE)
    assert cfg.account.ws_proxy is None
