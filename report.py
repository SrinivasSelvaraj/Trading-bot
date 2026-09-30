"""Summarise paper results: how often the 98% rule fired, filled, and won.

    python report.py
"""
from __future__ import annotations

import sqlite3
import sys

from config import load_config


def main() -> int:
    cfg = load_config()
    if not cfg.db_path.exists():
        print(f"No database yet at {cfg.db_path}. Run the bot first.")
        return 1
    conn = sqlite3.connect(str(cfg.db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM rounds ORDER BY round_start")]
    conn.close()

    settled = [r for r in rows if r["result"] not in (None, "UNRESOLVED")]
    signals = [r for r in rows if r["decision"] in ("UP", "DOWN")]
    signals_settled = [r for r in signals if r["result"] not in (None, "UNRESOLVED")]
    filled = [r for r in rows if r["fill_status"] == "FILLED"]
    filled_settled = [r for r in filled if r["profit_loss_inr"] is not None]
    wins = [r for r in filled_settled if r["decision"] == r["result"]]
    pnl = sum(r["profit_loss_inr"] for r in filled_settled)
    conflicts = [r for r in rows if r["decision"] == "CONFLICT"]

    print(f"Rounds observed          : {len(rows)} ({len(settled)} resolved)")
    print(f"98% signals              : {len(signals)}")
    if signals_settled:
        right = sum(1 for r in signals_settled if r["decision"] == r["result"])
        print(f"  signal side won        : {right}/{len(signals_settled)} "
              f"({100 * right / len(signals_settled):.2f}%)  <- includes rounds with no fill")
    print(f"Paper trades filled      : {len(filled)}")
    by_reason: dict[str, int] = {}
    for r in signals:
        if r["fill_status"] != "FILLED":
            key = f"{r['fill_status']}: {r['reason']}"
            by_reason[key] = by_reason.get(key, 0) + 1
    for key, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"  not filled             : {n} x {key}")
    if filled_settled:
        losses = len(filled_settled) - len(wins)
        avg_entry = sum(r["entry_price"] for r in filled_settled) / len(filled_settled)
        print(f"  settled                : {len(filled_settled)}  wins {len(wins)}  losses {losses}  "
              f"win rate {100 * len(wins) / len(filled_settled):.2f}%")
        print(f"  average entry price    : {avg_entry:.4f} (break-even win rate ≈ {100 * avg_entry:.2f}% + fees)")
        print(f"  total paper P/L        : ₹{pnl:,.2f}")
    print(f"Conflicts (both >= 98%)  : {len(conflicts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
