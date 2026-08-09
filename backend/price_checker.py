"""
Price checking service that scrapes product prices from URLs.
Supports multiple strategies for extracting prices.
"""

import re
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup

from database import SessionLocal
from models import Product, PriceEntry

logger = logging.getLogger(__name__)

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
    """
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: Look for JSON-LD structured data (schema.org/Product)
    price = _extract_jsonld_price(soup)
    if price is not None:
        logger.info(f"Extracted price from JSON-LD: {price}")
        return price

    # Strategy 2: Look for meta tags with price info (Open Graph, Facebook)
    price = _extract_meta_price(soup)
    if price is not None:
        logger.info(f"Extracted price from meta tags: {price}")
        return price

    # Strategy 3: Look for common price-related CSS selectors
    price = _extract_selector_price(soup)
    if price is not None:
        logger.info(f"Extracted price from CSS selectors: {price}")
        return price

    # Strategy 4: Look for price in microdata
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


def _extract_jsonld_price(soup: BeautifulSoup) -> Optional[float]:
    """Extract price from JSON-LD structured data."""
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            import json
            data = json.loads(script.string)

            # Handle both single object and array of objects
            if isinstance(data, dict):
                objects = [data]
            elif isinstance(data, list):
                objects = data
            else:
                continue

            for obj in objects:
                # Look for offers with price
                offers = obj.get('offers', {})
                if isinstance(offers, dict) and 'price' in offers:
                    price = _parse_price_string(str(offers['price']))
                    if price:
                        return price
                elif isinstance(offers, list):
                    for offer in offers:
                        if 'price' in offer:
                            price = _parse_price_string(str(offer['price']))
                            if price:
                                return price

                # Look directly at the object for price (if it's a Product)
                if '@type' in obj and 'Product' in str(obj.get('@type', '')):
                    if 'offers' in obj and isinstance(obj['offers'], dict):
                        price = _parse_price_string(str(obj['offers'].get('price')))
                        if price:
                            return price

        except (json.JSONDecodeError, TypeError, KeyError):
            continue

    return None


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


def check_product_price(product_id: int) -> Optional[PriceEntry]:
    """
    Check the price of a product and record it in the database.
    Returns the created PriceEntry or None if price couldn't be extracted.
    """
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            logger.error(f"Product with id {product_id} not found")
            return None

        # Fetch the page
        response = SESSION.get(product.url, timeout=30)
        response.raise_for_status()
        html = response.text

        # Extract price based on scraper type
        if product.scraper_type == "custom" and product.custom_selector:
            soup = BeautifulSoup(html, "html.parser")
            elem = soup.select_one(product.custom_selector)
            if elem:
                price_str = elem.get('data-price') or elem.get('content') or elem.get_text(strip=True)
                price = _parse_price_string(price_str)
            else:
                price = None
        else:
            price = extract_price_auto(html, product.url)

        if price is None:
            logger.warning(f"Could not extract price for {product.name} ({product.url})")
            return None

        # Find the current minimum price for this product
        min_entry = db.query(PriceEntry).filter(
            PriceEntry.product_id == product_id
        ).order_by(PriceEntry.price.asc()).first()

        is_minimum = min_entry is None or price < min_entry.price

        # Create price entry
        entry = PriceEntry(
            product_id=product_id,
            price=price,
            currency=product.currency,
            checked_at=datetime.now(timezone.utc),
            is_minimum=is_minimum,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)

        logger.info(
            f"Recorded price for {product.name}: {price} {product.currency}"
            f"{' (NEW MINIMUM!)' if is_minimum else ''}"
        )

        return entry

    except requests.RequestException as e:
        logger.error(f"Failed to fetch {product.url}: {e}")
        return None
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