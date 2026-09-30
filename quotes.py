"""Order book helpers: the displayed percentage and a realistic paper fill.

Polymarket's website shows the *midpoint* of best bid and best ask, or the last
traded price when the spread is wider than 10 cents. That displayed number is what
the 98% rule looks at. A real buy, however, pays the *ask*, and near 98-99% there
is often little or nothing on offer, so paper fills walk the real ask side of the
book instead of pretending we bought at the displayed number.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_DISPLAY_SPREAD = 0.10


@dataclass(frozen=True)
class Book:
    bids: list[tuple[float, float]]  # (price, size) best first (highest price)
    asks: list[tuple[float, float]]  # (price, size) best first (lowest price)
    last_trade: float | None
    timestamp_ms: int | None
    min_order_size: float

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None


def parse_book(raw: dict) -> Book:
    def levels(side: str, reverse: bool) -> list[tuple[float, float]]:
        out = []
        for lvl in raw.get(side) or []:
            price, size = float(lvl["price"]), float(lvl["size"])
            if 0 < price < 1 and size > 0:
                out.append((price, size))
        return sorted(out, key=lambda x: x[0], reverse=reverse)

    last = raw.get("last_trade_price")
    ts = raw.get("timestamp")
    return Book(
        bids=levels("bids", reverse=True),
        asks=levels("asks", reverse=False),
        last_trade=float(last) if last not in (None, "") else None,
        timestamp_ms=int(ts) if ts not in (None, "") else None,
        min_order_size=float(raw.get("min_order_size") or 0),
    )


def displayed_probability(book: Book) -> float | None:
    """Price the website displays for this outcome (0..1), or None if unknown."""
    bid, ask = book.best_bid, book.best_ask
    if bid is not None and ask is not None and ask - bid <= MAX_DISPLAY_SPREAD + 1e-9:
        return (bid + ask) / 2
    return book.last_trade


def to_pct(prob: float | None) -> float | None:
    return None if prob is None else round(prob * 100, 4)


@dataclass(frozen=True)
class Fill:
    filled: bool
    reason: str
    usd_spent: float = 0.0
    shares: float = 0.0
    avg_price: float = 0.0
    fee_usd: float = 0.0


def with_complement_asks(book: Book, other: Book) -> Book:
    """Everything you could buy this outcome from.

    Besides this outcome's own asks, a buy can match a *buy* of the opposite outcome
    (the exchange mints a Yes/No pair), so a bid of p on the other side acts like an
    ask of 1 - p here.
    """
    merged: dict[float, float] = {}
    for price, size in book.asks:
        merged[price] = merged.get(price, 0.0) + size
    for price, size in other.bids:
        comp = round(1 - price, 6)
        merged[comp] = merged.get(comp, 0.0) + size
    asks = sorted(merged.items())
    return Book(book.bids, asks, book.last_trade, book.timestamp_ms, book.min_order_size)


def polymarket_taker_fee(shares: float, price: float, fee_rate: float) -> float:
    """fee = C * feeRate * p * (1 - p)   (docs.polymarket.com/trading/fees)."""
    return shares * fee_rate * price * (1 - price)


def simulate_buy(book: Book, usd: float, max_price: float, fee_rate: float) -> Fill:
    """Fill-or-kill market buy of `usd` worth of shares, never paying above `max_price`."""
    if usd <= 0:
        return Fill(False, "stake must be positive")
    remaining = usd
    shares = 0.0
    fee = 0.0
    for price, size in book.asks:
        if price > max_price:
            break
        take_usd = min(remaining, price * size)
        take_shares = take_usd / price
        shares += take_shares
        fee += polymarket_taker_fee(take_shares, price, fee_rate)
        remaining -= take_usd
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        best = book.best_ask
        if best is None:
            return Fill(False, "no sellers (empty ask side)")
        return Fill(False, f"not enough asks at <= {max_price:.2f} (best ask {best:.3f})")
    if book.min_order_size and shares < book.min_order_size:
        return Fill(False, f"{shares:.2f} shares is below min order size {book.min_order_size}")
    return Fill(True, "filled", usd_spent=usd, shares=shares, avg_price=usd / shares, fee_usd=fee)
