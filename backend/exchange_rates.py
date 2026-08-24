"""
Exchange-rate service for cross-currency price comparison.

Sources (all keyless, public, updated daily), tried in order:
  1. frankfurter.dev  - ECB reference rates, EUR-anchored, clean JSON
  2. open.er-api.com  - ExchangeRate-API free tier, EUR-anchored, ~160 currencies

The EUR-anchored rate table is fetched once per FX_RATE_TTL_HOURS (default 12)
and cached in process memory. Every conversion is EUR-anchored (1 EUR =
rates[X] units of X), so a single table answers any currency pair:
the rate from A to B is rates[B] / rates[A].

If all sources fail, a previously cached table is reused while it is younger
than FX_RATE_MAX_STALE_HOURS (default 168, i.e. one week); after that
conversions are unavailable until a source answers again.
"""

import logging
import os
import re
import threading
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

_FX_SOURCES = (
    ("frankfurter.dev", "https://api.frankfurter.dev/v1/latest?base=EUR"),
    ("open.er-api.com", "https://open.er-api.com/v6/latest/EUR"),
)

# Words that appear in free-form currency fields instead of the ISO code.
_CURRENCY_WORDS = {
    "LEI": "RON",
    "EURO": "EUR",
    "DOLLAR": "USD",
    "POUND": "GBP",
}

_lock = threading.Lock()
_cache: Optional[dict] = None  # {"rates": {ccy: float}, "fetched_at": float, "source": str, "date": str}


def _ttl_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("FX_RATE_TTL_HOURS", "12"))) * 3600
    except (TypeError, ValueError):
        return 12 * 3600


def _max_stale_seconds() -> float:
    try:
        hours = max(float(os.getenv("FX_RATE_TTL_HOURS", "12")),
                    float(os.getenv("FX_RATE_MAX_STALE_HOURS", "168")))
        return hours * 3600
    except (TypeError, ValueError):
        return 168 * 3600


def currency_code(value) -> Optional[str]:
    """Normalize a free-form currency value to a currency code.

    "RON" -> "RON", "eur" -> "EUR", "RON (lei)" -> "RON", "lei" -> "RON".
    Returns None when no usable code can be determined.
    """
    if not value or not isinstance(value, str):
        return None
    match = re.search(r"[A-Za-z]{2,}", value)
    if not match:
        return None
    word = match.group(0).upper()
    if word in _CURRENCY_WORDS:
        return _CURRENCY_WORDS[word]
    if len(word) > 4:
        return None
    return word


def get_rates(force_refresh: bool = False) -> Optional[Dict[str, float]]:
    """Return the EUR-anchored rate table (units of each currency per 1 EUR).

    Uses the cached table while it is younger than the TTL; otherwise fetches
    from the public sources in order. Returns None when no usable table is
    available (all sources failed and no fresh-enough cache exists).
    """
    global _cache
    with _lock:
        if not force_refresh and _cache is not None:
            if time.time() - _cache["fetched_at"] < _ttl_seconds():
                return _cache["rates"]
        for source_name, url in _FX_SOURCES:
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                raw_rates = data.get("rates") if isinstance(data, dict) else None
                if not isinstance(raw_rates, dict):
                    raise ValueError("no 'rates' object in response")
                rates = {}
                for code, amount in raw_rates.items():
                    try:
                        amount = float(amount)
                    except (TypeError, ValueError):
                        continue
                    if amount > 0:
                        rates[str(code).upper()] = amount
                if not rates:
                    raise ValueError("empty rate table")
                rates.setdefault("EUR", 1.0)
                _cache = {
                    "rates": rates,
                    "fetched_at": time.time(),
                    "source": source_name,
                    "date": str(data.get("date") or ""),
                }
                logger.info(
                    f"Exchange rates loaded from {source_name}: "
                    f"{len(rates)} currencies (reference date {_cache['date'] or 'n/a'})"
                )
                return rates
            except Exception as e:
                logger.warning(f"Exchange-rate source {source_name} failed: {e}")
        if _cache is not None:
            age_hours = (time.time() - _cache["fetched_at"]) / 3600
            if age_hours * 3600 <= _max_stale_seconds():
                logger.warning(
                    f"Exchange rates unavailable - using stale table from {_cache['source']} ({age_hours:.1f}h old)"
                )
                return _cache["rates"]
            logger.error(
                f"Exchange rates unavailable - cached table from {_cache['source']} is {age_hours:.1f}h old "
                f"(max {_max_stale_seconds() / 3600:.0f}h); cross-currency comparison disabled until a source answers"
            )
        else:
            logger.error(
                "Exchange rates unavailable - all sources failed; "
                "cross-currency comparison disabled until a source answers"
            )
        return None


def source_info() -> Optional[dict]:
    """Where the current rate table came from, or None when no table is loaded."""
    with _lock:
        if _cache is None:
            return None
        return {"source": _cache["source"], "date": _cache["date"]}


def get_rate(from_ccy, to_ccy, rates: Optional[Dict[str, float]] = None) -> Optional[float]:
    """Multiplier converting an amount from from_ccy to to_ccy.

    Returns 1.0 for identical currencies, None when a code is unknown or no
    rate is available. When ``rates`` is None the cached table is used.
    """
    a, b = currency_code(from_ccy), currency_code(to_ccy)
    if a is None or b is None:
        return None
    if a == b:
        return 1.0
    table = rates if rates is not None else get_rates()
    if table is None:
        return None
    ra, rb = table.get(a), table.get(b)
    if not ra or not rb:
        return None
    return rb / ra


def convert_price(price, from_ccy, to_ccy, rates: Optional[Dict[str, float]] = None) -> Optional[float]:
    """Convert a price from one currency to another.

    Returns None when the price cannot be converted (unknown currency or
    unavailable rate) so the caller can skip the source/candidate instead of
    ranking an uncomparable number.
    """
    if price is None:
        return None
    rate = get_rate(from_ccy, to_ccy, rates)
    if rate is None:
        return None
    return round(price * rate, 2)
