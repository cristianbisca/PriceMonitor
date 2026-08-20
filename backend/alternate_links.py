"""
Alternate-link discovery service.

Run either on a schedule (ALTERNATE_LINK_TIMES env var, same pattern as
PRICE_CHECK_TIMES) or manually via the /find-alternates endpoint. For each
product it searches the web for other stores selling the same product, using
up to four matching methods in order of reliability (the first method that
yields at least one candidate cheaper than the current price wins):

  1. "code"    — the product's globally unique code (EAN/UPC/GTIN barcode or
     Amazon ASIN) extracted from the original page; candidates must display
     that exact code on their page.
  2. "model"   — a manufacturer model number (MPN) from the page metadata;
     candidates must display that model number on their page.
  3. "name"    — the product's full name (JSON-LD / Open Graph / <title>),
     searched as an exact phrase; candidates must carry a matching name.
  4. "keyword" — the same product name searched as loose keywords (unquoted);
     the looser query catches stores that phrase the title differently. Every
     result is still verified by the strict name matcher, which rejects
     accessories, a different size/multi-pack, or a different model tier.

All methods are restricted to domains with the same country suffix (e.g. a
.ro original -> only other .ro sites) and never consider the same store or
price-comparison aggregators (price.ro, compari.ro, idealo.de, ...) because
their pages list offers from many stores rather than being a shop to monitor.

Verified candidates that are cheaper than the product's current price are NOT
attached to the product automatically. They are stored in LinkCandidate with
status "pending" (up to ALTERNATE_LINKS_MAX, default 3, per run) and shown in
the UI, where the user approves them (the link is appended to
Product.alternative_urls) or dismisses them (the URL is never suggested again).

Store-internal identifiers (eMAG p-1234567, Notino variant IDs) are deliberately
rejected as "codes": they only identify a product inside one store.
"""

import difflib
import json
import logging
import os
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

from bs4 import BeautifulSoup

from database import SessionLocal
from models import Product, LinkCandidate
from price_checker import (
    SESSION,
    _fetch_page,
    _extract_main_domain,
    _get_alternative_urls,
    extract_price_auto,
    get_current_price,
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
# Reliability of each match method: lower = more reliable. Candidates are ranked
# by (method priority, price) so code matches always beat model matches, which
# beat name matches, which beat loose keyword matches.
METHOD_PRIORITY = {"code": 0, "model": 1, "name": 2, "keyword": 3}
# Cap on candidate pages fetched+verified per method (politeness + runtime).
MAX_VERIFY_PER_METHOD = 6
# How many MPN-style model numbers to try (most stores repeat the same one).
MAX_MODEL_NUMBERS = 2
# MPN-style model numbers are short alphanumeric codes with dashes/dots,
# e.g. "GH-B370", "GSR-18V-150", "X100-PRO".
_MODEL_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-/_\.]{4,39}$")
# Generic words ignored when comparing product names (EN/RO/DE/ES/FR/IT/PT/PL/HU),
# including store branding words so titles like "Name X - Shop Name" clean up well.
NAME_STOPWORDS = {
    "the", "and", "for", "with", "without", "plus", "of", "a", "an", "at", "no", "on", "to", "is", "it", "by", "or",
    "de", "la", "le", "les", "des", "el", "di", "da", "en", "et",
    "original", "autentic", "authentic", "official", "genuin", "genuino",
    "shop", "online", "store", "eshop", "magazin", "marketplace", "ecommerce", "mall",
}
# Words that mark a listing as an accessory for the product rather than the
# product itself (e.g. original "iPhone 15" vs candidate "iPhone 15 Case").
ACCESSORY_WORDS = {
    "case", "cases", "casing", "cover", "covers", "capa", "folie", "funda",
    "hulle", "housse", "etui", "etuis", "hus", "adapter", "adaptor",
    "cable", "cables", "kabel", "charger", "incarcatot", "holder", "stand",
    "mount", "brush", "brushes", "filter", "filters", "replacement",
    "spare", "parts", "zubehoer", "accesories", "accesiorie", "accesorio",
    "accesorios", "protector", "screen", "templet", "kit",
}
# Capacity/quantity tokens (16gb, 2x8gb, 100g, 2l, ...). Used to reject a
# candidate that is a different size of the same product line - a loose
# (keyword) search surfaces these often (a 2x8GB kit for a 16GB single, an
# 8GB listing for a 16GB one).
_CAPACITY_UNITS = r"(?:gb|tb|mb|g|kg|l|ml)"
_KIT_TOKEN_RE = re.compile(r"\d+x\d+" + _CAPACITY_UNITS + r"$")
_CAPACITY_TOKEN_RE = re.compile(r"\d+(?:x\d+)?" + _CAPACITY_UNITS + r"$")
# Model-tier words that mark a different product when the candidate adds them
# (a loose search of "iPhone 15" returns "iPhone 15 Pro Max"). Only the tier the
# candidate introduces is checked, so a product genuinely named "Pro" still
# matches another "Pro" listing. ("plus" is already a name stopword.)
VARIANT_WORDS = {"pro", "max", "ultra", "se", "mini", "lite"}

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

_CODE_FIELDS = ("gtin", "gtin13", "gtin12", "gtin14", "upc", "ean", "sku", "mpn", "model")

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


def _bing_rss_urls(code: str, quoted: bool = True) -> List[str]:
    """Bing's RSS output carries plain result URLs (no /ck/a redirect wrapper).

    quoted=True wraps the query in double quotes (exact phrase); quoted=False does
    a plain keyword search (looser, used by the "keyword" method).
    """
    q = f'"{code}"' if quoted else code
    search_url = "https://www.bing.com/search?q=" + quote(q) + "&format=rss"
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


def _bing_html_urls(code: str, quoted: bool = True) -> List[str]:
    """Result URLs from Bing's HTML page; /ck/a redirects are resolved to real URLs.

    quoted=True wraps the query in double quotes (exact phrase); quoted=False does
    a plain keyword search (looser, used by the "keyword" method).
    """
    q = f'"{code}"' if quoted else code
    search_url = "https://www.bing.com/search?q=" + quote(q)
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


def _search_engines(query: str, quoted: bool = True):
    """Yield result-URL lists from several search backends, best effort.

    No single engine is reliable from server IPs (DuckDuckGo frequently serves a
    reduced result set to automated clients), so every engine that answers is
    merged in _search_candidates. quoted=True wraps the query in double quotes
    (exact phrase); quoted=False does a plain keyword search.
    """
    q = f'"{query}"' if quoted else query
    try:
        yield _ddg_urls(_fetch_page("https://html.duckduckgo.com/html/?q=" + quote(q)), "a.result__a")
    except Exception as e:
        logger.warning(f"DuckDuckGo search failed for {query}: {e}")
    try:
        yield _ddg_urls(_fetch_page("https://lite.duckduckgo.com/lite/?q=" + quote(q)), "a.result-link")
    except Exception as e:
        logger.warning(f"DuckDuckGo Lite search failed for {query}: {e}")
    try:
        yield _bing_rss_urls(query, quoted)
    except Exception as e:
        logger.warning(f"Bing RSS search failed for {query}: {e}")
    try:
        yield _bing_html_urls(query, quoted)
    except Exception as e:
        logger.warning(f"Bing HTML search failed for {query}: {e}")


def _search_candidates(query: str, suffix: str, main_host: str, limit: int = SEARCH_RESULT_LIMIT,
                       quoted: bool = True) -> List[str]:
    """Web-search the query and keep results on the same domain suffix.

    Merges results from several search engines (DuckDuckGo html/lite, Bing RSS/HTML)
    so one flaky or bot-limited engine does not leave the others untried.
    quoted=True searches the exact phrase; quoted=False does a plain keyword search.
    """
    candidates: List[str] = []
    seen = set()
    for index, engine_urls in enumerate(_search_engines(query, quoted)):
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
        logger.info(f"No same-suffix search results for '{query}' (suffix '{suffix}')")
    return candidates


def _asin_candidates(asin: str, suffix: str) -> List[str]:
    """Same ASIN is the same product on any amazon domain, so build it directly."""
    tld = AMAZON_TLDS.get(suffix, suffix)
    return [f"https://amazon.{tld}/dp/{asin}"]


def _verify_candidate(url: str, code: str, case_insensitive: bool = False) -> Optional[float]:
    """Fetch a candidate page; return the price only if the exact product code is
    present in the URL or the HTML (same-product proof) and a price is extractable.

    case_insensitive is used for MPN-style model numbers, which are uppercase in
    metadata but may appear in any casing on the page.
    """
    try:
        html = _fetch_page(url)
    except Exception as e:
        logger.debug(f"Could not fetch candidate {url}: {e}")
        return None
    if case_insensitive:
        present = code.lower() in url.lower() or code.lower() in (html or "").lower()
    else:
        present = code in url or code in (html or "")
    if not present:
        logger.info(f"Candidate {url} rejected: product code {code} not present on the page")
        return None
    price = extract_price_auto(html, url)
    if price is None or not (0 < price <= PRICE_SANE_MAX):
        logger.info(f"Candidate {url} verified but no usable price found")
        return None
    return price


def _iter_jsonld_objects(data):
    """Yield dict objects from parsed JSON-LD, unwrapping @graph, lists and ItemGraph."""
    def _walk(obj, depth=0):
        if depth > 6:
            return
        if isinstance(obj, dict):
            yield obj
            for nested in obj.values():
                if isinstance(nested, (dict, list)):
                    yield from _walk(nested, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                yield from _walk(item, depth + 1)

    yield from _walk(data)


def _clean_title(raw: str) -> str:
    """Strip store branding from a page title, e.g. 'Name X - Store Name',
    'Store: Name X' or 'Store | Name X'. Keeps the part with the most tokens
    unique to it (store names repeat the brand, product parts carry the model)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    # " - " and " | " (spaces both sides) or ":" (branding prefix) or " » "/" > "
    parts = [p.strip() for p in re.split(r"\s+-\s+|:\s+|\s+[\|>»]\s+", raw) if p.strip()]
    if len(parts) <= 1:
        return parts[0] if parts else raw
    shared = set.intersection(*[set(_name_tokens(p)) for p in parts])

    def uniqueness(part: str, index: int) -> tuple:
        return (len(set(_name_tokens(part)) - shared), -index)

    return max(enumerate(parts), key=lambda item: uniqueness(item[1], item[0]))[1]


def _name_tokens(name: str) -> List[str]:
    raw = re.sub(r"[^a-z0-9]+", " ", str(name).lower()).split()
    return [t for t in raw if len(t) >= 2 and t not in NAME_STOPWORDS]


def _kit_tokens(name: str) -> set:
    """Multi-pack tokens such as '2x8gb' - a pack of separate units, not a single size."""
    return {t for t in _name_tokens(name) if _KIT_TOKEN_RE.fullmatch(t)}


def _capacity_tokens(name: str) -> set:
    """Size tokens such as '16gb', '100g', '2l' (multi-packs like '2x8gb' count too)."""
    return {t for t in _name_tokens(name) if _CAPACITY_TOKEN_RE.fullmatch(t)}


def _same_capacity(original: str, candidate: str) -> bool:
    """True when the candidate is not a different size/multi-pack of the product.

    A candidate that adds a multi-pack the original doesn't carry (a 2x8GB kit for
    a 16GB single) is rejected. The original's size must also appear in the
    candidate, so a 16GB original never matches a bare 8GB listing. Products with
    no size token (most electronics) are left to the token-overlap gate.
    """
    o_cap, c_cap = _capacity_tokens(original), _capacity_tokens(candidate)
    o_kit, c_kit = _kit_tokens(original), _kit_tokens(candidate)
    if c_kit - o_kit:
        return False
    if o_cap and not (o_cap & c_cap):
        return False
    return True


def _names_match(original: str, candidate: str) -> bool:
    """Conservative product-name similarity check for the "name"/"keyword" methods.

    Passes when either >=60% of the original's meaningful tokens are kept, or the
    compacted strings (letters/digits only) are at least 70% similar - the second
    gate catches model numbers that split differently ('GSR 18V-150' vs 'GSR18V-150').
    Candidates that add accessory vocabulary are rejected ('iPhone 15' vs
    'iPhone 15 Case'), as are ones that are a different size/multi-pack
    ('16GB' single vs '16GB (2x8GB)' kit, '16GB' vs '8GB') or a different model
    tier the candidate introduces ('iPhone 15' vs 'iPhone 15 Pro Max').
    """
    o_tokens = set(_name_tokens(original))
    c_tokens = set(_name_tokens(candidate))
    if len(o_tokens) < 2 or not c_tokens:
        return False
    inter = o_tokens & c_tokens
    if not inter:
        return False
    ratio = difflib.SequenceMatcher(
        None, re.sub(r"[^a-z0-9]", "", original.lower()), re.sub(r"[^a-z0-9]", "", candidate.lower())
    ).ratio()
    if len(inter) / len(o_tokens) < 0.6 and ratio < 0.70:
        return False
    if (c_tokens - o_tokens) & ACCESSORY_WORDS and not (o_tokens & ACCESSORY_WORDS):
        return False
    if (c_tokens - o_tokens) & VARIANT_WORDS:
        return False
    if not _same_capacity(original, candidate):
        return False
    return True


def _extract_product_name(soup: BeautifulSoup) -> Optional[str]:
    """The product's display name from JSON-LD first, then og:title, then <title>."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        for obj in _iter_jsonld_objects(data):
            obj_type = str(obj.get("@type", "")).lower() if isinstance(obj, dict) else ""
            if "product" in obj_type:
                name = obj.get("name")
                if isinstance(name, dict):  # multilingual {"en": "..."}
                    name = next((v for v in name.values() if isinstance(v, str)), None)
                if isinstance(name, str) and name.strip():
                    return name.strip()
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content") and meta["content"].strip():
        return _clean_title(meta["content"])
    if soup.title and soup.title.string:
        return _clean_title(soup.title.string)
    return None


def _extract_model_names(soup: BeautifulSoup) -> List[str]:
    """Manufacturer model numbers (MPN) that are not barcodes or ASINs.

    Values from sku/mpn/model metadata are kept only when they look like a real
    model code (short, alphanumeric with separators, at least one digit) — store
    internal IDs and bare barcodes are rejected, exactly like in the code method.
    """
    models: List[str] = []
    for field, value in _harvest_page_codes(soup):
        if field not in ("sku", "mpn", "model"):
            continue
        cleaned = re.sub(r"\s+", "", str(value)).upper()
        digits = re.sub(r"\D", "", cleaned)
        if _valid_ean(digits) or _ASIN_RE.match(cleaned):
            continue  # barcodes/ASINs are handled by the "code" method
        # Real MPNs contain at least one letter; purely numeric values are store-
        # internal IDs or malformed barcodes and are too generic to match on.
        if not _MODEL_RE.match(cleaned) or len(cleaned) < 5 or not digits or not any(ch.isalpha() for ch in cleaned):
            continue
        if models and all(cleaned.startswith(m) or m.startswith(cleaned) for m in models):
            continue
        if len(models) >= MAX_MODEL_NUMBERS:
            break
        models.append(cleaned)
    return models


def _verify_name_candidate(url: str, original_name: str) -> Optional[float]:
    """Fetch a candidate page; return the price only if the page carries a matching
    product name and a price is extractable."""
    try:
        html = _fetch_page(url)
    except Exception as e:
        logger.debug(f"Could not fetch candidate {url}: {e}")
        return None
    page_name = _extract_product_name(BeautifulSoup(html, "html.parser"))
    if not page_name or not _names_match(original_name, page_name):
        logger.info(f"Candidate {url} rejected: page name '{page_name}' does not match '{original_name}'")
        return None
    price = extract_price_auto(html, url)
    if price is None or not (0 < price <= PRICE_SANE_MAX):
        logger.info(f"Candidate {url} verified but no usable price found")
        return None
    return price


def _save_candidates(db, product: Product, pool: dict, current_price: Optional[float]):
    """Upsert this run's verified candidates as pending links.

    pool maps normalized URL -> [price, method]. Only candidates cheaper than the
    current price are stored (all verified ones when no price was ever recorded),
    ranked by (match-method reliability, price), capped at ALTERNATE_LINKS_MAX.
    Already dismissed URLs are never resuggested; pending rows get a price refresh.
    Returns (new_count, pending_count).
    """
    max_links = _max_links()
    ranked = sorted(
        pool.items(),
        key=lambda kv: (METHOD_PRIORITY.get(kv[1][1], 99), kv[1][0] if kv[1][0] is not None else 1e9),
    )
    to_store = []
    for url, (price, method) in ranked:
        if current_price is not None and (price is None or price >= current_price):
            continue
        to_store.append((url, price, method))
        if len(to_store) >= max_links:
            break

    dismissed_urls = {
        row.url for row in db.query(LinkCandidate)
        .filter(LinkCandidate.product_id == product.id, LinkCandidate.status == "dismissed")
        .all()
    }

    new_count = 0
    for url, price, method in to_store:
        if url in dismissed_urls:
            continue
        row = db.query(LinkCandidate).filter(
            LinkCandidate.product_id == product.id, LinkCandidate.url == url
        ).first()
        if row is None:
            db.add(LinkCandidate(product_id=product.id, url=url, price=price, match_method=method, status="pending"))
            new_count += 1
            logger.info(f"{product.name}: new candidate [{method}] {url} at {price}")
        elif row.status == "pending":
            row.price = price
            if row.match_method is None or METHOD_PRIORITY.get(method, 99) < METHOD_PRIORITY.get(row.match_method, 99):
                row.match_method = method
    db.commit()

    pending_count = db.query(LinkCandidate).filter(
        LinkCandidate.product_id == product.id, LinkCandidate.status == "pending"
    ).count()
    return new_count, pending_count


def find_alternate_links(product_id: int) -> dict:
    """Discover alternate links for one product and store them as pending candidates.

    Matches other-store pages by (1) EAN/UPC/GTIN/ASIN, (2) MPN-style model number,
    (3) product name — first method that finds a cheaper candidate wins. Candidates
    are cheaper than the current price, capped at ALTERNATE_LINKS_MAX, and only
    become real alternate links when the user approves them in the UI.
    Returns a JSON-safe summary.
    """
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            return {"success": False, "message": "Product not found"}

        main_host = _extract_main_domain(product.url)
        suffix = _domain_suffix(main_host)

        try:
            original_html = _fetch_page(product.url)
        except Exception as e:
            logger.error(f"Could not fetch original page for {product.name} ({product.url}): {e}")
            return {"success": False, "message": f"Could not fetch the original product page: {e}"}

        current_price = get_current_price(db, product_id)
        original_soup = BeautifulSoup(original_html, "html.parser")

        # URLs never considered again: the main URL, current alternate links
        # (including ones added through approved candidates) and URLs the user
        # dismissed. Pending candidates are intentionally NOT excluded - they are
        # re-verified on every run to refresh their prices.
        excluded_urls = {product.url.rstrip("/")} | {u.rstrip("/") for u in _get_alternative_urls(product)}
        excluded_urls |= {
            row.url for row in db.query(LinkCandidate)
            .filter(LinkCandidate.product_id == product.id, LinkCandidate.status == "dismissed")
            .all()
        }

        pool: dict = {}  # normalized URL -> [price, method] (keeps the most reliable method)

        def consider(url: str, price: float, method: str):
            host = _host_of(url)
            if not host or _is_same_site(host, main_host) or _is_aggregator(host):
                return
            norm = url.rstrip("/")
            if norm in excluded_urls:
                return
            if norm not in pool:
                pool[norm] = [price, method]
                return
            if METHOD_PRIORITY.get(method, 99) < METHOD_PRIORITY.get(pool[norm][1], 99):
                pool[norm][1] = method
            if pool[norm][0] is None or price < pool[norm][0]:
                pool[norm][0] = price

        def has_promising() -> bool:
            """A candidate is worth presenting when it beats the current price
            (any verified one when the product has no price recorded yet)."""
            return any(
                current_price is None or (p is not None and p < current_price)
                for p, _m in pool.values()
            )

        def probe(method: str, query: str, verify_fn, cap: int = MAX_VERIFY_PER_METHOD,
                  quoted: bool = True) -> None:
            """Web-search the query, then fetch+verify same-suffix candidate
            pages until a promising candidate is found, candidates run out or the
            per-method fetch cap is reached. quoted=False does a looser keyword
            search (used by the "keyword" method)."""
            logger.info(f"{product.name}: [{method}] searching {query!r} (suffix '{suffix}')...")
            verified = 0
            for url in _search_candidates(query, suffix, main_host, quoted=quoted):
                if has_promising() or verified >= cap:
                    break
                if url.rstrip("/") in pool or url.rstrip("/") in excluded_urls:
                    continue
                time.sleep(FETCH_DELAY_SECONDS)
                price = verify_fn(url)
                if price is None:
                    continue
                verified += 1
                consider(url, price, method)
                logger.info(f"{product.name}: [{method}] verified candidate {url} at {price} {product.currency}")

        # ── Method 1: globally unique code (EAN/UPC/GTIN/ASIN) ──
        code = _extract_product_code(original_html, product.url)
        if not code:
            logger.info(f"{product.name}: no globally unique code (EAN/UPC/GTIN/ASIN) on the original page")
        else:
            code_type, code_value = code
            logger.info(f"{product.name}: found product code {code_type.upper()} {code_value}")
            if code_type == "asin":
                # Same ASIN is the same product on any amazon domain
                for url in _asin_candidates(code_value, suffix):
                    if has_promising():
                        break
                    if url.rstrip("/") in pool or url.rstrip("/") in excluded_urls:
                        continue
                    time.sleep(FETCH_DELAY_SECONDS)
                    price = _verify_candidate(url, code_value)
                    if price is None:
                        continue
                    consider(url, price, "code")
            else:
                probe("code", code_value, lambda url: _verify_candidate(url, code_value))

        # ── Method 2: manufacturer model number (MPN) ──
        if not has_promising():
            for model_number in _extract_model_names(original_soup):
                if has_promising():
                    break
                probe("model", model_number,
                      lambda url, mn=model_number: _verify_candidate(url, mn, case_insensitive=True))

        # ── Method 3: product name (exact phrase) ──
        # ── Method 4: product name (loose keyword/title search) ──
        if not has_promising():
            product_name = _extract_product_name(original_soup)
            if not product_name:
                logger.info(f"{product.name}: no product name found on the original page")
            else:
                probe("name", product_name, lambda url: _verify_name_candidate(url, product_name))
                if not has_promising():
                    # An unquoted search of the same title catches stores that phrase
                    # the product slightly differently (add a colour, the model code,
                    # reorder specs). Results are still gated by the strict name
                    # matcher, so different sizes/kits/tiers stay rejected.
                    probe("keyword", product_name,
                          lambda url: _verify_name_candidate(url, product_name), quoted=False)

        new_count, pending_count = _save_candidates(db, product, pool, current_price)

        if new_count:
            message = f"Found {new_count} new candidate link(s) - review them on the product page"
        elif pending_count:
            message = f"No new cheaper links found ({pending_count} candidate(s) still pending review)"
        else:
            message = "No cheaper candidate links found"
        logger.info(f"{product.name}: {message}")
        return {
            "success": True,
            "code": {"type": code[0], "value": code[1]} if code else None,
            "domain_suffix": suffix,
            "current_price": current_price,
            "candidates_found": new_count,
            "pending_candidates": pending_count,
            "message": message,
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
                "new_candidates": summary.get("candidates_found", 0),
                "pending": summary.get("pending_candidates", 0),
                "message": summary.get("message"),
            })
        except Exception as e:
            logger.error(f"Alternate-link discovery failed for {name} (id {product_id}): {e}")
            results.append({"product_id": product_id, "name": name, "new_candidates": 0, "message": str(e)})

    logger.info(f"=== Alternate link discovery completed. Results: {results} ===")
