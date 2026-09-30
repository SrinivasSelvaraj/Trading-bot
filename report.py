"""Summarise paper results: how often the 98% rule fired, filled, and won.

    python report.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from config import load_config
from logger import utc_iso

TRADE_SIDES = ("UP", "DOWN")


def load_rounds(db_path: Path) -> list[dict]:
    """All recorded rounds, oldest first. Opens the database read-only."""
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM rounds ORDER BY round_start")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def summarize(rows: list[dict]) -> dict:
    resolved = [r for r in rows if r["result"] not in (None, "UNRESOLVED")]
    signals = [r for r in rows if r["decision"] in TRADE_SIDES]
    signals_resolved = [r for r in signals if r["result"] not in (None, "UNRESOLVED")]
    filled = [r for r in rows if r["fill_status"] == "FILLED"]
    settled = [r for r in filled if r["profit_loss_inr"] is not None]
    wins = [r for r in settled if r["decision"] == r["result"]]

    not_filled: dict[str, int] = {}
    for r in signals:
        if r["fill_status"] != "FILLED":
            key = f"{r['fill_status']}: {r['reason']}"
            not_filled[key] = not_filled.get(key, 0) + 1

    avg_entry = sum(r["entry_price"] for r in settled) / len(settled) if settled else None
    return {
        "rounds": len(rows),
        "resolved": len(resolved),
        "signals": len(signals),
        "signals_resolved": len(signals_resolved),
        "signal_side_won": sum(1 for r in signals_resolved if r["decision"] == r["result"]),
        "filled": len(filled),
        "open": len(filled) - len(settled),
        "settled": len(settled),
        "wins": len(wins),
        "losses": len(settled) - len(wins),
        "win_rate": 100 * len(wins) / len(settled) if settled else None,
        "avg_entry": avg_entry,
        "breakeven_rate": 100 * avg_entry if avg_entry is not None else None,
        "pnl_inr": round(sum(r["profit_loss_inr"] for r in settled), 2),
        "conflicts": sum(1 for r in rows if r["decision"] == "CONFLICT"),
        "not_filled": dict(sorted(not_filled.items(), key=lambda kv: -kv[1])),
    }


def wallet(rows: list[dict], starting_balance_inr: float, now_iso: str) -> dict:
    """Paper account valued like a real one: cash, open positions at live prices, total P/L.

    - cash        = starting balance + realized P/L - money in open trades (stake + fee)
    - positions   = shares x price Polymarket displays for the held side (the portfolio value)
    - sell value  = shares x best bid (what selling right now would actually fetch)
    - equity      = cash + positions;  total P/L = equity - starting balance
    """
    realized = sum(r["profit_loss_inr"] or 0 for r in rows)
    positions = []
    for r in rows:
        if r["fill_status"] != "FILLED" or r["result"] is not None or not r.get("stake_usd"):
            continue
        rate = r["stake_inr"] / r["stake_usd"]  # ₹ per $ used for this trade
        cost = r["stake_inr"] + (r["fee_usd"] or 0) * rate
        mark = r.get("mark_price") if r.get("mark_price") is not None else r["entry_price"]
        value = r["shares"] * mark * rate
        bid = r.get("mark_bid")
        positions.append({
            "slug": r["slug"], "question": r["question"], "side": r["decision"],
            "shares": r["shares"], "entry_price": r["entry_price"], "cost_inr": round(cost, 2),
            "mark_price": mark, "mark_bid": bid, "mark_at": r.get("mark_at"),
            "value_inr": round(value, 2),
            "sell_value_inr": round(r["shares"] * bid * rate, 2) if bid is not None else None,
            "unrealized_inr": round(value - cost, 2),
            "max_win_inr": round(r["shares"] * rate - cost, 2),
            "status": "live" if r["round_end"] > now_iso else "awaiting result",
            "round_end": r["round_end"],
        })
    open_cost = sum(p["cost_inr"] for p in positions)
    positions_value = sum(p["value_inr"] for p in positions)
    cash = starting_balance_inr + realized - open_cost
    equity = cash + positions_value
    return {
        "starting_balance_inr": starting_balance_inr,
        "cash_inr": round(cash, 2),
        "positions_value_inr": round(positions_value, 2),
        "equity_inr": round(equity, 2),
        "realized_inr": round(realized, 2),
        "unrealized_inr": round(sum(p["unrealized_inr"] for p in positions), 2),
        "total_pnl_inr": round(equity - starting_balance_inr, 2),
        "return_pct": round(100 * (equity - starting_balance_inr) / starting_balance_inr, 3),
        "positions": positions,
    }


def main() -> int:
    cfg = load_config()
    if not cfg.db_path.exists():
        print(f"No database yet at {cfg.db_path}. Run the bot first.")
        return 1
    rows = load_rounds(cfg.db_path)
    s = summarize(rows)
    w = wallet(rows, cfg.starting_balance_inr, utc_iso())

    print(f"Paper wallet             : equity ₹{w['equity_inr']:,.2f} "
          f"(start ₹{w['starting_balance_inr']:,.0f}, total P/L ₹{w['total_pnl_inr']:,.2f}, "
          f"live/unrealized ₹{w['unrealized_inr']:,.2f}, free cash ₹{w['cash_inr']:,.2f})")

    print(f"Rounds observed          : {s['rounds']} ({s['resolved']} resolved)")
    print(f"98% signals              : {s['signals']}")
    if s["signals_resolved"]:
        print(f"  signal side won        : {s['signal_side_won']}/{s['signals_resolved']} "
              f"({100 * s['signal_side_won'] / s['signals_resolved']:.2f}%)  <- includes rounds with no fill")
    print(f"Paper trades filled      : {s['filled']}")
    for key, n in s["not_filled"].items():
        print(f"  not filled             : {n} x {key}")
    if s["settled"]:
        print(f"  settled                : {s['settled']}  wins {s['wins']}  losses {s['losses']}  "
              f"win rate {s['win_rate']:.2f}%")
        print(f"  average entry price    : {s['avg_entry']:.4f} "
              f"(break-even win rate ≈ {s['breakeven_rate']:.2f}% + fees)")
        print(f"  total paper P/L        : ₹{s['pnl_inr']:,.2f}")
    print(f"Conflicts (both >= 98%)  : {s['conflicts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
