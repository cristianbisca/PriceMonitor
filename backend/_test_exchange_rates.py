"""Unit test for exchange_rates: currency normalization, cache/TTL/stale fallback,
conversion. No network access (requests.get is faked)."""
import os
import sys
import tempfile
import time

_tmpdir = tempfile.mkdtemp(prefix="pm_fx_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir.replace(os.sep, '/')}pm_fx.db"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exchange_rates as fx  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def install_mock(responses):
    """responses: list consumed one per call (may raise). Returns a call counter."""
    state = {"calls": 0}

    def fake_get(url, timeout=None):
        state["calls"] += 1
        item = responses[min(state["calls"] - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    orig = fx.requests.get
    fx.requests.get = fake_get
    return orig, state


def reset_cache():
    fx._cache = None


def main():
    # ── 1) currency_code normalization ──
    assert fx.currency_code("RON") == "RON"
    assert fx.currency_code("eur") == "EUR"
    assert fx.currency_code("lei") == "RON"
    assert fx.currency_code("RON (lei)") == "RON"
    assert fx.currency_code("dollar") == "USD"
    assert fx.currency_code("") is None
    assert fx.currency_code(None) is None
    assert fx.currency_code(123) is None
    assert fx.currency_code("TOOLONG") is None
    assert fx.currency_code("123 456") is None

    # ── 2) fetch + cache: first call fetches, second hits the TTL cache ──
    reset_cache()
    payload = {"rates": {"RON": 5.25, "USD": 1.08}, "date": "2026-08-24"}
    orig, state = install_mock([payload])
    try:
        rates = fx.get_rates()
        assert rates is not None and rates["RON"] == 5.25, rates
        assert rates["EUR"] == 1.0, rates  # EUR anchor always present
        assert state["calls"] == 1
        rates2 = fx.get_rates()
        assert rates2 == rates and state["calls"] == 1, state["calls"]  # cached, no second fetch
        assert fx.source_info() == {"source": "frankfurter.dev", "date": "2026-08-24"}, fx.source_info()
    finally:
        fx.requests.get = orig

    # ── 3) both sources fail, no cache -> None ──
    reset_cache()
    orig, state = install_mock([RuntimeError("down1"), RuntimeError("down2")])
    try:
        assert fx.get_rates() is None
        assert state["calls"] == 2  # both sources tried
        assert fx.source_info() is None
    finally:
        fx.requests.get = orig

    # ── 4) sources fail, fresh cache exists -> stale fallback within the max age ──
    reset_cache()
    fx._cache = {"rates": {"EUR": 1.0, "RON": 5.0}, "fetched_at": time.time() - 24 * 3600,
                 "source": "frankfurter.dev", "date": "2026-08-23"}
    orig, state = install_mock([RuntimeError("down")])
    try:
        assert fx.get_rates() == {"EUR": 1.0, "RON": 5.0}  # 24h old < 168h max stale
    finally:
        fx.requests.get = orig

    # ── 5) cache older than the max stale age -> None even when cached ──
    fx._cache = {"rates": {"EUR": 1.0, "RON": 5.0}, "fetched_at": time.time() - 400 * 3600,
                 "source": "frankfurter.dev", "date": "2026-08-01"}
    orig, _ = install_mock([RuntimeError("down")])
    try:
        assert fx.get_rates() is None
    finally:
        fx.requests.get = orig

    # ── 6) get_rate / convert_price ──
    reset_cache()
    table = {"EUR": 1.0, "RON": 5.0, "USD": 1.1}
    assert fx.get_rate("EUR", "RON", table) == 5.0
    assert fx.get_rate("RON", "EUR", table) == 0.2
    assert abs(fx.get_rate("USD", "RON", table) - 5.0 / 1.1) < 1e-9
    assert fx.get_rate("RON", "RON", table) == 1.0
    assert fx.get_rate("XXX", "RON", table) is None
    assert fx.get_rate("RON", "XXX", table) is None
    assert fx.get_rate("RON", "RON") == 1.0  # identical currencies never need a table

    reset_cache()
    fx._cache = {"rates": table, "fetched_at": time.time(), "source": "test", "date": "2026-08-24"}
    assert fx.get_rate("EUR", "RON") == 5.0  # resolves via the cache
    assert fx.convert_price(27.95, "EUR", "RON") == 139.75
    assert fx.convert_price(100, "RON", "EUR") == 20.0
    assert fx.convert_price(100, "EUR", "XXX") is None
    assert fx.convert_price(None, "EUR", "RON") is None

    print("ALL EXCHANGE RATES TESTS PASSED")


if __name__ == "__main__":
    main()
