"""Polymarket BTC 5-minute bot: main loop (PAPER TRADING ONLY).

    python bot.py            run continuously (Ctrl+C to stop)
    python bot.py --check    one-shot: show the live round, UP/DOWN and what the rule says
    python bot.py --stop     emergency stop: tells a running bot to stop (creates the STOP file)
    python report.py         summary of recorded results

Every poll the bot finds the live round, reads the UP/DOWN order books, applies the
98% rule and, if it fires and every risk check passes, records a paper trade at the
real ask prices. When a round ends, a row is written; once Polymarket resolves it,
the result and P/L are filled in and the row is appended to trades.csv.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from browser import BrowserMirror
from config import Config, ConfigError, load_config
from logger import TradeStore, setup_logging, utc_iso
from market_detector import MarketDetector, MarketVerificationError, Round, round_start_ts
from polymarket_api import ApiError, PolymarketClient
from quotes import Book, displayed_probability, parse_book, simulate_buy, to_pct, with_complement_asks
from risk_manager import RiskManager
from strategy import CONFLICT, INVALID, NO_TRADE, THRESHOLD_PCT, TRADE_DECISIONS, get_decision

log = logging.getLogger("bot")

LIVE_NOT_AVAILABLE = (
    "PAPER_MODE is False, but live order execution is not implemented in this version.\n"
    "Set PAPER_MODE=True. Live trading should only be added after the paper results\n"
    "have been reviewed (see README)."
)
SETTLE_EVERY_SECONDS = 15
GIVE_UP_SETTLING_AFTER_SECONDS = 24 * 3600

# fill_status values
NONE, FILLED, NO_FILL, BLOCKED = "NONE", "FILLED", "NO_FILL", "BLOCKED"


@dataclass
class RoundState:
    rnd: Round
    mode: str
    btc_price_start: float | None = None
    up: float | None = None
    down: float | None = None
    peak_up: float | None = None
    peak_down: float | None = None
    decision: str = NO_TRADE
    fill_status: str = NONE
    reason: str = ""
    signal_at: str | None = None
    seconds_left_at_signal: float | None = None
    btc_price_signal: float | None = None
    conflict: bool = False
    live_up: float | None = None  # latest raw reading, for display only
    live_down: float | None = None
    _last_note: str = ""

    def observe(self, up: float | None, down: float | None) -> None:
        """Record a reading that passed the data checks. After a fill, up/down stay at the entry values."""
        if self.fill_status != FILLED:
            self.up, self.down = up, down
        if up is not None:
            self.peak_up = up if self.peak_up is None else max(self.peak_up, up)
        if down is not None:
            self.peak_down = down if self.peak_down is None else max(self.peak_down, down)

    def note(self, level: int, msg: str) -> None:
        """Log a status line only when it changes, so a 2s poll doesn't flood the log."""
        if msg != self._last_note:
            log.log(level, "[%s] %s", self.rnd.slug, msg)
            self._last_note = msg

    def row(self) -> dict:
        return {
            "slug": self.rnd.slug,
            "market_id": self.rnd.market_id,
            "question": self.rnd.question,
            "round_start": utc_iso(self.rnd.start),
            "round_end": utc_iso(self.rnd.end),
            "mode": self.mode,
            "up_percentage": self.up,
            "down_percentage": self.down,
            "peak_up": self.peak_up,
            "peak_down": self.peak_down,
            "btc_price_start": self.btc_price_start,
            "btc_price_signal": self.btc_price_signal,
            "decision": self.decision,
            "fill_status": self.fill_status,
            "reason": self.reason,
            "signal_at": self.signal_at,
            "seconds_left_at_signal": self.seconds_left_at_signal,
        }


class Bot:
    def __init__(
        self,
        cfg: Config,
        client: PolymarketClient,
        detector: MarketDetector,
        store: TradeStore,
        risk: RiskManager,
        browser: BrowserMirror | None = None,
        clock=time.time,
        sleep=time.sleep,
    ):
        self.cfg = cfg
        self.client = client
        self.detector = detector
        self.store = store
        self.risk = risk
        self.browser = browser
        self.clock = clock
        self.sleep = sleep
        self.mode = "PAPER" if cfg.paper_mode else "LIVE"
        self.state: RoundState | None = None
        self._last_settle = 0.0
        self._last_error = ""

    # --- lifecycle -------------------------------------------------------------
    def run(self) -> int:
        if not self.cfg.paper_mode:
            log.error(LIVE_NOT_AVAILABLE)
            return 2
        if self.risk.emergency_stop_active():
            log.error("Emergency stop file %s exists. Delete it to start the bot.", self.cfg.stop_file)
            return 1
        log.info(
            "Starting in %s mode | rule: trade only if UP or DOWN >= %.0f%% | stake ₹%s (~$%.2f) | "
            "max daily loss ₹%s",
            self.mode, THRESHOLD_PCT, f"{self.cfg.stake_inr:,.0f}", self.cfg.stake_usd,
            f"{self.cfg.max_daily_loss_inr:,.0f}",
        )
        log.info("Emergency stop: press Ctrl+C, or run `python bot.py --stop` from another terminal.")
        try:
            while True:
                if self.risk.emergency_stop_active():
                    log.warning("EMERGENCY STOP detected (%s). Stopping.", self.cfg.stop_file)
                    break
                self.tick()
                self.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            log.warning("Stopped by user (Ctrl+C).")
        finally:
            if self.state:
                self.store.upsert_round(self.state.row())
            if self.browser:
                self.browser.close()
        return 0

    def tick(self) -> None:
        now = self.clock()
        if self.state and self.state.rnd.start_ts != round_start_ts(now):
            self._close_round(self.state)
            self.state = None
        self._maybe_settle(now)

        if self.state is None:
            try:
                rnd = self.detector.current_round(now)
            except (ApiError, MarketVerificationError) as exc:
                self._warn_once(f"Cannot identify the current BTC 5m market, not trading: {exc}")
                return
            self.state = self._new_state(rnd)
            if self.browser:
                self.browser.open(rnd.url)

        self._evaluate(self.state, now)
        self._write_status(now)

    def _write_status(self, now: float) -> None:
        """Heartbeat for the dashboard. Never allowed to break the trading loop."""
        st = self.state
        status = {
            "updated_at": now,
            "mode": self.mode,
            "poll_seconds": self.cfg.poll_seconds,
            "stake_inr": self.cfg.stake_inr,
            "max_daily_loss_inr": self.cfg.max_daily_loss_inr,
            "realized_today_inr": self.risk.realized_today_inr(now),
            "round": None if st is None else {
                **st.row(),
                "url": st.rnd.url,
                "seconds_left": round(st.rnd.seconds_left(now), 1),
                "live_up": st.live_up,
                "live_down": st.live_down,
            },
            "warning": self._last_error,
        }
        try:
            tmp = self.cfg.status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(status), encoding="utf-8")
            os.replace(tmp, self.cfg.status_path)
        except OSError as exc:
            self._warn_once(f"Could not write status file: {exc}")

    # --- per round ---------------------------------------------------------------
    def _new_state(self, rnd: Round) -> RoundState:
        st = RoundState(rnd=rnd, mode=self.mode)
        existing = self.store.get_round(rnd.slug)
        if existing:  # restarted mid-round: carry on from what was recorded
            restored = {"up_percentage": "up", "down_percentage": "down"}
            for col, attr in restored.items():
                if existing.get(col) is not None:
                    setattr(st, attr, existing[col])
            for key in ("btc_price_start", "peak_up", "peak_down", "decision", "fill_status",
                        "reason", "signal_at", "seconds_left_at_signal", "btc_price_signal"):
                if existing.get(key) is not None:
                    setattr(st, key, existing[key])
            st.conflict = st.decision == CONFLICT
        if self.store.has_order(rnd.slug):
            st.fill_status = FILLED
            self.risk.mark_traded(rnd.slug)
        if st.btc_price_start is None:
            st.btc_price_start = self.client.get_btc_spot()
        self.store.upsert_round(st.row())
        log.info("New round %s (%s), ends %s UTC", rnd.slug, rnd.question, rnd.end.strftime("%H:%M:%S"))
        return st

    def _close_round(self, st: RoundState) -> None:
        self.store.upsert_round(st.row())
        log.info(
            "Round over %s | last UP %s%% DOWN %s%% | peak UP %s%% DOWN %s%% | decision %s | %s%s",
            st.rnd.slug, st.up, st.down, st.peak_up, st.peak_down, st.decision, st.fill_status,
            f" ({st.reason})" if st.reason else "",
        )

    def _read_books(self, rnd: Round, now: float) -> tuple[Book, Book] | None:
        try:
            up_book = parse_book(self.client.get_book(rnd.up_token))
            down_book = parse_book(self.client.get_book(rnd.down_token))
        except (ApiError, KeyError, TypeError, ValueError) as exc:
            self._warn_once(f"Order book unavailable, not trading: {exc}")
            return None
        for name, book in (("UP", up_book), ("DOWN", down_book)):
            if book.timestamp_ms is None:
                continue
            age = now - book.timestamp_ms / 1000
            if age > self.cfg.max_book_age_seconds:
                self._warn_once(f"{name} order book is {age:.0f}s old (stale), not trading")
                return None
        self._last_error = ""
        return up_book, down_book

    def _evaluate(self, st: RoundState, now: float) -> None:
        rnd = st.rnd
        secs_left = rnd.seconds_left(now)
        if secs_left <= 0:
            return
        books = self._read_books(rnd, now)
        if books is None:
            return
        up_book, down_book = books
        up = to_pct(displayed_probability(up_book))
        down = to_pct(displayed_probability(down_book))
        st.live_up, st.live_down = up, down

        if up is None or down is None:
            decision = INVALID
        elif abs(up + down - 100) > self.cfg.max_prob_sum_deviation:
            decision = INVALID
        else:
            decision = get_decision(up, down)

        if decision == INVALID:
            st.note(logging.WARNING, f"Prices look inconsistent (UP {up}%, DOWN {down}%), not trading")
            return
        st.observe(up, down)
        if decision == CONFLICT or st.conflict:
            if not st.conflict:
                st.conflict = True
                st.decision = CONFLICT
                st.reason = f"both sides >= {THRESHOLD_PCT:.0f}% (UP {up}%, DOWN {down}%)"
                self.store.upsert_round(st.row())
            st.note(logging.ERROR, f"CONFLICT: {st.reason}. No trade this round.")
            return
        if decision not in TRADE_DECISIONS:
            return
        if st.fill_status == FILLED:
            return

        if st.decision != decision or st.signal_at is None:
            st.decision = decision
            st.signal_at = utc_iso(datetime.fromtimestamp(now, tz=timezone.utc))
            st.seconds_left_at_signal = round(secs_left, 1)
            st.btc_price_signal = self.client.get_btc_spot()
            log.info("[%s] SIGNAL %s | UP %s%% DOWN %s%% | %.0fs left", rnd.slug, decision, up, down, secs_left)
            self.store.upsert_round(st.row())

        blocked = self._pre_trade_block(st, now, secs_left, up, down)
        if blocked:
            st.fill_status, st.reason = BLOCKED, blocked
            st.note(logging.WARNING, f"{decision} signal but not trading: {blocked}")
            return

        book = with_complement_asks(up_book, down_book) if decision == "UP" else with_complement_asks(down_book, up_book)
        fill = simulate_buy(book, self.cfg.stake_usd, self.cfg.max_entry_price, self.cfg.taker_fee_rate)
        if not fill.filled:
            st.fill_status, st.reason = NO_FILL, fill.reason
            st.note(logging.INFO, f"{decision} signal, no paper fill: {fill.reason} (will keep trying)")
            return

        st.fill_status, st.reason = FILLED, ""
        fields = {
            **st.row(),
            "stake_inr": self.cfg.stake_inr,
            "stake_usd": round(fill.usd_spent, 4),
            "entry_price": round(fill.avg_price, 4),
            "shares": round(fill.shares, 4),
            "fee_usd": round(fill.fee_usd, 4),
        }
        if not self.store.record_order(rnd.slug, decision, self.cfg.stake_inr, self.mode, fields):
            log.warning("[%s] Duplicate order prevented: this round already has a trade.", rnd.slug)
            self.risk.mark_traded(rnd.slug)
            return
        self.risk.mark_traded(rnd.slug)
        log.info(
            "[%s] TRADE %s | UP %s%% DOWN %s%% | stake ₹%s ($%.2f) | %.4f shares @ avg %.4f | fee $%.4f | mode %s",
            rnd.slug, decision, up, down, f"{self.cfg.stake_inr:,.0f}", fill.usd_spent,
            fill.shares, fill.avg_price, fill.fee_usd, self.mode,
        )

    def _pre_trade_block(self, st: RoundState, now: float, secs_left: float, up: float, down: float) -> str:
        if secs_left <= self.cfg.no_trade_last_seconds:
            return f"inside the final {self.cfg.no_trade_last_seconds:.0f}s of the round"
        if not st.rnd.accepting_orders:
            return "market is not accepting orders"
        if self.cfg.browser_crosscheck:
            if not self.browser:
                return "browser cross-check enabled but browser is off"
            seen = self.browser.read_displayed()
            if seen is None:
                return "could not read UP/DOWN from the web page"
            tol = self.cfg.browser_tolerance_pct
            if abs(seen[0] - up) > tol or abs(seen[1] - down) > tol:
                return f"web page shows UP {seen[0]}% DOWN {seen[1]}%, API shows UP {up}% DOWN {down}%"
        risk = self.risk.check_trade(st.rnd.slug, self.cfg.stake_inr, now)
        return "" if risk.allowed else risk.reason

    # --- settlement ------------------------------------------------------------------
    def _maybe_settle(self, now: float) -> None:
        if now - self._last_settle < SETTLE_EVERY_SECONDS:
            return
        self._last_settle = now
        current = self.state.rnd.slug if self.state else None
        for row in self.store.unsettled_rounds(utc_iso(datetime.fromtimestamp(now, tz=timezone.utc))):
            if row["slug"] == current:
                continue
            try:
                result = self.detector.resolution(row["slug"])
            except ApiError as exc:
                self._warn_once(f"Could not check result for {row['slug']}: {exc}")
                return
            if result is None:
                ended = datetime.fromisoformat(row["round_end"]).timestamp()
                if now - ended > GIVE_UP_SETTLING_AFTER_SECONDS:
                    log.warning("[%s] no resolution after 24h, marking UNRESOLVED", row["slug"])
                    self.store.settle(row["slug"], "UNRESOLVED", None, None)
                continue
            self._settle_row(row, result)

    def _settle_row(self, row: dict, result: str) -> None:
        pnl_usd = pnl_inr = None
        if row.get("fill_status") == FILLED and row.get("decision") in TRADE_DECISIONS:
            won = row["decision"] == result
            payout = row["shares"] if won else 0.0
            pnl_usd = round(payout - row["stake_usd"] - (row.get("fee_usd") or 0.0), 4)
            pnl_inr = round(pnl_usd * self.cfg.inr_per_usd, 2)
        self.store.settle(row["slug"], result, pnl_usd, pnl_inr)
        if pnl_inr is None:
            return
        log.info(
            "[%s] RESULT %s | traded %s -> %s | P/L ₹%s ($%.2f) | today ₹%s",
            row["slug"], result, row["decision"], "WIN" if row["decision"] == result else "LOSS",
            f"{pnl_inr:,.2f}", pnl_usd, f"{self.risk.realized_today_inr(self.clock()):,.2f}",
        )
        if self.risk.daily_limit_reached(self.clock()):
            log.error("DAILY LOSS LIMIT REACHED. No more trades today (monitoring continues).")

    def _warn_once(self, msg: str) -> None:
        if msg != self._last_error:
            log.warning(msg)
            self._last_error = msg


# --- CLI ---------------------------------------------------------------------------------
def check_once(cfg: Config) -> int:
    client = PolymarketClient()
    now = time.time()
    try:
        rnd = MarketDetector(client).current_round(now)
    except (ApiError, MarketVerificationError) as exc:
        print(f"Could not identify the current round: {exc}")
        return 1
    up_book = parse_book(client.get_book(rnd.up_token))
    down_book = parse_book(client.get_book(rnd.down_token))
    up = to_pct(displayed_probability(up_book))
    down = to_pct(displayed_probability(down_book))
    decision = get_decision(up, down)
    print(f"Market   : {rnd.question}")
    print(f"URL      : {rnd.url}")
    print(f"Time left: {rnd.seconds_left(now):.0f}s")
    print(f"UP       : {up}%   (bid {up_book.best_bid}, ask {up_book.best_ask})")
    print(f"DOWN     : {down}%   (bid {down_book.best_bid}, ask {down_book.best_ask})")
    print(f"Decision : {decision}")
    if decision in TRADE_DECISIONS:
        book = with_complement_asks(up_book, down_book) if decision == "UP" else with_complement_asks(down_book, up_book)
        fill = simulate_buy(book, cfg.stake_usd, cfg.max_entry_price, cfg.taker_fee_rate)
        print(f"Paper fill for ₹{cfg.stake_inr:,.0f}: {fill}")
    return 0


def browser_check(cfg: Config) -> int:
    rnd = MarketDetector(PolymarketClient()).current_round(time.time())
    mirror = BrowserMirror(headless=cfg.browser_headless, executable_path=cfg.browser_executable)
    mirror.start()
    try:
        mirror.open(rnd.url)
        time.sleep(5)
        print(f"Opened {rnd.url}")
        print(f"Values read from page (UP%, DOWN%): {mirror.read_displayed()}")
    finally:
        mirror.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m 98%-rule paper trading bot")
    parser.add_argument("--stop", action="store_true", help="emergency stop a running bot")
    parser.add_argument("--check", action="store_true", help="show the live round and decision, then exit")
    parser.add_argument("--browser-check", action="store_true", help="test reading values from the web page")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except (ConfigError, ValueError) as exc:
        print(f"Configuration error: {exc}")
        return 2

    if args.stop:
        cfg.stop_file.write_text(f"{utc_iso()} manual emergency stop\n", encoding="utf-8")
        print(f"Emergency stop requested ({cfg.stop_file}). Delete this file before starting again.")
        return 0
    if args.check:
        return check_once(cfg)
    if args.browser_check:
        return browser_check(cfg)

    setup_logging(cfg.log_path)
    client = PolymarketClient()
    store = TradeStore(cfg.db_path, cfg.csv_path)
    browser = None
    if cfg.browser_enabled:
        browser = BrowserMirror(headless=cfg.browser_headless, executable_path=cfg.browser_executable)
        try:
            browser.start()
        except Exception as exc:  # noqa: BLE001
            log.error("Browser failed to start: %s", exc)
            if cfg.browser_crosscheck:
                return 1
            browser = None
    bot = Bot(cfg, client, MarketDetector(client), store, RiskManager(cfg, store), browser)
    try:
        return bot.run()
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
