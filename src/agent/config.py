from __future__ import annotations

import os
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
    swing_status_json: str = "data/swing_status.json"
    ai_signals_json: str = "data/ai_signals.json"
    kill_switch_json: str = "data/kill_switch.json"
    notifications_json: str = "data/notifications.json"
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
class SwingConfig:
    """Short-term PAPER sandbox. Replayed from start_date, so there is no extra state file to corrupt."""
    start_date: str = "2026-11-09"
    capital: float = 50000.0
    lookback: int = 20
    trend_days: int = 50
    rank_days: int = 60
    target_pct: float = 0.08
    stop_pct: float = 0.04
    max_hold_days: int = 10
    max_positions: int = 2


@dataclass(frozen=True)
class BrokerConfig:
    mode: str = "paper"  # "paper" or "live"
    api_key: str = ""
    totp_secret: str = ""
    token_cache_path: str = "data/groww_token.json"

    def resolved_mode(self) -> str:
        env_mode = os.environ.get("BROKER_MODE", "").strip().lower()
        if env_mode in ("live", "paper"):
            return env_mode
        if os.environ.get("AI_AUTO_TRADE", "").strip().lower() == "true":
            return "live"
        return self.mode.strip().lower() or "paper"

    def resolved_api_key(self) -> str:
        return os.environ.get("GROWW_API_KEY", "").strip() or self.api_key.strip()

    def resolved_totp_secret(self) -> str:
        return os.environ.get("GROWW_TOTP_SECRET", "").strip() or self.totp_secret.strip()


DEFAULT_AI_MODELS: dict[str, str] = {
    "claude": "claude-sonnet-5-5",
    "deepseek": "deepseek-reasoner",
    "openai": "o3-mini",
    "gemini": "gemini-3.5-flash",
}


@dataclass(frozen=True)
class AIBrainConfig:
    provider: str = ""          # "claude", "deepseek", "openai", "gemini"
    model: str = ""             # model ID override, or uses default for the provider
    min_confidence: float = 8.0 # 1.0 to 10.0 scale, minimum score to consider execution
    max_allocation: float = 5000.0  # max rupees per position (e.g. Rs 5k test)
    max_positions: int = 1      # max concurrent swing positions
    target_pct: float = 0.20    # +20% multi-month growth target (trailing up to +30%+)
    stop_pct: float = 0.04      # -4% stop loss (triggers AI stop consultation)
    hard_stop_pct: float = 0.07 # -7% maximum hard loss floor (absolute safety cutoff)
    max_hold_days: int = 60     # up to 60 trading days (multi-month patient holding)

    def resolved_provider(self) -> str:
        return os.environ.get("AI_PROVIDER", "").strip().lower() or self.provider.strip().lower() or "claude"

    def resolved_model(self) -> str:
        m = os.environ.get("AI_MODEL", "").strip() or self.model.strip()
        if m:
            return m
        prov = self.resolved_provider()
        return DEFAULT_AI_MODELS.get(prov, "claude-3-5-sonnet-latest")

    def resolved_api_key(self) -> str:
        prov = self.resolved_provider()
        if prov == "claude":
            return os.environ.get("ANTHROPIC_API_KEY", "").strip()
        elif prov == "deepseek":
            return os.environ.get("DEEPSEEK_API_KEY", "").strip()
        elif prov == "openai":
            return os.environ.get("OPENAI_API_KEY", "").strip()
        elif prov == "gemini":
            return os.environ.get("GEMINI_API_KEY", "").strip()
        return ""

    def resolved_min_confidence(self) -> float:
        val = os.environ.get("AI_MIN_CONFIDENCE", "").strip()
        try:
            return float(val) if val else self.min_confidence
        except ValueError:
            return self.min_confidence

    def resolved_max_allocation(self) -> float:
        val = os.environ.get("AI_MAX_ALLOCATION", "").strip()
        try:
            return float(val) if val else self.max_allocation
        except ValueError:
            return self.max_allocation

    def resolved_max_positions(self) -> int:
        val = os.environ.get("AI_MAX_POSITIONS", "").strip()
        try:
            return int(val) if val else self.max_positions
        except ValueError:
            return self.max_positions

    def resolved_target_pct(self) -> float:
        val = os.environ.get("AI_TARGET_PCT", "").strip()
        try:
            return float(val) if val else self.target_pct
        except ValueError:
            return self.target_pct

    def resolved_max_hold_days(self) -> int:
        val = os.environ.get("AI_MAX_HOLD_DAYS", "").strip()
        try:
            return int(val) if val else self.max_hold_days
        except ValueError:
            return self.max_hold_days


@dataclass(frozen=True)
class Config:
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    shariah: Thresholds = field(default_factory=Thresholds)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskLimits = field(default_factory=RiskLimits)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    swing: SwingConfig = field(default_factory=SwingConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    ai: AIBrainConfig = field(default_factory=AIBrainConfig)


def load_dotenv(path: str | Path = ".env") -> None:
    """Load key=value lines from .env into os.environ if not already present."""
    p = Path(path)
    if not p.is_file():
        return
    try:
        content = p.read_text(encoding="utf-8")
    except OSError:
        return
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val


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
    load_dotenv()
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
