"""Summarise paper results: how often the 98% rule fired, filled, and won.

    python report.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from config import load_config

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


def main() -> int:
    cfg = load_config()
    if not cfg.db_path.exists():
        print(f"No database yet at {cfg.db_path}. Run the bot first.")
        return 1
    s = summarize(load_rounds(cfg.db_path))

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
