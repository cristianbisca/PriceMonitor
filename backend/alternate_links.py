"""
Alternate-link discovery service.

Run either on a schedule (ALTERNATE_LINK_TIMES env var, same pattern as
PRICE_CHECK_TIMES) or manually via the /find-alternates endpoint. For each
product it:

  1. identifies the product by a globally unique code (EAN/UPC/GTIN barcode
     or Amazon ASIN) extracted from the original product page,
  2. searches the web for other stores selling the same product, restricted
     to domains with the same country suffix (e.g. .ro original -> only
     other .ro sites),
   3. verifies every candidate page actually displays the same product code,
   4. saves the cheapest verified links (up to ALTERNATE_LINKS_MAX, default 3)
      to Product.alternative_urls alongside the existing links; price-comparison
      aggregators (price.ro, compari.ro, idealo.de, ...) are never saved because
      their pages list offers from many stores rather than being a shop to monitor.

Store-internal identifiers (eMAG p-1234567, Notino variant IDs, plain model
numbers) are deliberately rejected: they only identify a product inside one
store, so using them could save a link pointing at a different item.
"""

import json
import logging
import os
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

from bs4 import BeautifulSoup

from database import SessionLocal
from models import Product
from price_checker import (
    SESSION,
    _fetch_page,
    _extract_main_domain,
    _get_alternative_urls,
    extract_price_auto,
)

logger = logging.getLogger(__name__)

# Be polite between candidate page fetches
FETCH_DELAY_SECONDS = 1.5
# Be polite between separate search-engine requests
SEARCH_DELAY_SECONDS = 1.0
# Be polite between Bing /ck/a redirect resolutions
BING_RESOLVE_DELAY_SECONDS = 0.5
# How many same-suffix search results to consider as candidates
SEARCH_RESULT_LIMIT = 10
PRICE_SANE_MAX = 1_000_000

_BING_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# Two-part domain suffixes mapped to their country code, so e.g.
# "magazin.com.ro" matches "emag.ro" (both resolve to "ro")
TWO_PART_CC_TLDS = {
    "com.ro": "ro", "net.ro": "ro", "org.ro": "ro",
    "com.de": "de", "com.pl": "pl", "com.ar": "ar", "com.au": "au",
    "com.br": "br", "com.cn": "cn", "com.hk": "hk", "com.mx": "mx",
    "com.my": "my", "com.sg": "sg", "com.tr": "tr", "com.tw": "tw",
    "co.in": "in", "co.jp": "jp", "co.nz": "nz", "co.za": "za",
    "co.uk": "uk", "me.uk": "uk", "net.uk": "uk", "org.uk": "uk",
}

# amazon.{tld} does not use the bare country code for every country
AMAZON_TLDS = {"uk": "co.uk"}

# Price-comparison aggregators: their pages list offers from many stores, so a
# verified link there is a multi-store listing, not a shop to monitor.
AGGREGATOR_HOSTS = {
    "price.ro", "compari.ro", "priceplanet.ro", "oferte.net",
    "idealo.de", "idealo.fr", "idealo.es", "idealo.it", "idealo.nl", "geizhals.de", "billiger.de",
    "comperia.pl", "kupi-tanio.pl",
    "shopzilla.com",
}

_CODE_FIELDS = ("gtin", "gtin13", "gtin12", "gtin14", "upc", "ean", "sku", "mpn")

_ASIN_URL_RE = re.compile(r"/(?:dp|gp/product|product)/([A-Za-z0-9]{10})(?:[/?#]|$)")
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def _max_links() -> int:
    try:
        return max(1, int(os.getenv("ALTERNATE_LINKS_MAX", "3")))
    except (TypeError, ValueError):
        return 3


def _domain_suffix(host: str) -> str:
    """Country-level domain suffix of a host: emag.ro -> ro, magazin.com.ro -> ro."""
    host = (host or "").lower().strip().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if not parts or not parts[0]:
        return ""
    if len(parts) >= 3:
        country = TWO_PART_CC_TLDS.get(".".join(parts[-2:]))
        if country:
            return country
    return parts[-1]


def _is_same_site(candidate_host: str, original_host: str) -> bool:
    """True when both hosts belong to the same store (subdomains count)."""
    c = (candidate_host or "").lower().strip()
    if c.startswith("www."):
        c = c[4:]
    o = (original_host or "").lower().strip().split(":")[0]
    if o.startswith("www."):
        o = o[4:]
    if not c or not o:
        return False
    return c == o or c.endswith("." + o) or o.endswith("." + c)


def _is_aggregator(host: str) -> bool:
    """True when the host is a known price-comparison aggregator (subdomains count)."""
    host = (host or "").lower().strip().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in AGGREGATOR_HOSTS)


def _valid_ean(digits: str) -> bool:
    """Mod-10 check-digit validation for EAN-8/UPC-12/EAN-13 barcodes."""
    if len(digits) not in (8, 12, 13):
        return False
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(reversed(digits)))
    return total % 10 == 0


def _harvest_codes(obj, depth: int = 0) -> List[Tuple[str, str]]:
    """Recursively collect (field, value) product-code pairs from a JSON structure."""
    found: List[Tuple[str, str]] = []
    if depth > 8:
        return found
    if isinstance(obj, dict):
        for field in _CODE_FIELDS:
            value = obj.get(field)
            if isinstance(value, dict):
                value = value.get("@value")
            if value is not None and not isinstance(value, (dict, list)):
                text = str(value).strip()
                if text:
                    found.append((field, text))
        for value in obj.values():
            if isinstance(value, (dict, list)):
                found.extend(_harvest_codes(value, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_harvest_codes(item, depth + 1))
    return found


def _harvest_page_codes(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """Collect (field, value) product-code pairs from JSON-LD, microdata and meta tags."""
    found: List[Tuple[str, str]] = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        found.extend(_harvest_codes(data))

    for field in _CODE_FIELDS:
        for elem in soup.find_all(attrs={"itemprop": field}):
            value = (elem.get("content") or elem.get_text(strip=True) or "").strip()
            if value:
                found.append((field, value))
        for meta_attr in ("name", "property"):
            meta = soup.find("meta", attrs={meta_attr: field})
            if meta and meta.get("content"):
                found.append((field, meta["content"].strip()))

    return found


def _ean_from_url(url: str) -> Optional[str]:
    """Extract a check-digit-validated barcode from the URL itself.

    Many stores embed the EAN in the URL slug (eMAG: .../name-4744131012001/pd/..),
    so a URL is a legitimate source for the code even when the page metadata is
    missing or only carries store-internal IDs.
    """
    for match in re.finditer(r"\d+", unquote(url or "")):
        digits = match.group(0)
        if _valid_ean(digits):
            return digits
    return None


def _extract_product_code(html: str, url: str) -> Optional[Tuple[str, str]]:
    """Return (type, code) for a globally unique product code, else None.

    Only identifiers that denote the same physical product on any store are
    accepted: EAN/UPC/GTIN barcodes (check-digit validated) and Amazon ASINs.
    Store-internal IDs are rejected so we never save an alternate link we are
    not reasonably sure is the same product.

    Sources, in priority order: page metadata (JSON-LD/microdata/meta), then a
    check-digit-validated barcode embedded in the product URL.
    """
    soup = BeautifulSoup(html, "html.parser")

    asin_candidates: List[str] = []
    match = _ASIN_URL_RE.search(url or "")
    if match:
        asin_candidates.append(match.group(1).upper())

    for field, value in _harvest_page_codes(soup):
        cleaned = re.sub(r"\s", "", value).upper()
        digits = re.sub(r"\D", "", cleaned)
        if field in ("gtin", "gtin13", "gtin12", "gtin14", "upc", "ean") and _valid_ean(digits):
            return ("ean", digits)
        if field in ("sku", "mpn"):
            # Some stores file the EAN under "sku"/"mpn" (eMAG uses "mpn" for it);
            # a value that passes the barcode check is a genuine EAN even there.
            if _ASIN_RE.match(cleaned):
                if cleaned not in asin_candidates:
                    asin_candidates.append(cleaned)
            elif _valid_ean(digits):
                return ("ean", digits)

    if asin_candidates:
        return ("asin", asin_candidates[0])

    ean = _ean_from_url(url)
    if ean:
        return ("ean", ean)
    return None


def _resolve_ddg_href(href: str) -> str:
    """DuckDuckGo wraps result links in a redirect URL; extract the real target."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if not parsed.netloc or parsed.scheme not in ("http", "https"):
        return ""
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        query = parse_qs(parsed.query)
        if "uddg" in query:
            return unquote(query["uddg"][0])
        return ""
    return href


def _ddg_urls(page: str, selector: str) -> List[str]:
    """Real result URLs from a DuckDuckGo page (both the html and lite variants
    wrap links in a /l/?uddg= redirect that must be unwrapped)."""
    soup = BeautifulSoup(page, "html.parser")
    urls = []
    for a in soup.select(selector):
        url = _resolve_ddg_href(a.get("href") or "")
        if url:
            urls.append(url)
    return urls


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _bing_rss_urls(code: str) -> List[str]:
    """Bing's RSS output carries plain result URLs (no /ck/a redirect wrapper)."""
    search_url = "https://www.bing.com/search?q=" + quote(f'"{code}"') + "&format=rss"
    page = _fetch_page(search_url)
    urls = []
    for raw in re.findall(r"<link>\s*([^<]+?)\s*</link>", page):
        url = raw.strip()
        if url.startswith("http") and "bing.com" not in _host_of(url):
            urls.append(url)
    return urls


def _resolve_bing_ck(href: str) -> str:
    """Follow a bing.com/ck/a result redirect and return the real target URL."""
    try:
        resp = SESSION.get(href, headers=_BING_HEADERS, timeout=20, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = (resp.headers.get("Location") or "").strip()
            if location.startswith("http"):
                return location
        resp = SESSION.get(href, headers=_BING_HEADERS, timeout=20, allow_redirects=True)
        final = (resp.url or "").strip()
        if final.startswith("http") and "bing.com" not in _host_of(final):
            return final
    except Exception as e:
        logger.debug(f"Could not resolve Bing redirect {href}: {e}")
    return ""


def _bing_html_urls(code: str) -> List[str]:
    """Result URLs from Bing's HTML page; /ck/a redirects are resolved to real URLs."""
    search_url = "https://www.bing.com/search?q=" + quote(f'"{code}"')
    page = _fetch_page(search_url)
    soup = BeautifulSoup(page, "html.parser")
    urls = []
    for a in soup.select("li.b_algo h2 a"):
        href = (a.get("href") or "").strip()
        if not href.startswith("http"):
            continue
        if "bing.com/ck/a" in href:
            time.sleep(BING_RESOLVE_DELAY_SECONDS)
            resolved = _resolve_bing_ck(href)
            if resolved:
                urls.append(resolved)
        elif "bing.com" not in _host_of(href):
            urls.append(href)
    return urls


def _search_engines(code: str):
    """Yield result-URL lists from several search backends, best effort.

    No single engine is reliable from server IPs (DuckDuckGo frequently serves a
    reduced result set to automated clients), so every engine that answers is
    merged in _search_candidates.
    """
    quoted = f'"{code}"'
    try:
        yield _ddg_urls(_fetch_page("https://html.duckduckgo.com/html/?q=" + quote(quoted)), "a.result__a")
    except Exception as e:
        logger.warning(f"DuckDuckGo search failed for {code}: {e}")
    try:
        yield _ddg_urls(_fetch_page("https://lite.duckduckgo.com/lite/?q=" + quote(quoted)), "a.result-link")
    except Exception as e:
        logger.warning(f"DuckDuckGo Lite search failed for {code}: {e}")
    try:
        yield _bing_rss_urls(code)
    except Exception as e:
        logger.warning(f"Bing RSS search failed for {code}: {e}")
    try:
        yield _bing_html_urls(code)
    except Exception as e:
        logger.warning(f"Bing HTML search failed for {code}: {e}")


def _search_candidates(code: str, suffix: str, main_host: str, limit: int = SEARCH_RESULT_LIMIT) -> List[str]:
    """Web-search the exact product code and keep results on the same domain suffix.

    Merges results from several search engines (DuckDuckGo html/lite, Bing RSS/HTML)
    so one flaky or bot-limited engine does not leave the others untried.
    """
    candidates: List[str] = []
    seen = set()
    for index, engine_urls in enumerate(_search_engines(code)):
        if len(candidates) >= limit:
            break
        if index:
            time.sleep(SEARCH_DELAY_SECONDS)
        for url in engine_urls:
            host = _host_of(url)
            if not host or _domain_suffix(host) != suffix or _is_same_site(host, main_host) or _is_aggregator(host):
                continue
            normalized = url.rstrip("/")
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(url)
            if len(candidates) >= limit:
                break

    if not candidates:
        logger.info(f"No same-suffix search results for product code {code} (suffix '{suffix}')")
    return candidates


def _asin_candidates(asin: str, suffix: str) -> List[str]:
    """Same ASIN is the same product on any amazon domain, so build it directly."""
    tld = AMAZON_TLDS.get(suffix, suffix)
    return [f"https://amazon.{tld}/dp/{asin}"]


def _price_url(url: str) -> Optional[float]:
    """Fetch a URL and return a sane price, or None."""
    try:
        html = _fetch_page(url)
    except Exception as e:
        logger.debug(f"Could not fetch {url}: {e}")
        return None
    price = extract_price_auto(html, url)
    if price is None or not (0 < price <= PRICE_SANE_MAX):
        return None
    return price


def _verify_candidate(url: str, code: str) -> Optional[float]:
    """Fetch a candidate page; return the price only if the exact product code is
    present in the URL or the HTML (same-product proof) and a price is extractable."""
    try:
        html = _fetch_page(url)
    except Exception as e:
        logger.debug(f"Could not fetch candidate {url}: {e}")
        return None
    if code not in url and code not in html:
        logger.info(f"Candidate {url} rejected: product code {code} not present on the page")
        return None
    price = extract_price_auto(html, url)
    if price is None or not (0 < price <= PRICE_SANE_MAX):
        logger.info(f"Candidate {url} verified but no usable price found")
        return None
    return price


def find_alternate_links(product_id: int) -> dict:
    """Discover and save alternate links for one product. Returns a JSON-safe summary."""
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            return {"success": False, "message": "Product not found"}

        main_host = _extract_main_domain(product.url)
        suffix = _domain_suffix(main_host)
        existing = _get_alternative_urls(product)

        try:
            original_html = _fetch_page(product.url)
        except Exception as e:
            logger.error(f"Could not fetch original page for {product.name} ({product.url}): {e}")
            return {"success": False, "message": f"Could not fetch the original product page: {e}"}

        code = _extract_product_code(original_html, product.url)
        if not code:
            message = ("No globally unique product code (EAN/GTIN/UPC or ASIN) found on the "
                       "original page - no alternate links added")
            logger.info(f"{product.name}: {message}")
            return {
                "success": True,
                "code": None,
                "domain_suffix": suffix,
                "message": message,
                "saved_alternative_urls": existing,
            }

        code_type, code_value = code
        logger.info(f"{product.name}: found product code {code_type.upper()} {code_value}, searching (suffix '{suffix}')...")

        if code_type == "asin":
            candidates = _asin_candidates(code_value, suffix)
        else:
            candidates = _search_candidates(code_value, suffix, main_host)

        pool: List[Tuple[str, Optional[float]]] = []
        seen_urls = {product.url.rstrip("/")} | {u.rstrip("/") for u in existing}
        for url in candidates:
            if url.rstrip("/") in seen_urls:
                continue
            host = (urlparse(url).netloc or "").lower()
            if not host or _is_same_site(host, main_host) or _is_aggregator(host):
                continue
            seen_urls.add(url.rstrip("/"))
            time.sleep(FETCH_DELAY_SECONDS)
            price = _verify_candidate(url, code_value)
            if price is None:
                continue
            pool.append((url, price))
            logger.info(f"{product.name}: verified candidate {url} at {price} {product.currency}")

        for url in existing:
            if _is_aggregator(_host_of(url)):
                logger.info(f"{product.name}: dropping aggregator link {url}")
                continue
            time.sleep(FETCH_DELAY_SECONDS)
            pool.append((url, _price_url(url)))

        pool.sort(key=lambda item: (item[1] is None, item[1] if item[1] is not None else 0.0))
        final_urls = [url for url, _price in pool[:_max_links()]]

        if final_urls != existing:
            product.alternative_urls = json.dumps(final_urls) if final_urls else None
            db.commit()
            logger.info(f"{product.name}: saved {len(final_urls)} alternate link(s): {final_urls}")
        else:
            logger.info(f"{product.name}: no change to alternate links ({existing})")

        new_count = sum(1 for url, _price in pool if url not in existing)
        return {
            "success": True,
            "code": {"type": code_type, "value": code_value},
            "domain_suffix": suffix,
            "candidates_found": new_count,
            "saved_alternative_urls": final_urls,
            "message": f"Saved {len(final_urls)} alternate link(s) for {code_type.upper()} {code_value}",
        }
    except Exception as e:
        logger.error(f"Alternate-link discovery failed for product {product_id}: {e}")
        return {"success": False, "message": str(e)}
    finally:
        db.close()


def scheduled_alternate_discovery():
    """Scheduled job: discover alternate links for all enabled products that allow it."""
    logger.info("=== Scheduled alternate link discovery started ===")

    db = SessionLocal()
    try:
        products = db.query(Product).filter(
            Product.enabled == True,
            Product.auto_alternate_links == True,
        ).all()
        targets = [(p.id, p.name) for p in products]
    finally:
        db.close()

    logger.info(f"Searching alternate links for {len(targets)} enabled product(s)")

    results = []
    for product_id, name in targets:
        try:
            summary = find_alternate_links(product_id)
            results.append({
                "product_id": product_id,
                "name": name,
                "saved": len(summary.get("saved_alternative_urls") or []),
                "message": summary.get("message"),
            })
        except Exception as e:
            logger.error(f"Alternate-link discovery failed for {name} (id {product_id}): {e}")
            results.append({"product_id": product_id, "name": name, "saved": 0, "message": str(e)})

    logger.info(f"=== Alternate link discovery completed. Results: {results} ===")
