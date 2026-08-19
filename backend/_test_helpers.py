"""Offline unit tests for the new alternate_links matching helpers (no network)."""
from bs4 import BeautifulSoup
from alternate_links import (
    _names_match, _clean_title, _extract_model_names,
    _extract_product_name, _extract_product_code,
)

# ── name matching ──
assert _names_match('Bosch GSR 18V-150 Akku-Bohrmaschine', 'Bosch GSR18V-150 Akku-Schrauber'), "compact-ratio case"
assert _names_match('iPhone 15 256GB', 'Apple iPhone 15 256GB Natural Titanium'), "superset case"
assert not _names_match('iPhone 15', 'iPhone 15 Case Silicone Cover'), "accessory case"
assert not _names_match('iPhone 15', 'Samsung Galaxy S25 Ultra 512GB'), "different product"
assert not _names_match('short', 'totally different unrelated product name here'), "token-count case"
assert _names_match('Nescafe Gold 100g', 'Nescafe Gold Instant Coffee 100g'), "extra word ok"

# ── title cleaning ──
assert _clean_title('Bosch GSR 18V-150 - Bosch Online Shop') == 'Bosch GSR 18V-150'
assert _clean_title('Shop Name: Bosch GSR 18V-150') == 'Bosch GSR 18V-150'
assert _clean_title('Just A Single Title') == 'Just A Single Title'
assert _clean_title('Cafea Arabica 1kg | Coffee Shop') == 'Cafea Arabica 1kg'

# ── name + model extraction from a page ──
html = """
<html><head>
<title>Cafea Arabica 1kg | Coffee Shop</title>
<meta property="og:title" content="Cafea Arabica 1kg"/>
<script type="application/ld+json">{
  "@context": "https://schema.org", "@type": "Product",
  "name": "Gigabyte B370M Gaming 3 (rev. 1.0)",
  "sku": "GH-B370MG3", "mpn": "4744131012001", "model": "B370MG3"
}</script>
</head><body></html>
"""
soup = BeautifulSoup(html, 'html.parser')
assert _extract_product_name(soup) == 'Gigabyte B370M Gaming 3 (rev. 1.0)', _extract_product_name(soup)

models = _extract_model_names(soup)
# GH-B370MG3 and B370MG3 are model numbers; valid EAN 4744131012001 -> not a model
assert models == ['GH-B370MG3', 'B370MG3'], models

html2 = ('<html><head><title>Cafea Arabica 1kg | Coffee Shop</title>'
         '<meta property="og:title" content="Cafea Arabica 1kg"/></head></html>')
assert _extract_product_name(BeautifulSoup(html2, 'html.parser')) == 'Cafea Arabica 1kg'

html3 = '<html><head><title>Simple Product Name Here</title></head></html>'
assert _extract_product_name(BeautifulSoup(html3, 'html.parser')) == 'Simple Product Name Here'

# ── code method unaffected by the new "model" field ──
code = _extract_product_code(html, 'https://shop.ro/product')
assert code == ('ean', '4744131012001'), code
code2 = _extract_product_code('<html></html>', 'https://emag.ro/bosch-4744131012001/pd/123/')
assert code2 == ('ean', '4744131012001'), code2
code3 = _extract_product_code('<html><body><span itemprop="mpn">X100-PRO</span></body></html>', 'https://shop.ro/x')
assert code3 is None, code3  # store-internal-style ID still rejected as code

# model-only metadata -> no code, but model names extracted
html4 = '<html><head><script type="application/ld+json">' \
        '{"@type": "Product", "name": "ASUS ROG X", "sku": "X100-PRO"}</script></head><body></body></html>'
assert _extract_product_code(html4, 'https://shop.ro/y') is None
soup4 = BeautifulSoup(html4, 'html.parser')
assert _extract_model_names(soup4) == ['X100-PRO']

print('ALL HELPER TESTS PASSED')
