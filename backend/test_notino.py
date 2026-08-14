"""Test script to verify improved price detection for Notino.ro"""
import sys
sys.path.insert(0, '.')

from price_checker import (
    extract_price_auto, 
    _extract_jsonld_price, 
    _extract_testid_price, 
    _extract_embedded_json_price,
    _parse_price_string
)
from bs4 import BeautifulSoup
import os

# Load saved HTML
html_path = os.path.join(os.path.dirname(__file__), '..', 'notino_raw.html')
html = open(html_path, encoding='utf-8').read()
url = "https://www.notino.ro/jean-paul-gaultier/divine-eau-de-parfum-pentru-femei/p-16192772/"

soup = BeautifulSoup(html, "html.parser")

print("=" * 60)
print("Testing individual strategies against saved Notino HTML")
print("=" * 60)

# Strategy 1: Embedded SSR JSON (URL-matched by product ID)
price_ssr = _extract_embedded_json_price(html, url)
print(f"Strategy 1 - Embedded SSR JSON:        {price_ssr}")

# Strategy 2: data-testid attributes
price_testid = _extract_testid_price(soup)
print(f"Strategy 2 - data-testid:              {price_testid}")

# Strategy 3: JSON-LD with URL matching
price_jsonld = _extract_jsonld_price(soup, url)
print(f"Strategy 3 - JSON-LD (URL-matched):    {price_jsonld}")

# Full pipeline (should use first successful strategy)
price_pipeline = extract_price_auto(html, url)
print(f"\nFull pipeline result:                  {price_pipeline}")

# Verify expected value
EXPECTED = 625.0
if price_pipeline == EXPECTED:
    print(f"\n✅ SUCCESS: Extracted correct price {price_pipeline} (expected {EXPECTED})")
else:
    print(f"\n❌ FAILURE: Got {price_pipeline}, expected {EXPECTED}")

# Test _parse_price_string edge cases
print("\n" + "=" * 60)
print("Testing _parse_price_string edge cases")
print("=" * 60)
test_cases = [
    ("625", 625.0),
    ("625,00", 625.0),
    ("1.234,56", 1234.56),
    ("1,234.56", 1234.56),
    ("99,99", 99.99),
    ("RON 625", 625.0),
    ("625 RON", 625.0),
]

all_passed = True
for input_str, expected in test_cases:
    result = _parse_price_string(input_str)
    status = "✅" if result == expected else "❌"
    if result != expected:
        all_passed = False
    print(f"  {status} parse('{input_str}') = {result} (expected {expected})")

print("\n" + "=" * 60)
if price_pipeline == EXPECTED and all_passed:
    print("All tests passed!")
else:
    print("Some tests failed.")
print("=" * 60)