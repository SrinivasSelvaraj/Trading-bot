"""The trading rule. Deliberately tiny and deterministic.

UP >= 98%   -> "UP"
DOWN >= 98% -> "DOWN"
otherwise   -> "NO_TRADE"

Safety additions (never produce a trade):
- both sides >= 98% at once  -> "CONFLICT"  (display/data problem: flag and wait)
- missing / non-numeric / out-of-range input -> "INVALID"
"""
from __future__ import annotations

import math

# Fixed on purpose: this is not a user setting. 60/70/80/85% must never trade.
THRESHOLD_PCT = 98.0

UP = "UP"
DOWN = "DOWN"
NO_TRADE = "NO_TRADE"
CONFLICT = "CONFLICT"
INVALID = "INVALID"

TRADE_DECISIONS = (UP, DOWN)


def _valid(pct: object) -> bool:
    return (
        isinstance(pct, (int, float))
        and not isinstance(pct, bool)
        and math.isfinite(pct)
        and 0.0 <= pct <= 100.0
    )


def get_decision(up_percentage: float | None, down_percentage: float | None) -> str:
    if not (_valid(up_percentage) and _valid(down_percentage)):
        return INVALID

    up_hit = up_percentage >= THRESHOLD_PCT
    down_hit = down_percentage >= THRESHOLD_PCT

    if up_hit and down_hit:
        return CONFLICT
    if up_hit:
        return UP
    if down_hit:
        return DOWN
    return NO_TRADE
