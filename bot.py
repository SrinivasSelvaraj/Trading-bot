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
from paper_account import BOT, MANUAL, SIDES, SOLD, UNRESOLVED, Books, PaperAccount, signed_inr, validate_exits
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
EXIT_RESTART = 3  # Bot.run() return code: rebuild the bot and carry on (dashboard "Restart bot")
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
    quote: dict | None = None        # best prices per side from the latest books, for the dashboard
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
        self.account = PaperAccount(cfg, store)
        self.state: RoundState | None = None
        self._last_settle = 0.0
        self._last_error = ""
        self._restart = False

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
                if self._restart:
                    log.warning("Restarting the bot (requested from the dashboard).")
                    return EXIT_RESTART
                self.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            log.warning("Stopped by user (Ctrl+C).")
        finally:
            if self.state:
                self.store.upsert_round(self.state.row())
            if self.browser and not self._restart:
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
                self._process_commands(None, None, now)
                self._write_status(now)
                return
            self.state = self._new_state(rnd)
            if self.browser:
                self.browser.open(rnd.url)

        st = self.state
        # raw: this poll's order books, fresh but not necessarily consistent with each other
        raw = self._read_books(st.rnd, now) if st.rnd.seconds_left(now) > 0 else None
        st.quote = raw.quote() if raw else None
        # valuations and take profit / stop loss only act on prices that pass the consistency check
        if raw is not None and self._consistent(raw):
            for event in self.account.update_marks_and_exits(st.rnd.slug, raw, self._iso(now)):
                log.info("[%s] %s", st.rnd.slug, event)
        # manual orders fill against the real book, so fresh books are enough for them
        self._process_commands(st, raw, now)
        if self.state is st and raw is not None:  # a reset or restart drops the round state
            self._evaluate(st, now, raw)
        self._write_status(now)

    @staticmethod
    def _iso(ts: float) -> str:
        return utc_iso(datetime.fromtimestamp(ts, tz=timezone.utc))

    def _consistent(self, books: Books) -> bool:
        up = to_pct(books.price("UP"))
        down = to_pct(books.price("DOWN"))
        return up is not None and down is not None and abs(up + down - 100) <= self.cfg.max_prob_sum_deviation

    def bot_settings(self) -> dict:
        def price(key: str) -> float | None:
            raw = self.store.get_setting(key)
            return float(raw) if raw not in (None, "") else None
        return {
            "enabled": self.store.get_setting("bot_enabled", "1") == "1",
            "take_profit": price("bot_take_profit"),
            "stop_loss": price("bot_stop_loss"),
        }

    def _write_status(self, now: float) -> None:
        """Heartbeat for the dashboard. Never allowed to break the trading loop."""
        st = self.state
        status = {
            "updated_at": now,
            "mode": self.mode,
            "poll_seconds": self.cfg.poll_seconds,
            "stake_inr": self.cfg.stake_inr,
            "max_daily_loss_inr": self.cfg.max_daily_loss_inr,
            "realized_today_inr": self.risk.realized_today_inr(now, BOT),
            "manual_realized_today_inr": self.risk.realized_today_inr(now, MANUAL),
            "max_manual_daily_loss_inr": self.cfg.max_manual_daily_loss_inr,
            "round": None if st is None else {
                **st.row(),
                "url": st.rnd.url,
                "seconds_left": round(st.rnd.seconds_left(now), 1),
                "live_up": st.live_up,
                "live_down": st.live_down,
                "quote": st.quote,
                "accepting_orders": st.rnd.accepting_orders,
            },
            "bot_settings": self.bot_settings(),
            "no_trade_last_seconds": self.cfg.no_trade_last_seconds,
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

    def _read_books(self, rnd: Round, now: float) -> Books | None:
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
        return Books(up_book, down_book)

    def _evaluate(self, st: RoundState, now: float, books: Books) -> None:
        """Apply the 98% rule to this poll's books and, if every check passes, make the bot's trade."""
        rnd = st.rnd
        secs_left = rnd.seconds_left(now)
        if secs_left <= 0:
            return
        up = to_pct(books.price("UP"))
        down = to_pct(books.price("DOWN"))
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

        fill = self.account.quote_buy(books, decision, self.cfg.stake_inr)
        if fill.filled and self.cfg.stake_inr + fill.fee_usd * self.cfg.inr_per_usd > self.risk.cash_inr():
            st.fill_status, st.reason = BLOCKED, "not enough paper balance for the stake plus fee"
            st.note(logging.WARNING, f"{decision} signal but not trading: {st.reason}")
            return
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
        exits = self.bot_settings()
        self.account.record_open(
            source=BOT, rnd=rnd, side=decision, fill=fill, amount_inr=self.cfg.stake_inr,
            take_profit=exits["take_profit"], stop_loss=exits["stop_loss"], mode=self.mode,
            now_iso=self._iso(now), books=books,
        )
        log.info(
            "[%s] TRADE %s | UP %s%% DOWN %s%% | stake ₹%s ($%.2f) | %.4f shares @ avg %.4f | fee $%.4f | mode %s",
            rnd.slug, decision, up, down, f"{self.cfg.stake_inr:,.0f}", fill.usd_spent,
            fill.shares, fill.avg_price, fill.fee_usd, self.mode,
        )

    def _pre_trade_block(self, st: RoundState, now: float, secs_left: float, up: float, down: float) -> str:
        if not self.bot_settings()["enabled"]:
            return "auto-trader is paused from the dashboard"
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
                    log.warning("[%s] no resolution after 24h, marking UNRESOLVED (stakes returned)", row["slug"])
                    self.account.settle(row["slug"], UNRESOLVED, self._iso(now))
                    self.store.settle(row["slug"], UNRESOLVED, None, None)
                continue
            self._settle_row(row, result)
        self._settle_stragglers(now, current)

    def _settle_stragglers(self, now: float, current: str | None) -> None:
        """Open positions whose round has ended but was settled without them (e.g. by an older
        bot version sharing the database). Pay them out from the round's recorded result."""
        for slug in self.store.ended_open_position_slugs(self._iso(now)):
            if slug == current:
                continue
            row = self.store.get_round(slug) or {}
            result = row.get("result")
            if result is None:
                try:
                    result = self.detector.resolution(slug)
                except ApiError:
                    return
            if result is None:
                continue
            for pos in self.account.settle(slug, result, self._iso(now)):
                log.info("[%s] RESULT %s | %s #%s %s | P/L ₹%s (late settlement)",
                         slug, result, pos["source"], pos["id"], pos["side"], pos.get("pnl_inr"))

    def _settle_row(self, row: dict, result: str) -> None:
        now = self.clock()
        for pos in self.account.settle(row["slug"], result, self._iso(now)):
            log.info(
                "[%s] RESULT %s | %s #%s %s -> %s | P/L %s | %s today %s",
                row["slug"], result, pos["source"], pos["id"], pos["side"],
                "WIN" if pos["side"] == result else "LOSS", signed_inr(pos["pnl_inr"]),
                pos["source"], signed_inr(self.risk.realized_today_inr(now, pos["source"])),
            )
        # the round's P/L is the bot's own position, however it ended (resolution, stop loss, ...)
        bot_pos = self.store.bot_position(row["slug"])
        pnl_usd = bot_pos["pnl_usd"] if bot_pos else None
        pnl_inr = bot_pos["pnl_inr"] if bot_pos else None
        self.store.settle(row["slug"], result, pnl_usd, pnl_inr)
        if bot_pos and self.risk.daily_limit_reached(now):
            log.error("DAILY LOSS LIMIT REACHED. No more trades today (monitoring continues).")

    # --- dashboard commands ------------------------------------------------------------
    def _process_commands(self, st: RoundState | None, books: Books | None, now: float) -> None:
        for cmd in self.store.pending_commands():
            if not self.store.claim_command(cmd["id"]):
                continue  # already taken: never run an action twice
            if now - cmd["created_at"] > self.cfg.command_max_age_seconds:
                ok, msg = False, "Expired before the bot could run it; nothing was done"
            else:
                try:
                    ok, msg = self._run_command(cmd, st, books, now)
                except (KeyError, TypeError, ValueError) as exc:
                    ok, msg = False, f"Bad request ({exc})"
            self.store.finish_command(cmd["id"], ok, msg, now)
            log.info("Dashboard %s #%s %s: %s", cmd["kind"], cmd["id"], "done" if ok else "refused", msg)
            if self.state is not st or self._restart:
                break  # reset or restart: later commands run on the next poll, against fresh state

    def _run_command(self, cmd: dict, st: RoundState | None, books: Books | None,
                     now: float) -> tuple[bool, str]:
        kind, p = cmd["kind"], cmd["payload"]
        cents = lambda v: None if v in (None, "") else float(v) / 100  # noqa: E731 - UI sends prices in ¢
        live = st is not None and books is not None and st.rnd.seconds_left(now) > 0

        if kind == "BUY":
            side, amount = p["side"], float(p["amount_inr"])
            tp, sl = cents(p.get("take_profit")), cents(p.get("stop_loss"))
            if side not in SIDES:
                return False, "Side must be UP or DOWN"
            problem = validate_exits(tp, sl)
            if problem:
                return False, problem
            if not live:
                return False, "No live order book right now; try again in a few seconds"
            if st.rnd.seconds_left(now) <= self.cfg.no_trade_last_seconds:
                return False, "The round is about to close; wait for the next one"
            if not st.rnd.accepting_orders:
                return False, "This market is not accepting orders"
            problem = self._exits_vs_market(tp, sl, side, books)
            if problem:
                return False, problem
            risk = self.risk.check_manual(amount, now)
            if not risk.allowed:
                return False, risk.reason[:1].upper() + risk.reason[1:]
            fill = self.account.quote_buy(books, side, amount)
            if not fill.filled:
                return False, f"Not filled: {fill.reason}"
            fee_inr = fill.fee_usd * self.cfg.inr_per_usd
            cash = self.risk.cash_inr()
            if amount + fee_inr > cash:
                return False, (f"Not enough paper balance for ₹{amount:,.0f} plus the ₹{fee_inr:,.2f} fee "
                               f"(₹{cash:,.2f} free)")
            pos_id = self.account.record_open(
                source=MANUAL, rnd=st.rnd, side=side, fill=fill, amount_inr=amount,
                take_profit=tp, stop_loss=sl, mode=self.mode, now_iso=self._iso(now), books=books,
            )
            return True, f"Bought {fill.shares:.2f} {side} @ {fill.avg_price * 100:.1f}¢ for ₹{amount:,.0f} (#{pos_id})"

        if kind == "SELL":
            pos = self.store.get_position(int(p["position_id"]))
            if pos is None or pos["status"] != "OPEN":
                return False, "That position is no longer open"
            if not live or pos["slug"] != st.rnd.slug:
                return False, "Its round has closed; it will settle when Polymarket resolves the market"
            ok, msg = self.account.close(pos, books, SOLD, self._iso(now))
            return ok, (msg[:1].upper() + msg[1:]) if ok else f"Could not sell: {msg}"

        if kind == "SET_EXITS":
            pos = self.store.get_position(int(p["position_id"]))
            tp, sl = cents(p.get("take_profit")), cents(p.get("stop_loss"))
            if pos is not None and live and pos["slug"] == st.rnd.slug:
                problem = validate_exits(tp, sl) or self._exits_vs_market(tp, sl, pos["side"], books)
                if problem:
                    return False, problem
            return self.account.set_exits(int(p["position_id"]), tp, sl)

        if kind == "BOT_SETTINGS":
            # a field left out (None) keeps its saved value, so Pause/Resume never trips over old exits
            current = self.bot_settings()
            tp = current["take_profit"] if p.get("take_profit") is None else cents(p["take_profit"])
            sl = current["stop_loss"] if p.get("stop_loss") is None else cents(p["stop_loss"])
            changing = p.get("take_profit") is not None or p.get("stop_loss") is not None
            problem = validate_exits(tp, sl) if changing else ""
            if not problem and changing and tp is not None and tp <= THRESHOLD_PCT / 100:
                problem = (f"The auto-trader buys at {THRESHOLD_PCT:.0f}–99¢, so a take profit at or below "
                           f"{THRESHOLD_PCT:.0f}¢ would sell at a loss straight away. Use 99¢ or leave it off")
            if not problem and changing and sl is not None and sl >= THRESHOLD_PCT / 100:
                problem = f"The auto-trader buys at {THRESHOLD_PCT:.0f}–99¢, so its stop loss must be below {THRESHOLD_PCT:.0f}¢"
            if problem:
                return False, problem
            enabled = current["enabled"] if p.get("enabled") is None else bool(p["enabled"])
            self.store.set_setting("bot_enabled", "1" if enabled else "0")
            self.store.set_setting("bot_take_profit", "" if tp is None else str(tp))
            self.store.set_setting("bot_stop_loss", "" if sl is None else str(sl))
            fmt = lambda v: "off" if v is None else f"{v * 100:g}¢"  # noqa: E731
            return True, f"Auto-trader {'on' if enabled else 'paused'} · take profit {fmt(tp)} · stop loss {fmt(sl)}"

        if kind == "RESET_ACCOUNT":
            if p.get("confirm") != "RESET":
                return False, "Type RESET to confirm"
            archived = self.store.reset_all(keep_command_id=cmd["id"])
            self.risk.forget_traded()
            self.state = None  # the current round is re-read, and re-recorded, on the next poll
            note = f"; old trades.csv kept as {archived}" if archived else ""
            log.warning("Paper account reset from the dashboard%s", note)
            return True, f"Paper account reset to ₹{self.cfg.starting_balance_inr:,.0f}{note}"

        if kind == "RESTART_BOT":
            self._restart = True
            return True, "Bot restarting; it will be back within a few seconds"

        return False, f"Unknown action {kind!r}"

    @staticmethod
    def _exits_vs_market(tp: float | None, sl: float | None, side: str, books: Books) -> str:
        """Refuse exits that would fire the moment they're set (almost always a typo)."""
        bid, price = books.sell_book(side).best_bid, books.price(side)
        if tp is not None and bid is not None and tp <= bid:
            return (f"Take profit {tp * 100:g}¢ is at or below what buyers pay now ({bid * 100:g}¢), so it would "
                    f"sell straight away. Set it above {bid * 100:g}¢")
        if sl is not None and price is not None and sl >= price:
            return (f"Stop loss {sl * 100:g}¢ is at or above the current price ({price * 100:.1f}¢), so it would "
                    f"sell straight away. Set it below {price * 100:.1f}¢")
        return ""

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
    try:
        while True:
            code = Bot(cfg, client, MarketDetector(client), store, RiskManager(cfg, store), browser).run()
            if code != EXIT_RESTART:
                return code
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
