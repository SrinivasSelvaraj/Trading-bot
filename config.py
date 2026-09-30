"""User settings.

Values can be overridden in a `.env` file next to this module (see `.env.example`)
or with real environment variables. Environment variables win over `.env`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines) so we don't need an extra dependency."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else float(raw)


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else raw.strip()


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    # --- Mode -------------------------------------------------------------
    paper_mode: bool = True

    # --- Money ------------------------------------------------------------
    stake_inr: float = 1100.0          # fixed amount per traded round
    max_stake_inr: float = 1100.0      # hard ceiling; anything above is rejected
    max_daily_loss_inr: float = 5000.0
    inr_per_usd: float = 96.0          # Polymarket settles in USDC; used to convert ₹ <-> $

    # --- Execution realism (paper fills) -----------------------------------
    max_entry_price: float = 0.99      # never "buy" above this price per share
    taker_fee_rate: float = 0.07       # Polymarket crypto taker fee: fee = C * rate * p * (1 - p)

    # --- Timing -----------------------------------------------------------
    poll_seconds: float = 2.0
    no_trade_last_seconds: float = 3.0  # don't enter in the final seconds of a round
    max_book_age_seconds: float = 15.0  # order book older than this is treated as stale
    max_prob_sum_deviation: float = 5.0  # |UP% + DOWN% - 100| above this => data looks wrong

    # --- Housekeeping -----------------------------------------------------
    timezone: str = "Asia/Kolkata"      # day boundary for the daily loss limit
    db_path: Path = BASE_DIR / "trades.db"
    csv_path: Path = BASE_DIR / "trades.csv"
    log_path: Path = BASE_DIR / "bot.log"
    stop_file: Path = BASE_DIR / "STOP"
    status_path: Path = BASE_DIR / "status.json"  # heartbeat read by the dashboard

    # --- Optional browser mirror -------------------------------------------
    browser_enabled: bool = False
    browser_headless: bool = False
    browser_crosscheck: bool = False     # if True, page value must agree with API before trading
    browser_tolerance_pct: float = 2.0
    browser_executable: str = ""         # optional path to Chrome/Chromium

    def validate(self) -> None:
        if self.stake_inr <= 0:
            raise ConfigError("STAKE_INR must be positive")
        if self.stake_inr > self.max_stake_inr:
            raise ConfigError(
                f"STAKE_INR ({self.stake_inr}) is above MAX_STAKE_INR ({self.max_stake_inr})"
            )
        if self.max_daily_loss_inr <= 0:
            raise ConfigError("MAX_DAILY_LOSS_INR must be positive")
        if self.inr_per_usd <= 0:
            raise ConfigError("INR_PER_USD must be positive")
        if not 0 < self.max_entry_price < 1:
            raise ConfigError("MAX_ENTRY_PRICE must be between 0 and 1 (exclusive)")
        if self.taker_fee_rate < 0:
            raise ConfigError("TAKER_FEE_RATE cannot be negative")
        if self.poll_seconds <= 0:
            raise ConfigError("POLL_SECONDS must be positive")

    @property
    def stake_usd(self) -> float:
        return self.stake_inr / self.inr_per_usd


def load_config(env_file: Path | None = None) -> Config:
    _load_dotenv(env_file or BASE_DIR / ".env")
    cfg = Config(
        paper_mode=_bool("PAPER_MODE", True),
        stake_inr=_float("STAKE_INR", 1100.0),
        max_stake_inr=_float("MAX_STAKE_INR", 1100.0),
        max_daily_loss_inr=_float("MAX_DAILY_LOSS_INR", 5000.0),
        inr_per_usd=_float("INR_PER_USD", 96.0),
        max_entry_price=_float("MAX_ENTRY_PRICE", 0.99),
        taker_fee_rate=_float("TAKER_FEE_RATE", 0.07),
        poll_seconds=_float("POLL_SECONDS", 2.0),
        no_trade_last_seconds=_float("NO_TRADE_LAST_SECONDS", 3.0),
        max_book_age_seconds=_float("MAX_BOOK_AGE_SECONDS", 15.0),
        max_prob_sum_deviation=_float("MAX_PROB_SUM_DEVIATION", 5.0),
        timezone=_str("TIMEZONE", "Asia/Kolkata"),
        db_path=Path(_str("DB_PATH", str(BASE_DIR / "trades.db"))),
        csv_path=Path(_str("CSV_PATH", str(BASE_DIR / "trades.csv"))),
        log_path=Path(_str("LOG_PATH", str(BASE_DIR / "bot.log"))),
        stop_file=Path(_str("STOP_FILE", str(BASE_DIR / "STOP"))),
        status_path=Path(_str("STATUS_PATH", str(BASE_DIR / "status.json"))),
        browser_enabled=_bool("BROWSER_ENABLED", False),
        browser_headless=_bool("BROWSER_HEADLESS", False),
        browser_crosscheck=_bool("BROWSER_CROSSCHECK", False),
        browser_tolerance_pct=_float("BROWSER_TOLERANCE_PCT", 2.0),
        browser_executable=_str("BROWSER_EXECUTABLE", ""),
    )
    cfg.validate()
    return cfg
