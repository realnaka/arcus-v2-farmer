"""YAML configuration models."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class AccountConfig(BaseModel):
    name: str
    address: str
    account_index: int = 0
    api_key_path: str
    api_key_public: str | None = None
    env: Literal["testnet", "mainnet"] = "testnet"
    # Empty (the default) connects directly.  Set a URL only when the machine
    # genuinely needs a proxy for outbound WebSocket traffic.
    ws_proxy: str | None = None

    @property
    def base_url(self) -> str:
        return "https://api.testnet.arcus.xyz" if self.env == "testnet" else "https://api.arcus.xyz"

    @property
    def ws_url(self) -> str:
        return "wss://api.testnet.arcus.xyz/v1/ws" if self.env == "testnet" else "wss://api.arcus.xyz/v1/ws"


class StrategyConfig(BaseModel):
    symbol: str
    direction: Literal["buy", "sell"] = "buy"
    order_size_notional: Decimal = Decimal("200")
    max_orders: int = 5
    take_profit_bps: Decimal = Decimal("3")
    grid_step_bps: Decimal = Decimal("30")
    wait_time_s: int = 300
    open_requote_s: int = 5
    requote_move_ticks: int = 1
    alo_retry_max: int = 5
    alo_retry_backoff_s: Decimal = Decimal("1.0")
    good_til_days: int = 45
    # Anti-adverse-selection guard: pause opening when the mark has fallen this
    # far below the session reference (relative, unlike perp-dex-tools' absolute
    # pause_price).  Set to 0 to disable.
    pause_drawdown_pct: Decimal = Decimal("0")


class DeadManSwitchConfig(BaseModel):
    enabled: bool = True
    lead_s: int = 180  # must be within [5, 300]
    refresh_s: int = 60


class RiskConfig(BaseModel):
    max_inventory_pct: Decimal = Decimal("50")
    account_max_dd_pct: Decimal = Decimal("30")
    margin_usage_warn_pct: Decimal = Decimal("70")
    reconcile_deviation_pct: Decimal = Decimal("1")
    leverage: int = 5
    dead_man_switch: DeadManSwitchConfig = Field(default_factory=DeadManSwitchConfig)
    watchdog_path: str = "state/arcus-farmer.watchdog"
    # Mirror of tools/watchdog.py's --stale-seconds, for display only.  Keep the
    # two in step; the dashboard used to hard-code 30s while the launchd job ran
    # with 180s, which made the panel lie about the real safety margin.
    watchdog_stale_s: int = 30


class DashboardConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8787


class EngineConfig(BaseModel):
    dry_run: bool = True
    poll_reconcile_s: int = 60
    stop_file: str = "state/STOP"


class AppConfig(BaseModel):
    account: AccountConfig
    engine: EngineConfig = Field(default_factory=EngineConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    # The spread strategy holds a fixed basket and has no per-symbol ladder, so
    # this list may be empty.  It is kept because the account/risk block is
    # shared, and because a config that also drives a grid still needs it.
    strategies: list[StrategyConfig] = Field(default_factory=list)
    storage_path: str = "state/arcus-farmer.db"
    log_dir: str = "logs"

    def strategy_for(self, symbol: str) -> StrategyConfig | None:
        for s in self.strategies:
            if s.symbol.upper() == symbol.upper():
                return s
        return None


def load_config(path: str | Path) -> AppConfig:
    p = Path(path).expanduser()
    data = yaml.safe_load(p.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"config {p} is not a mapping")
    return AppConfig.model_validate(data)
