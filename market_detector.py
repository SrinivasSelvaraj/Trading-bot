"""Find and verify the current Polymarket BTC 5-minute Up/Down round.

Each round is its own market with a slug of the form `btc-updown-5m-<start>` where
<start> is the round's start time in Unix seconds (always a multiple of 300).
Before anything is traded, the market returned by the API is checked against what
we expect; any mismatch raises MarketVerificationError and the round is skipped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from polymarket_api import PolymarketClient

ROUND_SECONDS = 300
SLUG_PREFIX = "btc-updown-5m-"
EVENT_URL = "https://polymarket.com/event/"
EXPECTED_OUTCOMES = ["Up", "Down"]


class MarketVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Round:
    slug: str
    market_id: str
    condition_id: str
    question: str
    start: datetime
    end: datetime
    up_token: str
    down_token: str
    accepting_orders: bool
    closed: bool

    @property
    def url(self) -> str:
        return EVENT_URL + self.slug

    @property
    def start_ts(self) -> int:
        return int(self.start.timestamp())

    def seconds_left(self, now_ts: float) -> float:
        return self.end.timestamp() - now_ts


def round_start_ts(now_ts: float) -> int:
    return int(now_ts) // ROUND_SECONDS * ROUND_SECONDS


def slug_for(start_ts: int) -> str:
    return f"{SLUG_PREFIX}{start_ts}"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    raise MarketVerificationError(f"expected a JSON list, got {value!r}")


def parse_and_verify(market: dict, expected_start_ts: int) -> Round:
    """Turn a Gamma market dict into a Round, refusing anything that doesn't look right."""
    slug = market.get("slug")
    expected_slug = slug_for(expected_start_ts)
    if slug != expected_slug:
        raise MarketVerificationError(f"slug {slug!r} != expected {expected_slug!r}")

    question = str(market.get("question") or "")
    if "bitcoin up or down" not in question.lower():
        raise MarketVerificationError(f"unexpected question: {question!r}")

    try:
        outcomes = _json_list(market.get("outcomes"))
        tokens = [str(t) for t in _json_list(market.get("clobTokenIds"))]
    except (ValueError, TypeError) as exc:
        raise MarketVerificationError(f"bad outcomes/token ids: {exc}") from exc
    if outcomes != EXPECTED_OUTCOMES:
        raise MarketVerificationError(f"unexpected outcomes {outcomes!r}")
    if len(tokens) != 2 or tokens[0] == tokens[1] or not all(tokens):
        raise MarketVerificationError(f"unexpected token ids {tokens!r}")

    start = datetime.fromtimestamp(expected_start_ts, tz=timezone.utc)
    if market.get("eventStartTime"):
        api_start = _parse_time(market["eventStartTime"])
        if api_start != start:
            raise MarketVerificationError(f"eventStartTime {api_start} != slug start {start}")
    if not market.get("endDate"):
        raise MarketVerificationError("market has no endDate")
    end = _parse_time(market["endDate"])
    if (end - start).total_seconds() != ROUND_SECONDS:
        raise MarketVerificationError(f"round length is {(end - start)}, expected 5 minutes")

    return Round(
        slug=slug,
        market_id=str(market.get("id", "")),
        condition_id=str(market.get("conditionId", "")),
        question=question,
        start=start,
        end=end,
        up_token=tokens[0],
        down_token=tokens[1],
        accepting_orders=bool(market.get("acceptingOrders")),
        closed=bool(market.get("closed")),
    )


def parse_resolution(market: dict) -> str | None:
    """'UP' / 'DOWN' once the market is closed and paid out, else None."""
    if not market.get("closed"):
        return None
    try:
        outcomes = _json_list(market.get("outcomes"))
        prices = [float(p) for p in _json_list(market.get("outcomePrices"))]
    except (ValueError, TypeError, MarketVerificationError):
        return None
    if outcomes != EXPECTED_OUTCOMES or len(prices) != 2:
        return None
    if prices == [1.0, 0.0]:
        return "UP"
    if prices == [0.0, 1.0]:
        return "DOWN"
    return None


class MarketDetector:
    def __init__(self, client: PolymarketClient):
        self.client = client

    def get_round(self, start_ts: int) -> Round:
        slug = slug_for(start_ts)
        market = self.client.get_market_by_slug(slug)
        if market is None:
            raise MarketVerificationError(f"market {slug} not found")
        return parse_and_verify(market, start_ts)

    def current_round(self, now_ts: float) -> Round:
        rnd = self.get_round(round_start_ts(now_ts))
        if not rnd.start.timestamp() <= now_ts < rnd.end.timestamp():
            raise MarketVerificationError(f"{rnd.slug} is not live at {now_ts}")
        return rnd

    def resolution(self, slug: str) -> str | None:
        market = self.client.get_market_by_slug(slug)
        return None if market is None else parse_resolution(market)
