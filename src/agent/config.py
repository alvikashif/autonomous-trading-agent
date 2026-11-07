from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

import yaml

from .costs import CostConfig
from .risk import RiskLimits
from .shariah import Thresholds
from .strategy import StrategyConfig


@dataclass(frozen=True)
class UniverseConfig:
    nse_list_csv: str = "data/nifty500_shariah.csv"
    fundamentals_csv: str = "data/fundamentals.csv"
    screen_report_csv: str = "data/screen_report.csv"
    prices_cache_csv: str = "data/prices_cache.csv"
    paper_state_json: str = "data/paper_state.json"
    monitor_status_json: str = "data/monitor_status.json"
    excluded_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortfolioConfig:
    monthly_contribution: float = 5000.0
    min_trade_value: float = 1500.0
    cash_buffer_pct: float = 0.01
    rebalance_months: int = 3  # contributions pile up as cash between rebalances: fewer, bigger trades
    stop_loss_pct: float = 0.0  # trailing stop from the highest close since purchase; 0 = off
    alert_drop_pct: float = 0.15  # monitor alerts (no action) when a holding is this far below its peak


@dataclass(frozen=True)
class Config:
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    shariah: Thresholds = field(default_factory=Thresholds)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskLimits = field(default_factory=RiskLimits)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    costs: CostConfig = field(default_factory=CostConfig)


def _build(cls, raw: dict):
    allowed = {f.name for f in fields(cls)}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown config keys for {cls.__name__}: {sorted(unknown)}")
    kwargs = dict(raw)
    if "excluded_symbols" in kwargs:
        kwargs["excluded_symbols"] = tuple(kwargs["excluded_symbols"] or ())
    return cls(**kwargs)


def load_config(path: str | Path = "config.yaml") -> Config:
    p = Path(path)
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    raw = raw or {}
    sections = {f.name: f.type for f in fields(Config)}
    unknown = set(raw) - set(sections)
    if unknown:
        raise ValueError(f"unknown config sections: {sorted(unknown)}")
    defaults = Config()
    built = {}
    for name in sections:
        default_obj = getattr(defaults, name)
        assert is_dataclass(default_obj)
        built[name] = _build(type(default_obj), raw[name]) if name in raw else default_obj
    return Config(**built)
