"""Paper account: positions, manual orders, take-profit / stop-loss exits and settlement.

Money moves the way it would on Polymarket:
- a buy walks the real ask side of the book (plus complementary bids) and pays the taker fee
- a sell (take profit, stop loss or "Sell now") walks the real bid side and pays the taker fee
- at resolution each share of the winning side pays 1 USDC and the losing side pays 0

Exit rules, checked on every poll for positions in the live round:
- take profit fires when the best bid reaches the target, i.e. when you could actually sell there
- stop loss fires when the displayed price falls to the stop; it then sells at whatever the bids
  pay, which can be below the stop (slippage), exactly like a real stop-market order
"""
from __future__ import annotations

from dataclasses import dataclass

from config import Config
from logger import TradeStore
from quotes import (
    Book, Fill, displayed_probability, simulate_buy, simulate_sell, with_complement_asks, with_complement_bids,
)

BOT, MANUAL = "BOT", "MANUAL"
OPEN, CLOSED, SETTLED = "OPEN", "CLOSED", "SETTLED"
TAKE_PROFIT, STOP_LOSS, SOLD, RESOLVED, UNRESOLVED = "TAKE_PROFIT", "STOP_LOSS", "SOLD", "RESOLVED", "UNRESOLVED"
SIDES = ("UP", "DOWN")


def signed_inr(v: float) -> str:
    return f"{'+' if v > 0 else '−' if v < 0 else ''}₹{abs(v):,.2f}"


def validate_exits(take_profit: float | None, stop_loss: float | None) -> str:
    """'' if the exit prices (0-1) are usable, else what's wrong."""
    for name, v in (("Take profit", take_profit), ("Stop loss", stop_loss)):
        if v is not None and not 0.01 <= v <= 0.99:
            return f"{name} must be between 1¢ and 99¢"
    if take_profit is not None and stop_loss is not None and take_profit <= stop_loss:
        return "Take profit must be above stop loss"
    return ""


@dataclass(frozen=True)
class Books:
    up: Book
    down: Book

    def own(self, side: str) -> Book:
        return self.up if side == "UP" else self.down

    def other(self, side: str) -> Book:
        return self.down if side == "UP" else self.up

    def buy_book(self, side: str) -> Book:
        return with_complement_asks(self.own(side), self.other(side))

    def sell_book(self, side: str) -> Book:
        return with_complement_bids(self.own(side), self.other(side))

    def price(self, side: str) -> float | None:
        return displayed_probability(self.own(side))

    def quote(self) -> dict:
        """Best prices per side, as shown in the dashboard's trade ticket."""
        out = {}
        for side in SIDES:
            buy, sell = self.buy_book(side), self.sell_book(side)
            out[side] = {"price": self.price(side), "ask": buy.best_ask, "bid": sell.best_bid}
        return out


class PaperAccount:
    def __init__(self, cfg: Config, store: TradeStore):
        self.cfg = cfg
        self.store = store

    # --- money ---------------------------------------------------------------------
    def cash_inr(self) -> float:
        return self.cfg.starting_balance_inr + self.store.total_realized_inr() - self.store.open_exposure_inr()

    # --- opening -------------------------------------------------------------------
    def quote_buy(self, books: Books, side: str, amount_inr: float) -> Fill:
        return simulate_buy(books.buy_book(side), amount_inr / self.cfg.inr_per_usd,
                            self.cfg.max_entry_price, self.cfg.taker_fee_rate)

    def record_open(self, *, source: str, rnd, side: str, fill: Fill, amount_inr: float,
                    take_profit: float | None, stop_loss: float | None, mode: str, now_iso: str,
                    books: Books | None = None) -> int:
        mark = books.price(side) if books else None
        return self.store.insert_position({
            "source": source, "slug": rnd.slug, "question": rnd.question,
            "round_start": rnd.start.isoformat(timespec="seconds"),
            "round_end": rnd.end.isoformat(timespec="seconds"),
            "side": side, "opened_at": now_iso, "mode": mode,
            "stake_inr": round(amount_inr, 2), "stake_usd": round(fill.usd_spent, 6),
            "shares": round(fill.shares, 6), "entry_price": round(fill.avg_price, 6),
            "fee_usd": round(fill.fee_usd, 6),
            "take_profit": take_profit, "stop_loss": stop_loss,
            "mark_price": round(mark, 4) if mark is not None else None,
            "mark_bid": books.sell_book(side).best_bid if books else None,
            "mark_at": now_iso if books else None,
            "status": OPEN,
        })

    # --- closing -------------------------------------------------------------------
    def close(self, pos: dict, books: Books, reason: str, now_iso: str) -> tuple[bool, str]:
        sale = simulate_sell(books.sell_book(pos["side"]), pos["shares"], self.cfg.taker_fee_rate)
        if not sale.filled:
            return False, sale.reason
        pnl_usd = sale.proceeds_usd - sale.fee_usd - pos["stake_usd"] - pos["fee_usd"]
        pnl_inr = pnl_usd * self._rate(pos)
        self.store.update_position(pos["id"], {
            "status": CLOSED, "close_reason": reason, "exit_price": round(sale.avg_price, 6),
            "exit_value_usd": round(sale.proceeds_usd, 6), "exit_fee_usd": round(sale.fee_usd, 6),
            "closed_at": now_iso, "pnl_usd": round(pnl_usd, 6), "pnl_inr": round(pnl_inr, 2),
        })
        return True, f"sold {pos['shares']:.2f} {pos['side']} @ {sale.avg_price * 100:.1f}¢, P/L {signed_inr(pnl_inr)}"

    def update_marks_and_exits(self, slug: str, books: Books, now_iso: str) -> list[str]:
        """Re-value open positions in this round and fire any take profit / stop loss."""
        events = []
        for pos in self.store.open_positions(slug):
            side = pos["side"]
            mark, bid = books.price(side), books.sell_book(side).best_bid
            if mark is not None:
                self.store.update_position(pos["id"], {"mark_price": round(mark, 4), "mark_bid": bid, "mark_at": now_iso})
            reason = None
            if pos["take_profit"] is not None and bid is not None and bid >= pos["take_profit"]:
                reason = TAKE_PROFIT
            elif pos["stop_loss"] is not None and mark is not None and mark <= pos["stop_loss"]:
                reason = STOP_LOSS
            if reason:
                ok, msg = self.close(pos, books, reason, now_iso)
                label = "Take profit" if reason == TAKE_PROFIT else "Stop loss"
                events.append(f"#{pos['id']} {pos['source']} {label}: {msg}" if ok
                              else f"#{pos['id']} {pos['source']} {label} triggered but could not sell: {msg}")
        return events

    def set_exits(self, pos_id: int, take_profit: float | None, stop_loss: float | None) -> tuple[bool, str]:
        pos = self.store.get_position(pos_id)
        if pos is None or pos["status"] != OPEN:
            return False, "That position is no longer open"
        problem = validate_exits(take_profit, stop_loss)
        if problem:
            return False, problem
        self.store.update_position(pos_id, {"take_profit": take_profit, "stop_loss": stop_loss})
        return True, "Exits updated"

    # --- settlement ------------------------------------------------------------------
    def settle(self, slug: str, result: str, now_iso: str) -> list[dict]:
        """Pay out every open position in a resolved round. result is UP, DOWN or UNRESOLVED."""
        settled = []
        for pos in self.store.open_positions(slug):
            if result == UNRESOLVED:  # market never resolved: stake is returned, no P/L
                fields = {"status": CLOSED, "close_reason": UNRESOLVED, "closed_at": now_iso, "result": result}
            else:
                payout = pos["shares"] if pos["side"] == result else 0.0
                pnl_usd = payout - pos["stake_usd"] - pos["fee_usd"]
                fields = {
                    "status": SETTLED, "close_reason": RESOLVED, "closed_at": now_iso, "result": result,
                    "exit_price": 1.0 if payout else 0.0, "exit_value_usd": round(payout, 6), "exit_fee_usd": 0.0,
                    "pnl_usd": round(pnl_usd, 6), "pnl_inr": round(pnl_usd * self._rate(pos), 2),
                }
            self.store.update_position(pos["id"], fields)
            settled.append({**pos, **fields})
        return settled

    @staticmethod
    def _rate(pos: dict) -> float:
        """₹ per $ this position was opened at."""
        return pos["stake_inr"] / pos["stake_usd"]


def wallet_summary(positions: list[dict], starting_balance_inr: float, now_iso: str) -> dict:
    """The paper account valued like a real one (pure function over position rows).

    - cash      = starting balance + realized P/L - money in open positions (stake + buy fee)
    - value     = shares x price Polymarket displays for the held side (how its portfolio values it)
    - sell now  = shares x best bid (roughly what selling immediately would fetch)
    - equity    = cash + value of open positions; total P/L = equity - starting balance
    """
    realized = sum(p["pnl_inr"] or 0 for p in positions)
    open_rows, closed_rows = [], []
    for p in positions:
        (open_rows if p["status"] == OPEN else closed_rows).append(p)

    out_open = []
    for p in open_rows:
        rate = p["stake_inr"] / p["stake_usd"]
        cost = p["stake_inr"] + p["fee_usd"] * rate
        mark = p["mark_price"] if p["mark_price"] is not None else p["entry_price"]
        value = p["shares"] * mark * rate
        bid = p["mark_bid"]
        out_open.append({
            "id": p["id"], "source": p["source"], "slug": p["slug"], "question": p["question"],
            "side": p["side"], "shares": p["shares"], "entry_price": p["entry_price"],
            "stake_inr": p["stake_inr"], "cost_inr": round(cost, 2),
            "take_profit": p["take_profit"], "stop_loss": p["stop_loss"],
            "mark_price": mark, "mark_bid": bid, "mark_at": p["mark_at"],
            "value_inr": round(value, 2),
            "sell_value_inr": round(p["shares"] * bid * rate, 2) if bid is not None else None,
            "unrealized_inr": round(value - cost, 2),
            "max_win_inr": round(p["shares"] * rate - cost, 2),
            "status": "live" if (p["round_end"] or "") > now_iso else "awaiting result",
            "round_end": p["round_end"],
        })

    open_cost = sum(p["cost_inr"] for p in out_open)
    positions_value = sum(p["value_inr"] for p in out_open)
    cash = starting_balance_inr + realized - open_cost
    equity = cash + positions_value
    done = [p for p in closed_rows if p["pnl_inr"] is not None]
    by_source = {
        src: round(sum(p["pnl_inr"] for p in done if p["source"] == src), 2) for src in (BOT, MANUAL)
    }
    return {
        "starting_balance_inr": starting_balance_inr,
        "cash_inr": round(cash, 2),
        "positions_value_inr": round(positions_value, 2),
        "equity_inr": round(equity, 2),
        "realized_inr": round(realized, 2),
        "realized_by_source_inr": by_source,
        "unrealized_inr": round(sum(p["unrealized_inr"] for p in out_open), 2),
        "total_pnl_inr": round(equity - starting_balance_inr, 2),
        "return_pct": round(100 * (equity - starting_balance_inr) / starting_balance_inr, 3),
        "closed_trades": len(done),
        "wins": sum(1 for p in done if p["pnl_inr"] > 0),
        "losses": sum(1 for p in done if p["pnl_inr"] < 0),
        "positions": out_open,
    }
