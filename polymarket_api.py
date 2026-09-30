"""Read-only HTTP client for Polymarket's public APIs.

- Gamma API: market metadata (which market is the current round, token ids, resolution).
- CLOB API: live order books (the numbers the website shows are derived from these).

Nothing in this module can place, sign or cancel an order.
"""
from __future__ import annotations

import requests

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


class ApiError(RuntimeError):
    pass


class PolymarketClient:
    def __init__(self, timeout: float = 10.0, session: requests.Session | None = None):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "polymarket-btc-paper-bot/1.0")

    def _get(self, url: str, params: dict | None = None):
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ApiError(f"GET {url} failed: {exc}") from exc
        if resp.status_code != 200:
            raise ApiError(f"GET {url} returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(f"GET {url} returned non-JSON body") from exc

    def get_market_by_slug(self, slug: str) -> dict | None:
        """Return the Gamma market dict for `slug`, open or closed, or None if unknown."""
        # Gamma hides closed markets unless asked, so try open first, then closed.
        for params in ({"slug": slug}, {"slug": slug, "closed": "true"}):
            data = self._get(f"{GAMMA_URL}/markets", params)
            if not isinstance(data, list):
                raise ApiError(f"Unexpected Gamma response for {slug}: {type(data).__name__}")
            matches = [m for m in data if isinstance(m, dict) and m.get("slug") == slug]
            if len(matches) > 1:
                raise ApiError(f"Gamma returned {len(matches)} markets for slug {slug}")
            if matches:
                return matches[0]
        return None

    def get_book(self, token_id: str) -> dict:
        data = self._get(f"{CLOB_URL}/book", {"token_id": token_id})
        if not isinstance(data, dict) or "bids" not in data or "asks" not in data:
            raise ApiError(f"Unexpected order book response for token {token_id[:12]}...")
        return data

    def get_btc_spot(self) -> float | None:
        """BTC/USD spot from Coinbase, for logging only (resolution uses Chainlink)."""
        try:
            data = self._get(COINBASE_SPOT_URL)
            return float(data["data"]["amount"])
        except (ApiError, KeyError, TypeError, ValueError):
            return None
