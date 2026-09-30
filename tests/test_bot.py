"""Offline tests: no network, no browser. Run with `python -m pytest`."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import BLOCKED, FILLED, NO_FILL, Bot  # noqa: E402
from browser import parse_displayed  # noqa: E402
from config import Config, ConfigError  # noqa: E402
from logger import TradeStore  # noqa: E402
from market_detector import (  # noqa: E402
    MarketDetector, MarketVerificationError, parse_and_verify, parse_resolution, round_start_ts, slug_for,
)
from quotes import (  # noqa: E402
    displayed_probability, parse_book, polymarket_taker_fee, simulate_buy, with_complement_asks,
)
from risk_manager import RiskManager  # noqa: E402
from strategy import CONFLICT, DOWN, INVALID, NO_TRADE, UP, get_decision  # noqa: E402

START = 1790793900  # 2026-09-30 18:45:00 UTC, a real round boundary


# --- strategy ------------------------------------------------------------------
@pytest.mark.parametrize("up,down,expected", [
    (98, 2, UP), (99, 1, UP), (2, 98, DOWN), (1, 99, DOWN),
    (70, 30, NO_TRADE), (60, 40, NO_TRADE), (15, 85, NO_TRADE), (50, 50, NO_TRADE),
    (97.99, 2.01, NO_TRADE), (85, 15, NO_TRADE), (100, 0, UP),
    (98, 98, CONFLICT), (99, 99, CONFLICT),
    (None, 2, INVALID), (98, None, INVALID), (float("nan"), 2, INVALID), (101, 0, INVALID), (-1, 99, INVALID),
    (True, 99, INVALID),
])
def test_decision_table(up, down, expected):
    assert get_decision(up, down) == expected


# --- market detection --------------------------------------------------------------
def gamma_market(start=START, **over):
    m = {
        "id": "5136733",
        "slug": slug_for(start),
        "question": "Bitcoin Up or Down - September 30, 2:45PM-2:50PM ET",
        "conditionId": "0xabc",
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["0.5", "0.5"]',
        "clobTokenIds": '["111", "222"]',
        "eventStartTime": "2026-09-30T18:45:00Z",
        "endDate": "2026-09-30T18:50:00Z",
        "acceptingOrders": True,
        "closed": False,
    }
    m.update(over)
    return m


def test_round_math():
    assert round_start_ts(START + 299) == START
    assert round_start_ts(START + 300) == START + 300
    assert slug_for(START) == "btc-updown-5m-1790793900"


def test_verify_accepts_real_shape():
    rnd = parse_and_verify(gamma_market(), START)
    assert (rnd.up_token, rnd.down_token) == ("111", "222")
    assert rnd.url.endswith("btc-updown-5m-1790793900")


@pytest.mark.parametrize("over", [
    {"slug": "btc-updown-5m-1790793600"},
    {"question": "Ethereum Up or Down - September 30"},
    {"outcomes": '["Down", "Up"]'},
    {"outcomes": '["Yes", "No"]'},
    {"clobTokenIds": '["111"]'},
    {"clobTokenIds": '["111", "111"]'},
    {"eventStartTime": "2026-09-30T18:40:00Z"},
    {"endDate": "2026-09-30T19:00:00Z"},
    {"endDate": None},
])
def test_verify_rejects_wrong_market(over):
    with pytest.raises(MarketVerificationError):
        parse_and_verify(gamma_market(**over), START)


def test_resolution():
    assert parse_resolution(gamma_market(closed=True, outcomePrices='["1", "0"]')) == "UP"
    assert parse_resolution(gamma_market(closed=True, outcomePrices='["0", "1"]')) == "DOWN"
    assert parse_resolution(gamma_market(closed=False, outcomePrices='["1", "0"]')) is None
    assert parse_resolution(gamma_market(closed=True, outcomePrices='["0.5", "0.5"]')) is None


# --- quotes / fills --------------------------------------------------------------------
def raw_book(bids=(), asks=(), last="0.5", ts=None):
    return {
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "last_trade_price": last,
        "timestamp": str(ts) if ts is not None else None,
        "min_order_size": "5",
    }


def test_displayed_uses_midpoint_or_last_trade():
    assert displayed_probability(parse_book(raw_book([(0.97, 10)], [(0.99, 10)]))) == pytest.approx(0.98)
    # spread wider than 10c -> last trade
    assert displayed_probability(parse_book(raw_book([(0.5, 10)], [(0.99, 10)], last="0.9"))) == 0.9
    # one-sided book -> last trade
    assert displayed_probability(parse_book(raw_book([(0.99, 10)], [], last="0.99"))) == 0.99


def test_book_sorting_best_first():
    b = parse_book(raw_book([(0.97, 1), (0.99, 1), (0.98, 1)], [(0.99, 1), (0.97, 1), (0.98, 1)]))
    assert b.best_bid == 0.99 and b.best_ask == 0.97


def test_simulate_buy_walks_asks_and_charges_fee():
    book = parse_book(raw_book(asks=[(0.98, 5), (0.99, 100)]))
    fill = simulate_buy(book, usd=10.0, max_price=0.99, fee_rate=0.07)
    assert fill.filled
    shares = 5 + (10 - 4.9) / 0.99
    assert fill.shares == pytest.approx(shares)
    assert fill.fee_usd == pytest.approx(
        polymarket_taker_fee(5, 0.98, 0.07) + polymarket_taker_fee((10 - 4.9) / 0.99, 0.99, 0.07)
    )


def test_simulate_buy_is_fill_or_kill_and_respects_max_price():
    assert not simulate_buy(parse_book(raw_book(asks=[(0.99, 1)])), 10, 0.99, 0.07).filled
    assert not simulate_buy(parse_book(raw_book(asks=[(0.995, 1000)])), 10, 0.99, 0.07).filled
    assert "no sellers" in simulate_buy(parse_book(raw_book()), 10, 0.99, 0.07).reason


def test_complement_asks():
    down = parse_book(raw_book(bids=[(0.99, 50)], asks=[]))
    up = parse_book(raw_book(bids=[(0.02, 30)], asks=[(0.01, 40)]))
    merged = with_complement_asks(down, up)
    assert merged.asks == [(0.98, 30)]


def test_browser_text_parser():
    assert parse_displayed("Buy Sell Up 99¢ Down 1.5¢ Amount") == (99.0, 1.5)
    assert parse_displayed("Up\n98¢\nDown\n3¢") == (98.0, 3.0)
    assert parse_displayed("nothing here") is None


# --- config ------------------------------------------------------------------------------
def test_config_rejects_stake_above_max():
    with pytest.raises(ConfigError):
        Config(stake_inr=1200, max_stake_inr=1100).validate()


# --- bot end-to-end with a fake exchange -------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.markets = {}
        self.books = {}
        self.spot = 83_000.0

    def get_market_by_slug(self, slug):
        return self.markets.get(slug)

    def get_book(self, token_id):
        return self.books[token_id]

    def get_btc_spot(self):
        return self.spot


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    cfg = Config(
        db_path=tmp_path / "t.db", csv_path=tmp_path / "t.csv", log_path=tmp_path / "b.log",
        stop_file=tmp_path / "STOP", inr_per_usd=100.0, taker_fee_rate=0.0,
    )
    client = FakeClient()
    client.markets[slug_for(START)] = gamma_market()
    store = TradeStore(cfg.db_path, cfg.csv_path)
    clock = Clock(START + 100)
    bot = Bot(cfg, client, MarketDetector(client), store, RiskManager(cfg, store), clock=clock, sleep=lambda s: None)
    yield cfg, client, store, clock, bot
    store.close()


def set_books(client, up_bid, up_ask, down_bid, down_ask, size=1000):
    def side(p):
        return [(p, size)] if p is not None else []
    client.books["111"] = raw_book(side(up_bid), side(up_ask), last=str(up_bid or up_ask))
    client.books["222"] = raw_book(side(down_bid), side(down_ask), last=str(down_bid or down_ask))


def settle_market(client, prices):
    client.markets[slug_for(START)] = gamma_market(closed=True, acceptingOrders=False, outcomePrices=prices)


def test_no_trade_at_85_percent(env):
    cfg, client, store, clock, bot = env
    set_books(client, 0.84, 0.86, 0.14, 0.16)
    bot.tick()
    assert not store.has_order(slug_for(START))
    assert bot.state.decision == NO_TRADE


def test_trades_once_at_98_and_settles_win(env):
    cfg, client, store, clock, bot = env
    set_books(client, 0.97, 0.99, 0.01, 0.03)  # UP displayed 98%
    bot.tick()
    assert store.has_order(slug_for(START))
    assert bot.state.fill_status == FILLED
    row = store.get_round(slug_for(START))
    assert row["decision"] == "UP" and row["entry_price"] == pytest.approx(0.99)
    assert row["stake_usd"] == pytest.approx(11.0)

    for _ in range(5):  # keeps polling the same round: no second order
        clock.t += 2
        bot.tick()
    assert store.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1

    settle_market(client, '["1", "0"]')
    clock.t = START + 400  # next round (market missing -> no trade, but settlement runs)
    bot.tick()
    row = store.get_round(slug_for(START))
    assert row["result"] == "UP"
    assert row["profit_loss_usd"] == pytest.approx(11.0 / 0.99 - 11.0, abs=1e-3)
    assert cfg.csv_path.read_text().count("\n") == 2  # header + one row


def test_inconsistent_readings_after_fill_do_not_overwrite_entry_values(env):
    cfg, client, store, clock, bot = env
    set_books(client, 0.98, 0.99, 0.01, 0.02)  # UP 98.5 / DOWN 1.5 -> trade
    bot.tick()
    set_books(client, 0.99, None, 0.99, None)  # both sides read 99% (seen live): rejected
    clock.t += 5
    bot.tick()
    set_books(client, 0.99, 1.0, 0.0, 0.01)  # valid, higher reading after the fill
    clock.t += 5
    bot.tick()
    bot._close_round(bot.state)
    row = store.get_round(slug_for(START))
    assert (row["up_percentage"], row["down_percentage"]) == (98.5, 1.5)
    assert row["peak_down"] == 1.5
    assert store.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_loss_is_full_stake(env):
    cfg, client, store, clock, bot = env
    set_books(client, 0.01, 0.03, 0.97, 0.99)  # DOWN 98%
    bot.tick()
    settle_market(client, '["1", "0"]')
    clock.t = START + 400
    bot.tick()
    assert store.get_round(slug_for(START))["profit_loss_inr"] == pytest.approx(-1100.0)


def test_conflict_never_trades(env):
    cfg, client, store, clock, bot = env
    set_books(client, 0.98, 0.99, 0.98, 0.99)
    bot = Bot(replace(cfg, max_prob_sum_deviation=100), client, bot.detector, store, bot.risk,
              clock=clock, sleep=lambda s: None)
    bot.tick()
    assert bot.state.decision == CONFLICT
    assert not store.has_order(slug_for(START))


def test_no_fill_when_nobody_sells(env):
    cfg, client, store, clock, bot = env
    set_books(client, None, 0.01, 0.99, None)  # DOWN shows 99% but there are no sellers of DOWN
    bot.tick()
    assert bot.state.decision == "DOWN"
    assert bot.state.fill_status == NO_FILL
    assert not store.has_order(slug_for(START))


def test_no_trade_in_final_seconds(env):
    cfg, client, store, clock, bot = env
    clock.t = START + 298
    set_books(client, 0.97, 0.99, 0.01, 0.03)
    bot.tick()
    assert bot.state.fill_status == BLOCKED
    assert not store.has_order(slug_for(START))


def test_emergency_stop_blocks_trade(env):
    cfg, client, store, clock, bot = env
    cfg.stop_file.write_text("stop")
    set_books(client, 0.97, 0.99, 0.01, 0.03)
    bot.tick()
    assert not store.has_order(slug_for(START))
    assert bot.run() == 1  # refuses to start while STOP exists


def test_unverifiable_market_never_trades(env):
    cfg, client, store, clock, bot = env
    client.markets[slug_for(START)] = gamma_market(outcomes='["Yes", "No"]')
    set_books(client, 0.97, 0.99, 0.01, 0.03)
    bot.tick()
    assert bot.state is None
    assert not store.has_order(slug_for(START))


def test_stale_book_never_trades(env):
    cfg, client, store, clock, bot = env
    set_books(client, 0.97, 0.99, 0.01, 0.03)
    client.books["111"]["timestamp"] = str((START + 100 - 60) * 1000)
    bot.tick()
    assert not store.has_order(slug_for(START))


def test_restart_mid_round_does_not_duplicate(env):
    cfg, client, store, clock, bot = env
    set_books(client, 0.97, 0.99, 0.01, 0.03)
    bot.tick()
    bot2 = Bot(cfg, client, bot.detector, store, RiskManager(cfg, store), clock=clock, sleep=lambda s: None)
    bot2.tick()
    assert bot2.state.fill_status == FILLED
    assert store.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_live_mode_refused(env):
    cfg, client, store, clock, bot = env
    live = Bot(replace(cfg, paper_mode=False), client, bot.detector, store, bot.risk, clock=clock)
    assert live.run() == 2


# --- risk ------------------------------------------------------------------------------------
def test_daily_loss_limit_and_worst_case(tmp_path):
    cfg = Config(db_path=tmp_path / "t.db", csv_path=tmp_path / "t.csv", stop_file=tmp_path / "STOP",
                 max_daily_loss_inr=5000, timezone="UTC")
    store = TradeStore(cfg.db_path, cfg.csv_path)
    risk = RiskManager(cfg, store)
    now = START + 100
    assert risk.check_trade("a", 1100, now).allowed
    assert not risk.check_trade("a", 1101, now).allowed
    assert not risk.check_trade("a", 0, now).allowed
    for i in range(4):  # four settled losses today: -4400
        store.upsert_round({"slug": f"l{i}", "round_start": "2026-09-30T18:00:00+00:00",
                            "fill_status": "FILLED", "profit_loss_inr": -1100.0, "result": "DOWN"})
    decision = risk.check_trade("b", 1100, now)
    assert not decision.allowed and "worst case" in decision.reason
    store.upsert_round({"slug": "l4", "round_start": "2026-09-30T18:05:00+00:00",
                        "fill_status": "FILLED", "profit_loss_inr": -1100.0, "result": "DOWN"})
    assert risk.daily_limit_reached(now)
    store.close()


def test_daily_loss_day_boundary_is_local_time(tmp_path):
    # 18:00 UTC on Sep 30 is 23:30 IST, i.e. "yesterday" once it is past midnight in India.
    cfg = Config(db_path=tmp_path / "t.db", csv_path=tmp_path / "t.csv", stop_file=tmp_path / "STOP",
                 timezone="Asia/Kolkata")
    store = TradeStore(cfg.db_path, cfg.csv_path)
    store.upsert_round({"slug": "old", "round_start": "2026-09-30T18:00:00+00:00", "profit_loss_inr": -5000.0})
    risk = RiskManager(cfg, store)
    assert risk.daily_limit_reached(START - 3600)       # 23:15 IST Sep 30: same day
    assert not risk.daily_limit_reached(START + 100)    # 00:16 IST Oct 1: new day
    store.close()


def test_orders_table_rejects_duplicates(tmp_path):
    store = TradeStore(tmp_path / "t.db", tmp_path / "t.csv")
    assert store.record_order("x", "UP", 1100, "PAPER", {})
    assert not store.record_order("x", "UP", 1100, "PAPER", {})
    store.close()
