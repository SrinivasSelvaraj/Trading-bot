"""Optional browser mirror (Playwright).

Opens the current round's Polymarket page in a browser window so you can watch
along, and reads the displayed Up/Down prices as a cross-check against the API.

This module is READ-ONLY by design: it navigates and reads text, and never clicks
buy/sell buttons, types amounts or submits anything. Paper mode needs none of that.

Page text is matched on the visible labels ("Up 99¢", "Down 1¢") rather than on
screen coordinates or generated CSS class names. If the site layout changes and
the values can't be read, `read_displayed()` returns None and (when
BROWSER_CROSSCHECK is on) the bot does not trade that round.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("bot")

_PRICE = r"([0-9]{1,3}(?:\.[0-9]+)?)\s*¢"
UP_RE = re.compile(r"\bUp\b\s*" + _PRICE)
DOWN_RE = re.compile(r"\bDown\b\s*" + _PRICE)


def parse_displayed(text: str) -> tuple[float, float] | None:
    """Extract (up_pct, down_pct) from page text like 'Up 99¢ ... Down 1¢'."""
    up = UP_RE.search(text)
    down = DOWN_RE.search(text)
    if not up or not down:
        return None
    up_pct, down_pct = float(up.group(1)), float(down.group(1))
    if not (0 <= up_pct <= 100 and 0 <= down_pct <= 100):
        return None
    return up_pct, down_pct


class BrowserMirror:
    def __init__(self, headless: bool = False, executable_path: str = ""):
        self.headless = headless
        self.executable_path = executable_path or None
        self._pw = None
        self._browser = None
        self._page = None
        self.current_url: str | None = None

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"
            ) from exc
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless, executable_path=self.executable_path)
        self._page = self._browser.new_page()

    def open(self, url: str) -> None:
        if self._page is None or url == self.current_url:
            return
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            self._page.keyboard.press("Escape")  # close any welcome/region popup without clicking
            self.current_url = url
        except Exception as exc:  # noqa: BLE001 - browser errors must never crash the bot
            log.warning("Browser could not open %s: %s", url, exc)
            self.current_url = None

    def read_displayed(self) -> tuple[float, float] | None:
        if self._page is None or self.current_url is None:
            return None
        try:
            return parse_displayed(self._page.inner_text("body", timeout=5_000))
        except Exception as exc:  # noqa: BLE001
            log.warning("Browser read failed: %s", exc)
            return None

    def close(self) -> None:
        for closer in (getattr(self._browser, "close", None), getattr(self._pw, "stop", None)):
            if closer:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        self._page = self._browser = self._pw = None
