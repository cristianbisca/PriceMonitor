"""
Price checking service that scrapes product prices from URLs.
Supports multiple strategies for extracting prices.
"""

import re
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from database import SessionLocal
from models import Product, PriceEntry

logger = logging.getLogger(__name__)


@dataclass
class PriceCheckResult:
    """Plain-data result of a price check.

    Returned by :func:`check_product_price` instead of a live ORM object so callers can
    safely read its fields after the function's internal session has been closed (avoids
    ``DetachedInstanceError`` when accessing attributes on a detached PriceEntry).
    """
    product_id: int
    price: float
    currency: str
    is_minimum: bool
    checked_at: datetime

# Common price patterns found in web pages
PRICE_PATTERNS = [
    # Matches prices like: 1,234.56 or 1234.56 or 1.234,56
    r'[\d]{1,3}(?:[.,]\d{3})*(?:\s*\d{3})*(?:[.,]\d{1,2})?',
]

# Headers to mimic a real browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def extract_price_auto(html: str, url: str) -> Optional[float]:
    """
    Attempt to extract price using multiple strategies.
    Returns the price as a float or None if not found.

    Strategy order is designed for reliability:
    - URL-aware strategies first (embedded JSON matched by product ID from URL)
    - Then data-testid (SPA test hooks that are usually accurate)
    - Then JSON-LD with URL matching to pick the right variant
    - Finally fallback strategies
    """
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: Embedded SSR JSON matched by product ID from URL (most reliable for SPAs like Notino)
    price = _extract_embedded_json_price(html, url)
    if price is not None:
        logger.info(f"Extracted price from embedded JSON: {price}")
        return price

    # Strategy 2: data-testid attributes (SPAs use these for test automation)
    price = _extract_testid_price(soup)
    if price is not None:
        logger.info(f"Extracted price from data-testid: {price}")
        return price

    # Strategy 3: JSON-LD structured data with URL matching (schema.org/Product)
    price = _extract_jsonld_price(soup, url)
    if price is not None:
        logger.info(f"Extracted price from JSON-LD: {price}")
        return price

    # Strategy 4: Meta tags with price info (Open Graph, Facebook)
    price = _extract_meta_price(soup)
    if price is not None:
        logger.info(f"Extracted price from meta tags: {price}")
        return price

    # Strategy 5: Amazon-specific price (priceToPay / corePriceDisplay / a-offscreen).
    # Gated to Amazon hosts so it cannot affect other sites. Placed before the generic
    # selector strategy because Amazon splits its price into sub-spans (.a-price-whole /
    # .a-price-fraction) which would make the generic [class*="price"] match drop decimals.
    if "amazon." in url.lower():
        price = _extract_amazon_price(soup)
        if price is not None:
            logger.info(f"Extracted Amazon price: {price}")
            return price

    # Strategy 6: Common price-related CSS selectors
    price = _extract_selector_price(soup)
    if price is not None:
        logger.info(f"Extracted price from CSS selectors: {price}")
        return price

    # Strategy 7: Price in microdata
    price = _extract_microdata_price(soup)
    if price is not None:
        logger.info(f"Extracted price from microdata: {price}")
        return price

    logger.warning(f"No price found for URL: {url}")
    return None


def _parse_price_string(price_str: str) -> Optional[float]:
    """Parse a price string, handling various formats."""
    if not price_str:
        return None

    # Remove currency symbols and whitespace
    cleaned = re.sub(r'[^\d.,]', '', price_str.strip())

    if not cleaned:
        return None

    # Handle European format (1.234,56) vs US format (1,234.56)
    if ',' in cleaned and '.' in cleaned:
        # Determine which is the decimal separator (last one)
        last_comma = cleaned.rfind(',')
        last_dot = cleaned.rfind('.')
        if last_comma > last_dot:
            # European: 1.234,56
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            # US: 1,234.56
            cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        # Could be decimal (1,99) or thousands (1,000)
        parts = cleaned.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2:
            # Likely decimal separator
            cleaned = cleaned.replace(',', '.')
        else:
            # Likely thousands separator
            cleaned = cleaned.replace(',', '')

    try:
        price = float(cleaned)
        return price if price > 0 else None
    except ValueError:
        return None


def _extract_jsonld_price(soup: BeautifulSoup, url: str = "") -> Optional[float]:
    """Extract price from JSON-LD structured data.
    
    When a URL is provided, attempts to match the Product @id against the URL
    to select the correct variant when multiple products are present.
    """
    # Extract path portion of URL for matching (e.g., /jean-paul-gaultier/divine-...)
    url_path = ""
    if url:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        url_path = parsed.path.rstrip('/')
    
    candidates = []  # list of (matched_bool, price_float)
    
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string)

            # Handle both single object and array of objects
            if isinstance(data, dict):
                objects = [data]
            elif isinstance(data, list):
                objects = data
            else:
                continue

            for obj in objects:
                price = None
                matched_url = False
                
                # Look for offers with price
                offers = obj.get('offers', {})
                if isinstance(offers, dict) and 'price' in offers:
                    price = _parse_price_string(str(offers['price']))
                elif isinstance(offers, list):
                    for offer in offers:
                        if 'price' in offer:
                            price = _parse_price_string(str(offer['price']))
                            if price:
                                break

                # Look directly at the object for price (if it's a Product)
                if '@type' in obj and 'Product' in str(obj.get('@type', '')):
                    if 'price' in obj and price is None:
                        price = _parse_price_string(str(obj['price']))
                    if 'offers' in obj and isinstance(obj['offers'], dict) and price is None:
                        price = _parse_price_string(str(obj['offers'].get('price')))
                    
                    # Check URL matching via @id
                    obj_id = obj.get('@id', '')
                    if obj_id and url_path:
                        # Normalize both for comparison
                        obj_id_normalized = obj_id.rstrip('/')
                        matched_url = (url_path in obj_id_normalized or 
                                       obj_id_normalized in url_path or
                                       _paths_match(obj_id, url))

                if price:
                    candidates.append((matched_url, price))

        except (json.JSONDecodeError, TypeError, KeyError):
            continue

    # Prefer URL-matched candidate; otherwise return first found
    for matched, price in candidates:
        if matched:
            return price
    
    if candidates:
        return candidates[0][1]

    return None


def _paths_match(obj_id: str, page_url: str) -> bool:
    """Check if a JSON-LD @id URL matches the current page URL."""
    from urllib.parse import urlparse
    try:
        id_parsed = urlparse(obj_id)
        url_parsed = urlparse(page_url)
        # Compare paths (normalize trailing slashes and query params)
        id_path = id_parsed.path.rstrip('/')
        url_path = url_parsed.path.rstrip('/')
        return id_path == url_path or id_path in url_path or url_path in id_path
    except Exception:
        return False


def _extract_meta_price(soup: BeautifulSoup) -> Optional[float]:
    """Extract price from meta tags."""
    # Facebook/Open Graph price meta tag
    meta_price = soup.find('meta', property='product:price:amount')
    if meta_price and meta_price.get('content'):
        price = _parse_price_string(meta_price['content'])
        if price:
            return price

    # Schema.org meta tag
    meta_schema = soup.find('meta', attrs={'itemprop': 'price'})
    if meta_schema and meta_schema.get('content'):
        price = _parse_price_string(meta_schema['content'])
        if price:
            return price

    return None


def _extract_testid_price(soup: BeautifulSoup) -> Optional[float]:
    """Extract price from elements with data-testid attributes.
    
    Many modern SPAs (Notino, etc.) use data-testid for test automation
    which conveniently includes price elements like data-testid='pd-price'.
    """
    # Look for common price-related testids
    price_testids = ['pd-price', 'price-variant', 'product-price', 'current-price']
    
    for testid in price_testids:
        elem = soup.find(attrs={'data-testid': testid})
        if elem:
            # Check content attribute first (Notino uses content="625")
            price_str = elem.get('content')
            if price_str:
                price = _parse_price_string(price_str)
                if price:
                    return price
            # Then check text content
            text = elem.get_text(strip=True)
            if text:
                price = _parse_price_string(text)
                if price and price < 100000:
                    return price

    return None


def _extract_embedded_json_price(html: str, url: str) -> Optional[float]:
    """Extract price from embedded SSR JSON data in script tags.

    Sites like Notino embed product data as JSON in the HTML for hydration.
    This looks for patterns like CatalogVariant:{id}.price.value in the payload.
    Handles Apollo state (__APOLLO_STATE__) and Next.js (__NEXT_DATA__) scripts.
    """
    try:
        # Find the productId or variantId from URL to target the right variant
        # e.g., /p-16192772/ -> 16192772
        url_match = re.search(r'p-(\d+)', url)
        product_id_from_url = url_match.group(1) if url_match else None

        # Target specific script IDs that contain SSR state (avoid parsing all scripts)
        targeted_scripts = []

        # Apollo state (Notino, etc.) - can be very large JSON
        for match in re.finditer(r'<script[^>]*id=["\']__APOLLO_STATE__["\'][^>]*>(.*?)</script>', html, re.DOTALL):
            targeted_scripts.append(match.group(1))

        # Next.js data
        for match in re.finditer(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL):
            targeted_scripts.append(match.group(1))

        # If no targeted scripts found, fall back to scanning all script tags with __typename
        if not targeted_scripts:
            for match in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
                text = match.group(1)
                if len(text) >= 50 and '__typename' in text:
                    targeted_scripts.append(text)

        for text in targeted_scripts:
            # Try to parse as JSON directly first
            data = None
            try:
                data = json.loads(text.strip())
            except json.JSONDecodeError:
                pass

            # If direct parse failed, try to extract JSON starting with {
            if data is None:
                json_start = text.find('{')
                if json_start == -1:
                    continue
                # Use json.JSONDecoder for robust parsing of large JSON
                try:
                    decoder = json.JSONDecoder()
                    data, _ = decoder.raw_decode(text[json_start:])
                except (json.JSONDecodeError, ValueError):
                    continue

            if not isinstance(data, dict):
                continue

            # Strategy A: Look for CatalogVariant:{id}.price pattern (Notino)
            if product_id_from_url:
                variant_key = f"CatalogVariant:{product_id_from_url}"
                if variant_key in data:
                    variant_data = data[variant_key]
                    if isinstance(variant_data, dict):
                        price_obj = variant_data.get('price', {})
                        if isinstance(price_obj, dict) and 'value' in price_obj:
                            price = _parse_price_string(str(price_obj['value']))
                            if price:
                                return price

            # Strategy B: Recursive search for price objects with value+currency
            price_found = _search_json_for_price(data)
            if price_found:
                return price_found

    except Exception as e:
        logger.debug(f"Embedded JSON price extraction failed: {e}")

    return None


def _search_json_for_price(obj, depth: int = 0, max_depth: int = 10) -> Optional[float]:
    """Recursively search a parsed JSON object for price patterns.
    
    Looks for objects with 'value' and 'currency' keys (common SSR pattern),
    or direct numeric 'price' fields.
    """
    if depth > max_depth:
        return None
    
    if isinstance(obj, dict):
        # Check for price object pattern: {"__typename":"Price","value":625,"currency":"RON"}
        if 'value' in obj and 'currency' in obj:
            value = _parse_price_string(str(obj['value']))
            if value and value < 100000:
                return value
        
        # Check for direct price key with numeric value
        if 'price' in obj and isinstance(obj.get('price'), (int, float)):
            price = float(obj['price'])
            if 0 < price < 100000:
                return price
        
        # Recurse into children
        for key, val in obj.items():
            result = _search_json_for_price(val, depth + 1, max_depth)
            if result:
                return result
    
    elif isinstance(obj, list):
        for item in obj:
            result = _search_json_for_price(item, depth + 1, max_depth)
            if result:
                return result
    
    return None


def _extract_amazon_price(soup: BeautifulSoup) -> Optional[float]:
    """Extract price from Amazon product pages (amazon.com/.de/.co.uk/etc.).

    Amazon splits its displayed price into sub-spans (.a-price-symbol / .a-price-whole /
    .a-price-fraction), so the generic [class*="price"] selector strategy would grab only
    the whole-number part and drop the decimals. This targets the dedicated "price to pay"
    element and the offscreen (screen-reader) price, which contain the full formatted value.
    """
    # 1. The "price to pay" element - Amazon's canonical current selling price.
    for selector in (".priceToPay", ".apex-pricetopay-value"):
        elem = soup.select_one(selector)
        if elem:
            text = elem.get_text(" ", strip=True)
            price = _parse_price_string(text)
            if price and 0 < price < 100000:
                return price

    # 2. The core price display block (desktop feature div).
    for container_id in ("corePriceDisplay_desktop_feature_div", "corePrice_feature_div"):
        container = soup.find(id=container_id)
        if not container:
            continue
        offscreen = container.select_one(".a-offscreen")
        if offscreen:
            price = _parse_price_string(offscreen.get_text(" ", strip=True))
            if price and 0 < price < 100000:
                return price

    # 3. First screen-reader (offscreen) price that isn't a list/RRP/null value.
    for elem in soup.select(".a-offscreen"):
        text = elem.get_text(" ", strip=True)
        if not text or text.lower() == "null":
            continue
        lowered = text.lower()
        if lowered.startswith(("was:", "rrp:")):
            continue  # skip list/reference prices
        price = _parse_price_string(text)
        if price and 0 < price < 100000:
            return price

    return None


def _extract_selector_price(soup: BeautifulSoup) -> Optional[float]:
    """Extract price using common CSS selectors."""
    # Common price-related selectors
    selectors = [
        '[class*="price"]',
        '[id*="price"]',
        '[itemprop="price"]',
        '.product-price',
        '.current-price',
        '.selling-price',
        '[data-price]',
        '[content*="price"]',
    ]

    for selector in selectors:
        elements = soup.select(selector)
        for elem in elements:
            # Check data attributes first
            price_str = elem.get('data-price') or elem.get('content')
            if price_str:
                price = _parse_price_string(price_str)
                if price:
                    return price

            # Then check text content
            text = elem.get_text(strip=True)
            if text:
                price = _parse_price_string(text)
                if price and price < 100000:  # Sanity check
                    return price

    return None


def _extract_microdata_price(soup: BeautifulSoup) -> Optional[float]:
    """Extract price from HTML microdata."""
    price_elements = soup.find_all(attrs={'itemprop': 'price'})
    for elem in price_elements:
        text = elem.get('content') or elem.get_text(strip=True)
        if text:
            price = _parse_price_string(text)
            if price:
                return price

    return None


def _fetch_page(url: str) -> str:
    """Fetch a page's HTML, preferring browser-impersonated requests.

    Sites like Amazon sit behind Akamai/Cloudflare bot walls that serve a captcha
    interstitial to plain `requests`. curl_cffi replicates a real browser's TLS/JA3
    fingerprint so we receive the actual product HTML instead of the wall. Falls back
    to the plain requests session (original behaviour) if curl_cffi is unavailable or
    every impersonation attempt fails, so existing sites are never made worse off.
    """
    try:
        from curl_cffi import requests as cffi_requests

        for browser in ("chrome124", "chrome120", "safari17_0"):
            try:
                resp = cffi_requests.get(url, impersonate=browser, timeout=30)
                if resp.status_code == 200 and resp.text:
                    return resp.text
            except Exception as e:
                logger.debug(f"curl_cffi {browser} fetch failed for {url}: {e}")
    except ImportError:
        logger.debug("curl_cffi not installed; using plain requests")

    response = SESSION.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def _extract_main_domain(url: str) -> str:
    """Extract the main domain from a URL (e.g., 'https://www.notino.ro/x' -> 'notino.ro')."""
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host or url
    except Exception:
        return url


def _get_alternative_urls(product: Product) -> list:
    """Parse the product's alternative URLs (JSON array stored in a Text column)."""
    if not product.alternative_urls:
        return []
    try:
        parsed = json.loads(product.alternative_urls)
        if isinstance(parsed, list):
            return [u.strip() for u in parsed if isinstance(u, str) and u.strip()]
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Could not parse alternative_urls for product {getattr(product, 'id', '?')}: {e}")
    return []


def _extract_price_for_url(product: Product, url: str) -> Optional[float]:
    """Fetch a single URL and extract the price using the product's scraper configuration."""
    # Fetch the page (browser-impersonated to get past bot walls like Amazon's)
    html = _fetch_page(url)

    if product.scraper_type == "custom" and product.custom_selector:
        soup = BeautifulSoup(html, "html.parser")
        elem = soup.select_one(product.custom_selector)
        if elem:
            price_str = elem.get('data-price') or elem.get('content') or elem.get_text(strip=True)
            return _parse_price_string(price_str)
        return None

    return extract_price_auto(html, url)


def check_product_price(product_id: int) -> Optional[PriceCheckResult]:
    """
    Check the price of a product across all its sources (main URL + alternative URLs)
    and record one PriceEntry per successfully checked source. Each entry is tagged
    with `source` = main domain of the URL it came from (e.g., "notino.ro").

    Returns a :class:`PriceCheckResult` for the first successfully recorded price
    (the main URL when it succeeds, otherwise the first successful alternative), or
    None if no price could be extracted anywhere. The result is plain data (not an ORM
    object) so callers can read its fields safely after this function's session closes.
    """
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            logger.error(f"Product with id {product_id} not found")
            return None

        # Build the list of (url, source_label) pairs to check. The main URL is always
        # first; alternative URLs are deduplicated and must differ from the main URL.
        sources_to_check = [(product.url, _extract_main_domain(product.url))]
        seen = {product.url.rstrip("/")}
        for alt_url in _get_alternative_urls(product):
            normalized = alt_url.rstrip("/")
            if normalized not in seen:
                seen.add(normalized)
                sources_to_check.append((alt_url, _extract_main_domain(alt_url)))

        # Current global minimum price (across all sources) before this check cycle.
        # A new entry is marked as minimum when it beats every previously recorded price.
        min_entry = db.query(PriceEntry).filter(
            PriceEntry.product_id == product_id
        ).order_by(PriceEntry.price.asc()).first()
        current_min = min_entry.price if min_entry else None

        first_result: Optional[PriceCheckResult] = None

        for url, source_label in sources_to_check:
            try:
                price = _extract_price_for_url(product, url)
            except requests.RequestException as e:
                logger.error(f"Failed to fetch {url}: {e}")
                continue

            if price is None:
                logger.warning(f"Could not extract price for {product.name} ({url})")
                continue

            is_minimum = current_min is None or price < current_min
            entry = PriceEntry(
                product_id=product_id,
                price=price,
                currency=product.currency,
                checked_at=datetime.now(timezone.utc),
                is_minimum=is_minimum,
                source=source_label,
            )
            db.add(entry)
            db.commit()
            db.refresh(entry)

            # Capture plain values while the entry is still bound to a live session. A later
            # commit in this loop would expire it again, and reading attributes after the
            # session closes below would raise DetachedInstanceError.
            if first_result is None:
                first_result = PriceCheckResult(
                    product_id=product_id,
                    price=entry.price,
                    currency=entry.currency,
                    is_minimum=entry.is_minimum,
                    checked_at=entry.checked_at,
                )

            if current_min is None or price < current_min:
                current_min = price

            logger.info(
                f"Recorded price for {product.name} [{source_label}]: "
                f"{price} {product.currency}"
                f"{' (NEW MINIMUM!)' if is_minimum else ''}"
            )

        return first_result

    except Exception as e:
        logger.error(f"Error checking price for product {product_id}: {e}")
        return None
    finally:
        db.close()


def run_all_price_checks():
    """Check prices for all enabled products."""
    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.enabled == True).all()
        logger.info(f"Running price checks for {len(products)} enabled products")

        results = []
        for product in products:
            result = check_product_price(product.id)
            results.append({
                "product_id": product.id,
                "name": product.name,
                "success": result is not None,
                "price": result.price if result else None,
                "is_minimum": result.is_minimum if result else False,
            })

        return results

    finally:
        db.close()