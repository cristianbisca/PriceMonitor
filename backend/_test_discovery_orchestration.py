"""Test the find_alternate_links orchestration with the network layer mocked.

Covers: code method + early stop, model method, name method, keyword (loose title)
method, no-cheaper outcome, URL exclusions (main / existing alternates / dismissed)
and the ASIN branch. No network access is required.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

_tmpdir = tempfile.mkdtemp(prefix="pm_disc_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir.replace(os.sep, '/')}pm_discovery.db"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database as dbmod  # noqa: E402
import alternate_links as alt  # noqa: E402
from models import Base, LinkCandidate, PriceEntry, Product  # noqa: E402

SessionLocal = alt.SessionLocal
Base.metadata.create_all(dbmod.engine)

alt.FETCH_DELAY_SECONDS = 0.0   # don't sleep in tests


def now_utc():
    return datetime.now(timezone.utc)


def jsonld_page(name, price, extra=None, extra_body=""):
    obj = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "offers": {"@type": "Offer", "price": f"{price:.2f}", "priceCurrency": "RON"},
    }
    if extra:
        obj.update(extra)
    return (f'<html><head><script type="application/ld+json">{json.dumps(obj)}</script></head>'
            f'<body>{extra_body}</body></html>')


def seed_product(name, url, price=None, alternative_urls=None):
    s = SessionLocal()
    try:
        p = Product(user_id=1, name=name, url=url, alternative_urls=json.dumps(alternative_urls) if alternative_urls else None)
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
        if price is not None:
            cycle = now_utc()
            s.add(PriceEntry(product_id=pid, price=price, checked_at=cycle, check_cycle=cycle, source="mea.ro"))
            s.commit()
        return pid
    finally:
        s.close()


def run_with_mocks(pid, pages, searches, mock_name_verify=True):
    """Run find_alternate_links with the network layer faked (_fetch_page,
    _search_candidates). _verify_name_candidate is real unless stubbed, so the
    real name-matching logic is exercised too."""
    calls = {}
    fetch_log = []

    def fake_fetch(url):
        fetch_log.append(url)
        if url not in pages:
            raise ConnectionError(f"mock: no page for {url}")
        return pages[url]

    def fake_search(code, suffix, main_host, limit=10, quoted=True):
        calls.setdefault("search", []).append(code)
        calls.setdefault("search_quoted", []).append(quoted)
        val = searches.get(code, [])
        if isinstance(val, dict):  # per-quoted-ness results: {True: [...], False: [...]}
            val = val.get(quoted, [])
        return list(val)

    def fake_name_verify(url, original_name):
        calls.setdefault("name_verify", []).append(url)
        return None

    orig_fetch = alt._fetch_page
    orig_search = alt._search_candidates
    orig_name_verify = alt._verify_name_candidate
    orig_model = alt._extract_model_names
    model_calls = []
    try:
        alt._fetch_page = fake_fetch
        alt._search_candidates = fake_search
        if mock_name_verify:
            alt._verify_name_candidate = fake_name_verify

        def counting_model(soup):
            model_calls.append(1)
            return orig_model(soup)

        alt._extract_model_names = counting_model
        summary = alt.find_alternate_links(pid)
    finally:
        alt._fetch_page = orig_fetch
        alt._search_candidates = orig_search
        alt._verify_name_candidate = orig_name_verify
        alt._extract_model_names = orig_model
    calls["fetches"] = fetch_log
    calls["model_calls"] = model_calls
    return summary, calls


def pending_for(pid):
    s = SessionLocal()
    try:
        rows = s.query(LinkCandidate).filter(
            LinkCandidate.product_id == pid, LinkCandidate.status == "pending").all()
        return {r.url: (r.price, r.match_method) for r in rows}
    finally:
        s.close()


def main():
    # ── A: EAN code method hits, later candidates never fetched (early stop) ──
    pages = {
        "https://mea.ro/product-a": jsonld_page("Product A", 100, {"gtin": "4744131012001"}),
        "https://alt1.ro/p": jsonld_page("Product A", 90, {"gtin": "4744131012001"}),
        "https://alt2.ro/p": jsonld_page("Product A", 120, {"gtin": "4744131012001"}),
        "https://alt3.ro/p": jsonld_page("Product A", 80),  # same product but code missing
    }
    pid = seed_product("A", "https://mea.ro/product-a", price=100)
    summary, calls = run_with_mocks(pid, pages, {"4744131012001": ["https://alt1.ro/p", "https://alt2.ro/p", "https://alt3.ro/p"]})
    assert summary["success"] is True, summary
    assert summary["code"] == {"type": "ean", "value": "4744131012001"}, summary
    assert summary["candidates_found"] == 1 and summary["pending_candidates"] == 1, summary
    assert "Found 1 new candidate" in summary["message"], summary
    assert calls["search"] == ["4744131012001"], calls["search"]
    assert calls["fetches"] == ["https://mea.ro/product-a", "https://alt1.ro/p"], calls["fetches"]  # alt2/alt3 never fetched
    assert "name_verify" not in calls
    assert calls["model_calls"] == [], calls["model_calls"]  # model method skipped after a promising code match
    assert pending_for(pid) == {"https://alt1.ro/p": (90.0, "code")}, pending_for(pid)

    # ── B: no EAN, MPN model method (case-insensitive verify) ──
    pages = {
        "https://mea.ro/product-b": jsonld_page("Product B", 50, {"sku": "X100-PRO"}),
        "https://alt4.ro/x": jsonld_page("Product B", 40),
    }
    pages["https://alt4.ro/x"] = pages["https://alt4.ro/x"].replace("</body>", "<p>model: x100-pro</p></body>")
    pid = seed_product("B", "https://mea.ro/product-b", price=50)
    summary, calls = run_with_mocks(pid, pages, {"X100-PRO": ["https://alt4.ro/x"]})
    assert summary["success"] is True, summary
    assert summary["code"] is None, summary
    assert summary["candidates_found"] == 1, summary
    assert calls["search"] == ["X100-PRO"], calls["search"]
    assert "name_verify" not in calls, calls
    assert calls["model_calls"] == [1], calls["model_calls"]
    assert pending_for(pid) == {"https://alt4.ro/x": (40.0, "model")}, pending_for(pid)

    # ── C: no code, no MPN -> name method (matching + rejections) ──
    pages = {
        "https://mea.ro/product-c": jsonld_page("Bosch GSR 18V-150 Akku-Bohrmaschine", 50),
        "https://alt5.ro/bosch": jsonld_page("Bosch GSR 18V-150 Akku-Bohrmaschine 1500W", 45),
        "https://alt5b.ro/other": jsonld_page("Samsung 20V Brushless Drill Machine", 5),  # different product
        "https://alt5c.ro/acc": jsonld_page("Bosch GSR 18V-150 Case", 3),  # accessory, not the product
    }
    pid = seed_product("C", "https://mea.ro/product-c", price=50)
    name = "Bosch GSR 18V-150 Akku-Bohrmaschine"
    # mock_name_verify=False: the real name matching (name extraction + similarity) runs.
    # Rejections come first so they are actually tried, then the good one stops the probe.
    summary, calls = run_with_mocks(pid, pages, {name: ["https://alt5b.ro/other", "https://alt5c.ro/acc", "https://alt5.ro/bosch"]}, mock_name_verify=False)
    assert summary["success"] is True, summary
    assert calls["search"] == [name], calls["search"]
    assert calls["fetches"] == [
        "https://mea.ro/product-c", "https://alt5b.ro/other", "https://alt5c.ro/acc", "https://alt5.ro/bosch"
    ], calls["fetches"]
    assert calls["model_calls"] == [1], calls["model_calls"]  # tried (no MPN found) before the name method
    assert pending_for(pid) == {"https://alt5.ro/bosch": (45.0, "name")}, pending_for(pid)

    # ── D: verified candidate is NOT cheaper -> nothing stored ──
    pages = {
        "https://mea.ro/product-d": jsonld_page("Product D", 50, {"gtin": "4744131012001"}),
        "https://alt6.ro/p": jsonld_page("Product D", 80, {"gtin": "4744131012001"}),
    }
    pid = seed_product("D", "https://mea.ro/product-d", price=50)
    summary, calls = run_with_mocks(pid, pages, {"4744131012001": ["https://alt6.ro/p"]})
    assert summary["success"] is True, summary
    assert summary["candidates_found"] == 0 and summary["pending_candidates"] == 0, summary
    assert summary["message"] == "No cheaper candidate links found", summary
    # non-promising code result -> model (empty), name and the looser keyword method
    # all still ran; the keyword method re-searches the same name unquoted.
    assert calls["search"] == ["4744131012001", "Product D", "Product D"], calls["search"]
    assert calls["search_quoted"] == [True, True, False], calls["search_quoted"]
    assert pending_for(pid) == {}, pending_for(pid)

    # ── E: main URL, existing alternates and dismissed URLs are excluded ──
    pid = seed_product("E", "https://mea.ro/product-e", price=100, alternative_urls=["https://exist.ro/e/"])
    s = SessionLocal()
    try:
        s.add(LinkCandidate(product_id=pid, url="https://dead.ro/e", price=10.0, match_method="code", status="dismissed", found_at=now_utc(), decided_at=now_utc()))
        s.commit()
    finally:
        s.close()
    pages = {
        "https://mea.ro/product-e": jsonld_page("Product E", 100, {"gtin": "4744131012001"}),
        "https://dead.ro/e": jsonld_page("Product E", 10, {"gtin": "4744131012001"}),
        "https://exist.ro/e/": jsonld_page("Product E", 10, {"gtin": "4744131012001"}),
        "https://fresh.ro/e": jsonld_page("Product E", 90, {"gtin": "4744131012001"}),
    }
    search_results = ["https://dead.ro/e", "https://exist.ro/e/", "https://mea.ro/product-e", "https://fresh.ro/e"]
    summary, calls = run_with_mocks(pid, pages, {"4744131012001": search_results})
    assert summary["success"] is True, summary
    # dead/dismissed is the first result and is skipped; fresh.ro (90 < 100) makes it promising
    assert calls["fetches"] == ["https://mea.ro/product-e", "https://fresh.ro/e"], calls["fetches"]
    assert pending_for(pid) == {"https://fresh.ro/e": (90.0, "code")}, pending_for(pid)

    # ── F: ASIN branch (amazon candidate built directly, no web search) ──
    # A non-amazon store that carries the ASIN in product metadata; the real
    # _asin_candidates builds https://amazon.ro/dp/<ASIN> for suffix "ro".
    pages = {
        "https://mea.ro/product-f": jsonld_page("Product F", 40, {"sku": "B0ABC123XY"}),
        "https://amazon.ro/dp/B0ABC123XY": jsonld_page("Product F", 30),
    }
    pages["https://amazon.ro/dp/B0ABC123XY"] = pages["https://amazon.ro/dp/B0ABC123XY"].replace(
        "</body>", "<a href='/dp/B0ABC123XY'>B0ABC123XY</a></body>")
    pid = seed_product("F", "https://mea.ro/product-f", price=40)

    fetch_log = []

    def fake_fetch(url):
        fetch_log.append(url)
        if url not in pages:
            raise ConnectionError(f"mock: no page for {url}")
        return pages[url]

    def fake_search(code, suffix, main_host, limit=10, quoted=True):
        raise AssertionError("web search must not run for the ASIN branch")

    orig_fetch, orig_search = alt._fetch_page, alt._search_candidates
    try:
        alt._fetch_page = fake_fetch
        alt._search_candidates = fake_search
        summary = alt.find_alternate_links(pid)
    finally:
        alt._fetch_page, alt._search_candidates = orig_fetch, orig_search
    assert summary["success"] is True, summary
    assert summary["code"] == {"type": "asin", "value": "B0ABC123XY"}, summary
    assert fetch_log == ["https://mea.ro/product-f", "https://amazon.ro/dp/B0ABC123XY"], fetch_log
    assert pending_for(pid) == {"https://amazon.ro/dp/B0ABC123XY": (30.0, "code")}, pending_for(pid)

    # ── G: exact-phrase (name) search misses; the loose keyword (unquoted) search
    #    finds a same-name store whose title phrases the product differently. The
    #    2x8GB kit the phrase search surfaces is rejected by the capacity guard, and
    #    the keyword hit is stored as a "keyword" candidate ──
    pages = {
        "https://mea.ro/product-g": jsonld_page("Memorie Kingston Fury 16GB DDR4 3200MHz", 800),
        "https://kit.ro/kit": jsonld_page("Memorie Kingston Fury 16GB (2x8GB) DDR4 3200MHz", 500),
        "https://altex.ro/ram": jsonld_page("Memorie Kingston Fury 16GB DDR4 3200MHz CL20", 600),
    }
    pid = seed_product("G", "https://mea.ro/product-g", price=800)
    name = "Memorie Kingston Fury 16GB DDR4 3200MHz"
    # quoted (name method) -> a 2x8GB kit; unquoted (keyword method) -> the real store
    searches = {name: {True: ["https://kit.ro/kit"], False: ["https://altex.ro/ram"]}}
    summary, calls = run_with_mocks(pid, pages, searches, mock_name_verify=False)
    assert summary["success"] is True, summary
    assert summary["candidates_found"] == 1, summary
    assert calls["search"] == [name, name], calls["search"]
    assert calls["search_quoted"] == [True, False], calls["search_quoted"]
    assert calls["fetches"] == [
        "https://mea.ro/product-g", "https://kit.ro/kit", "https://altex.ro/ram"
    ], calls["fetches"]
    assert pending_for(pid) == {"https://altex.ro/ram": (600.0, "keyword")}, pending_for(pid)

    print("ALL DISCOVERY ORCHESTRATION TESTS PASSED")


if __name__ == "__main__":
    main()
