"""Risk controls. Every trade must pass `check_trade` first; any failure means no trade.

- Emergency stop: a file named STOP next to the bot (create it with `python bot.py --stop`).
- Fixed stake: anything other than a positive amount <= MAX_STAKE_INR is refused.
- Duplicate protection: one order per round, checked in memory and in the database.
- Daily loss limit: a trade is refused if losing it could take today's realized loss,
  plus stakes still waiting for a result, past MAX_DAILY_LOSS_INR.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from config import Config
from logger import TradeStore, utc_iso


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


class RiskManager:
    def __init__(self, cfg: Config, store: TradeStore):
        self.cfg = cfg
        self.store = store
        self.tz = ZoneInfo(cfg.timezone)
        self._traded_slugs: set[str] = set()

    # --- emergency stop ---------------------------------------------------
    def emergency_stop_active(self) -> bool:
        return self.cfg.stop_file.exists()

    def trigger_emergency_stop(self, reason: str) -> None:
        self.cfg.stop_file.write_text(f"{utc_iso()} {reason}\n", encoding="utf-8")

    # --- daily loss ---------------------------------------------------------
    def day_bounds_utc(self, now_ts: float) -> tuple[str, str]:
        local_day = datetime.fromtimestamp(now_ts, tz=self.tz).date()
        start = datetime.combine(local_day, time.min, tzinfo=self.tz)
        end = start + timedelta(days=1)
        return utc_iso(start.astimezone(timezone.utc)), utc_iso(end.astimezone(timezone.utc))

    def realized_today_inr(self, now_ts: float) -> float:
        return self.store.realized_pnl_inr(*self.day_bounds_utc(now_ts))

    def daily_limit_reached(self, now_ts: float) -> bool:
        return self.realized_today_inr(now_ts) <= -self.cfg.max_daily_loss_inr

    # --- the gate -------------------------------------------------------------
    def check_trade(self, slug: str, stake_inr: float, now_ts: float) -> RiskDecision:
        if self.emergency_stop_active():
            return RiskDecision(False, "emergency stop is active")
        if not stake_inr > 0:
            return RiskDecision(False, f"invalid stake {stake_inr}")
        if stake_inr > self.cfg.max_stake_inr:
            return RiskDecision(False, f"stake ₹{stake_inr:,.2f} exceeds max ₹{self.cfg.max_stake_inr:,.2f}")
        if slug in self._traded_slugs or self.store.has_order(slug):
            return RiskDecision(False, "round already traded")

        realized = self.realized_today_inr(now_ts)
        if realized <= -self.cfg.max_daily_loss_inr:
            return RiskDecision(False, f"daily loss limit reached (₹{realized:,.2f} today)")
        worst_case = realized - self.store.open_exposure_inr() - stake_inr
        if worst_case < -self.cfg.max_daily_loss_inr:
            return RiskDecision(
                False,
                f"worst case ₹{worst_case:,.2f} would breach daily loss limit ₹{self.cfg.max_daily_loss_inr:,.2f}",
            )
        return RiskDecision(True, "ok")

    def mark_traded(self, slug: str) -> None:
        self._traded_slugs.add(slug)
