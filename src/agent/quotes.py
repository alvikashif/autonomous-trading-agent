"""Resilient market-hours quote fetcher with anti-throttling & multi-source fallback.

Guards against data-center IP rate-limits (e.g. Oracle Cloud):
1. Reusable session with browser-like headers.
2. In-memory quote cache with timestamp to prevent duplicate queries within MIN_FETCH_INTERVAL.
3. Fallback to Google Finance when Yahoo Finance returns 429/403 or times out.
4. Graceful fallback to last-cached price on temporary network blips.
"""
from __future__ import annotations

import logging
import re
import time
import urllib.request
from typing import Mapping

logger = logging.getLogger(__name__)

# Minimum seconds before allowing another remote query for the exact same symbol (tightened for 60s monitor loop)
MIN_FETCH_INTERVAL = 20

# In-memory quote cache: symbol -> (last_price, prev_close, timestamp)
_QUOTE_CACHE: dict[str, tuple[float, float, float]] = {}


def fetch_google_finance_quote(symbol: str, timeout: float = 6.0) -> float | None:
    """Fetch current price from Google Finance as an emergency backup if Yahoo is rate-limited."""
    url = f"https://www.google.com/finance/quote/{symbol}:NSE"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # Match primary price pattern in Google Finance HTML
        m = re.search(r'₹\s*([0-9,]+\.[0-9]+)', html)
        if m:
            val_str = m.group(1).replace(",", "")
            return float(val_str)
        # Secondary fallback: embedded JSON structure
        m2 = re.search(r'\["' + re.escape(symbol) + r'",.*?,\[([0-9]+\.?[0-9]*),', html)
        if m2:
            return float(m2.group(1))
    except Exception as exc:
        logger.debug("Google Finance fallback failed for %s: %s", symbol, exc)
    return None


def fetch_single_quote(symbol: str, force: bool = False) -> tuple[float | None, float | None]:
    """Fetch (last_price, prev_close) for an NSE symbol with caching & fallback."""
    now = time.time()
    sym = symbol.upper().replace(".NS", "")

    # Return cached if still within MIN_FETCH_INTERVAL to strictly prevent rate limiting
    if not force and sym in _QUOTE_CACHE:
        last, prev, ts = _QUOTE_CACHE[sym]
        if now - ts < MIN_FETCH_INTERVAL:
            return last, prev

    last_px: float | None = None
    prev_px: float | None = None

    # Source 1: Yahoo Finance fast_info
    try:
        import yfinance as yf
        ticker = yf.Ticker(f"{sym}.NS")
        fi = ticker.fast_info
        last_px = float(fi["last_price"])
        prev_px = float(fi["previous_close"])
    except Exception as exc:
        logger.warning("Yahoo Finance fast_info failed for %s (rate-limit or error: %s). Trying Google Finance fallback...", sym, exc)
        # Source 2: Google Finance fallback
        gf_px = fetch_google_finance_quote(sym)
        if gf_px is not None and gf_px > 0:
            last_px = gf_px
            # If prev_close was cached, retain it; otherwise estimate from last
            cached_prev = _QUOTE_CACHE.get(sym, (None, None, 0))[1]
            prev_px = cached_prev if cached_prev else last_px

    # If both failed, use last cached price if available
    if last_px is None or last_px <= 0:
        if sym in _QUOTE_CACHE:
            logger.warning("Using last known cached quote for %s", sym)
            return _QUOTE_CACHE[sym][0], _QUOTE_CACHE[sym][1]
        return None, None

    _QUOTE_CACHE[sym] = (last_px, prev_px or last_px, now)
    return last_px, prev_px


def fetch_resilient_quotes(symbols: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    """Batch fetch (last, prev) per symbol safely throttled for Oracle/cloud IPs."""
    last: dict[str, float] = {}
    prev: dict[str, float] = {}
    for s in symbols:
        sym = s.upper().replace(".NS", "")
        lp, pp = fetch_single_quote(sym)
        if lp is not None and lp > 0:
            last[sym] = lp
            prev[sym] = pp or lp
    return last, prev
