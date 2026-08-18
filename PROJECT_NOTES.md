# PriceMonitor — Project Notes

> Reference documentation for AI-assisted development. Captures the architecture,
> data model, key flows, and non-obvious implementation details so future
> conversations can pick up context quickly. Keep this file updated as the codebase evolves.

## 1. What It Is

A **multi-user web application** that monitors e-commerce product prices. It:
- Checks prices on a schedule (default 09:00 & 14:00, TZ-aware)
- Records full price history per product
- Tracks all-time minimums
- **Current price = the cheapest price across all sources at the time of the last check** (the minimum of one check run, which records one entry per source). **Minimum price = all-time minimum** (lowest of every price ever recorded). Notifications are based on the current-price logic.
- Supports **alternative price sources** per product (extra URLs checked alongside the main one; each recorded entry is tagged with its source domain and plotted as a separate colored line on the graph)
- **Auto-discovers alternative links**: finds the product's globally unique code (EAN/GTIN/UPC or ASIN), web-searches other stores in the same country domain suffix, verifies the code on each candidate page, and keeps the cheapest confirmed links (see §7)
- Sends **Telegram** notifications on price drops / new minimums / first check
- Serves a dark-themed PWA dashboard with Chart.js price graphs

## 2. Tech Stack

| Layer | Technology |
|-------|-----------|
| Web framework | FastAPI + Uvicorn (Python 3.12) |
| ORM / DB | SQLAlchemy 2.0 — SQLite by default (`DATABASE_URL`), PostgreSQL-capable |
| Scheduling | APScheduler (BackgroundScheduler, cron triggers) |
| Scraping | `requests` + `curl_cffi` (browser TLS/JA3 impersonation) + BeautifulSoup4 |
| Charts | matplotlib (Agg backend) → base64 PNG; frontend uses Chart.js + Luxon |
| Frontend | Single-file SPA: `backend/static/index.html` (vanilla JS, no build step) |
| Notifications | Telegram Bot API via `requests` (python-telegram-bot is in deps but the notifier uses raw HTTP) |
| Packaging | Single-stage Dockerfile (`python:3.12-slim`), docker-compose (host network), Portainer-ready |

## 3. File Map

```
PriceMonitor/
├── backend/
│   ├── main.py              # Thin uvicorn launcher: reads HOST/PORT/RELOAD, runs "api:app"
│   ├── api.py               # THE FastAPI app: app instance, middleware, startup/shutdown, ALL endpoints
│   ├── auth.py              # Token auth: base64-JSON tokens, SHA-256 passwords, AuthMiddleware
│   ├── database.py          # SQLAlchemy engine/session; DATABASE_URL env; SQLite check_same_thread=False
│   ├── models.py            # ORM models: User, Product, PriceEntry
│   ├── price_checker.py     # Page fetch + 7-tier price extraction strategy (multi-source aware)
│   ├── alternate_links.py   # Alternate-link discovery (EAN/GTIN/UPC/ASIN matching, multi-engine web search)
│   ├── scheduler.py         # APScheduler cron jobs; per-product notification dispatch; alternate-link schedule; backup schedule
│   ├── backup.py            # Dropbox backup/restore: token refresh, snapshots, uploads, retention, boot-restore
│   ├── telegram_notifier.py # Telegram Bot API message senders (per-user chat IDs)
│   ├── graph_generator.py   # matplotlib price history / comparison charts → base64 PNG
│   ├── requirements.txt
│   └── static/
│       ├── index.html       # The entire frontend SPA
│       ├── manifest.json    # PWA manifest
│       └── icon-*.png       # PWA icons (16/32/192/512)
├── Dockerfile               # python:3.12-slim, installs matplotlib system deps, EXPOSE 8000
├── docker-compose.yml       # Port 13020→3000 mapping, pricemonitor_data volume
├── .env.example             # Env var template
└── README.md                # User-facing docs (see §10 for known drift)
```

## 4. Data Model

```
User (id, username, password_hash, created_at,
      telegram_chat_id, telegram_notifications_enabled)
  └── Product (id, user_id, name, url, price_field, currency, enabled,
               created_at, updated_at, scraper_type, custom_selector,
               alternative_urls, auto_alternate_links)
         └── PriceEntry (id, product_id, price, currency, checked_at,
                         is_minimum, source, check_cycle, raw_html)

TelegramNotification (id, product_id, entry_id, chat_id, message_type,
                      sent_at, message_text)          # DEAD CODE — defined, never used
AppSettings (id, key, value)                          # DEAD CODE — defined, never used
```

- **`User`** — owns products; stores per-user Telegram settings (chat ID + enable toggle).
- **`Product`** — `scraper_type` is `"auto"` (default) or `"custom"`; `custom_selector` holds a user CSS selector when `scraper_type == "custom"`. `enabled` gates scheduled checks. `price_field` is a legacy hint column (unused by the current extraction logic). URL is unique per user (`uq_user_url`). `alternative_urls` stores extra product URLs as a **JSON array of strings** in a `Text` column (e.g. `'["https://emag.ro/...", "https://amazon.de/..."]'`); `None` when empty. The API layer parses it to/from a list (`_parse_alternative_urls`) and validates entries on create/update (must be http(s), no duplicates of the main URL or each other, trailing-slash-insensitive).
- **`Product.auto_alternate_links`** — per-product toggle (default `True`): allows the scheduled alternate-link discovery to read/write this product's `alternative_urls`. Toggled by the "Auto find alternate links" checkbox in the add/edit forms (checked by default).
- **`PriceEntry`** — one row per check **per source**. `is_minimum` = True when this price is lower than every previously recorded price for the product. `source` holds the main domain of the URL the price came from (e.g. `"notino.ro"`, via `_extract_main_domain()`); NULL on legacy rows predating the column — the API falls back to the product's main domain when building chart data. `check_cycle` is the **grouping key for a check run**: every source recorded in a single `check_product_price()` call shares one `check_cycle` UTC timestamp (and the same `checked_at`). **Current price = `MIN(price)` of the latest `check_cycle`** (cheapest source at the last check); **minimum price = `MIN(price)` over all rows** (all-time). NULL `check_cycle` on legacy rows predating the column — the API/notification logic falls back (see §6, §8). `checked_at` stored as UTC. `raw_html` is a `Text` column (comment says "for debugging/retries") but is **never populated** — `check_product_price()` creates the entry without it. Dead column.
- **`TelegramNotification`** — defined to track sent notifications (avoid duplicates) but **never referenced** anywhere in the codebase. Dead code.
- **`AppSettings`** — defined as a key/value store but **never referenced** anywhere in the codebase. Dead code.

## 5. Authentication

- **Tokens:** base64-encoded JSON `{user_id, username, ts}`. TTL = 7 days (`TOKEN_TTL_SECONDS = 604800`).
- **Password hashing:** SHA-256 hex digest (no salt — see §10).
- **Transport:** `X-PM-Token` header (primary) or `Authorization: Bearer <token>`.
- **Middleware:** `AuthMiddleware` (Starlette `BaseHTTPMiddleware`) protects all `/api/*` except:
  - `PUBLIC_PATHS`: `/api/health`, `/api/config/timezone`, `/api/auth/status`, `/api/auth/login`, `/api/auth/logout`, `/api/auth/register`
  - `/static/*` (login page assets)
  - `/` and non-`/api/` paths (SPA routes)
- On success, user info is attached to `request.state.user`.
- **Sign-up control:** `ENABLE_SIGNUP` env var (`true`/`false`) gates `/api/auth/register`.
- **Frontend:** token persisted in `localStorage`; auth overlay with login/register tabs; `validateToken()` pings `/api/health` with the token.

## 6. Price Extraction (the core logic)

`price_checker.py` → `check_product_price(product_id)`:
1. Load product; build the source list: main URL first, then each alternative URL from `_get_alternative_urls()` (JSON array parsed defensively — malformed JSON logs a warning and yields `[]`). Duplicates are dropped on trailing-slash-normalized URLs. Each source is labeled with its main domain (`_extract_main_domain`, strips `www.`).
2. For each source: fetch HTML via `_fetch_page()`. If `scraper_type == "custom"` and `custom_selector` set → use that selector; otherwise → `extract_price_auto(html, url)`. A failing/price-less source is skipped (logged), never aborting the other sources.
3. Compute `is_minimum` against the running global minimum across all sources seen so far this cycle (and everything recorded before it). Note: in a first-ever check cycle both the baseline entry and any lower subsequent entry are flagged — same semantics as the original single-source design.
4. Insert one `PriceEntry` per successful source (tagged with its `source` domain), commit, and return a **plain `PriceCheckResult` dataclass** (not the ORM object — avoids `DetachedInstanceError` once the function's session closes).

All entries from one run share a single `check_cycle` timestamp (computed once at the start of the call, also used as `checked_at`), which groups the run into a "check cycle". The returned result describes the cycle: `price` = the **cycle minimum** (cheapest successful source = the product's current price after the check), `source` = the source that had that cheapest price, `is_minimum` = True when the cycle minimum is a new all-time minimum, `checked_at` = the cycle timestamp.

### `_fetch_page()` — bot-wall evasion
Tries `curl_cffi` with browser impersonation (`chrome124`, `chrome120`, `safari17_0`) to get past
Akamai/Cloudflare walls (e.g. Amazon). Falls back to a plain `requests.Session` (with browser-like
headers) if curl_cffi is unavailable or all impersonations fail.

### `extract_price_auto()` — strategy priority (most → least reliable)
1. **Embedded SSR JSON** (`_extract_embedded_json_price`) — parses `__APOLLO_STATE__` / `__NEXT_DATA__`
   scripts; targets the right variant via product ID from URL (`/p-12345/` → `CatalogVariant:12345`);
   recursive search for `{value, currency}` price objects. Best for SPAs like Notino.
2. **`data-testid`** (`_extract_testid_price`) — `pd-price`, `price-variant`, `product-price`, `current-price`.
3. **JSON-LD** (`_extract_jsonld_price`) — `schema.org/Product` `offers.price`; URL/`@id` matching to pick the right variant.
4. **Meta tags** (`_extract_meta_price`) — `product:price:amount`, `itemprop="price"`.
5. **Amazon-specific** (`_extract_amazon_price`) — **gated to `amazon.` hosts**; `.priceToPay`,
   `.apex-pricetopay-value`, `corePriceDisplay_desktop_feature_div .a-offscreen`, generic `.a-offscreen`
   (skips "was:"/"rrp:"/null). Placed before generic selectors because Amazon splits price into
   `.a-price-whole`/`.a-price-fraction` sub-spans that would drop decimals.
6. **Generic CSS selectors** (`_extract_selector_price`) — `[class*="price"]`, `[id*="price"]`,
   `[data-price]`, `.product-price`, etc.
7. **Microdata** (`_extract_microdata_price`) — `itemprop="price"`.

### `_parse_price_string()` — number-format handling
Strips non-`[0-9.,]`, then disambiguates EU (`1.234,56`) vs US (`1,234.56`) by which separator is last.
Lone comma: decimal if ≤2 trailing digits, else thousands. Returns `None` for non-positive values.
Sanity cap of `< 100000` is applied in several extraction paths.

## 7. Alternate-Link Discovery (`alternate_links.py`)

Finds other stores selling the **same product** and stores the cheapest of them in `Product.alternative_urls`, so the price checker then monitors all of them (see §6) and the chart plots one line per store.

- **Triggers:** the scheduled job `scheduled_alternate_discovery()` (see §8) and the manual `POST /api/products/{id}/find-alternates` endpoint (a sync `def` on purpose, so FastAPI runs it in the worker thread pool — discovery takes minutes). The UI shows a "🔎 Find Links" button in the product detail header.
- **Entry point:** `find_alternate_links(product_id)` → returns a JSON-safe summary `{success, code, domain_suffix, candidates_found, saved_alternative_urls, message}`.

### Algorithm
1. **Identify the product by a globally unique code** (`_extract_product_code`): only EAN/UPC/GTIN barcodes (mod-10 check-digit validated via `_valid_ean`) and Amazon ASINs are accepted — store-internal IDs (eMAG `p-12345`, variant IDs, model numbers) are deliberately rejected so a link we're not reasonably sure is the same product is never saved.
   - Sources in priority order: page metadata (JSON-LD / microdata / `<meta name|property>` for `gtin, gtin13, gtin12, gtin14, upc, ean, sku, mpn` — harvested recursively by `_harvest_page_codes`/`_harvest_codes`; some stores file the EAN under `sku`/`mpn`), then check-digit-validated barcodes embedded in the URL slug itself (`_ean_from_url`, eMAG puts the EAN in the slug).
   - ASIN candidates: from `/dp/`, `/gp/product/`, `/product/` URL patterns and from `sku`/`mpn` fields matching `^[A-Z0-9]{10}$`.
2. **Search the web** (`_search_engines` / `_search_candidates`): quotes the exact code and merges results from several engines because no single one is reliable from server IPs — DuckDuckGo HTML (`a.result__a`, unwraps the `/l/?uddg=` redirect), DuckDuckGo Lite, Bing RSS (`format=rss`, plain `<link>` URLs), Bing HTML (`li.b_algo h2 a`, resolves `/ck/a` redirects with a follow-up request). Candidates are filtered to: same **country TLD suffix** as the original site (`_domain_suffix`, handles two-part suffixes like `com.ro` → `ro`), not the same store site (`_is_same_site`, subdomains count), not a **price-comparison aggregator** (`AGGREGATOR_HOSTS`: price.ro, compari.ro, idealo.*, geizhals.de, … — their pages list many stores' offers, not a single shop to monitor). Up to `SEARCH_RESULT_LIMIT = 10` candidates.
   - ASIN shortcut: no web search — the same ASIN is the same product on any Amazon domain, so `_asin_candidates` builds `https://amazon.{tld}/dp/{asin}` directly (`AMAZON_TLDS` maps `uk` → `co.uk`).
3. **Verify each candidate** (`_verify_candidate`): fetch the page and require that the exact product code appears in the URL **or** the HTML (same-product proof) **and** that `extract_price_auto` returns a sane price (`0 < p <= 1_000_000`).
4. **Rank and save**: verified new candidates plus the product's existing alternative links (re-priced, and any existing aggregator links are dropped) are pooled, sorted by price (None prices last), and the top `ALTERNATE_LINKS_MAX` (env, default 3, min 1) are written back to `alternative_urls` (committed only when the list actually changed).
- **Politeness:** 1.5 s delay between candidate page fetches, 1.0 s between search engines, 0.5 s between Bing redirect resolutions. Reuses `price_checker._fetch_page` (browser-impersonated) and `extract_price_auto`.
- **No code found** → returns `success: True` with an explanatory message and leaves existing links untouched.

## 8. Scheduling & Notifications

### `scheduler.py`
- `BackgroundScheduler(timezone=TZ env, default Europe/Bucharest)`.
- `init_scheduler()` parses `PRICE_CHECK_TIMES` (comma-separated `HH:MM`, default `09:00,14:00`) into cron jobs, then `ALTERNATE_LINK_TIMES` (same `HH:MM` format, **empty by default = feature disabled**) into `scheduled_alternate_discovery` jobs with `max_instances=1, coalesce=True` (a run takes minutes — it must never overlap itself).
- `scheduled_price_check()` is **global**: checks **all** enabled products across **all** users, then dispatches notifications per product.
- `scheduled_alternate_discovery()` runs `find_alternate_links()` (see §7) for every **enabled** product whose `auto_alternate_links` is True, logging a per-product summary.

### Notification dispatch (`_send_notifications_for_product`)
Called once per successfully-checked product (from `scheduled_price_check`). Everything is based on the **current price** = `MIN(price)` of the latest `check_cycle` (cheapest source at the last check). It loads all entries, finds the latest `check_cycle`, and splits rows into this cycle vs. everything before it:
- **First price** (no entries recorded before this cycle) → `send_first_price_notification` (price = current price)
- **New minimum** (current price < min over all previously recorded rows, i.e. a new all-time low) → `send_new_minimum_notification` (old min = previous all-time minimum)
- **Price drop** (current price < the previous check cycle's current price, and not a new minimum) → `send_price_drop_notification` (old price = previous cycle's current price)

Rows with NULL `check_cycle` (legacy) are treated as "before" but, without cycle stamps to compare a previous current price from, only suppress the drop case.

### `telegram_notifier.py`
- `is_configured()` = bot token present (chat ID is now per-user, not global).
- Chat ID resolution: product owner's `User.telegram_chat_id` (if notifications enabled), else fallback to global `TELEGRAM_CHAT_ID` env (comma-separated).
- Messages are HTML-formatted (`parse_mode="HTML"`) with product link.
- `verify_telegram_connection()` calls `getMe`; `test_notification()` for the settings-page test button.
- Extensive diagnostic logging for common failures (invalid token, chat not found, bot not in group).

## 9. Charts (`graph_generator.py`)
- `generate_price_chart()` — single-product history line chart, **one line per source domain**: the chronologically-first source keeps the primary blue and gets the area fill; alternatives get distinct colors (amber/green/purple/…) from a fixed palette. Each line is labeled with its main domain in the legend. Current-price + change annotations per source; stats box (max/avg). **No minimum-price markers** — min stars, min horizontal line, and the "Min:" annotation were removed on request (the frontend Chart.js chart likewise has no Minimum Price dataset). Returns base64 PNG. Entries without `source` (legacy rows) fall back to `'Price'`.
- `generate_comparison_chart()` — multi-product overlay.
- `get_price_statistics()` — min/max/avg/current/first/change/change_percent/total_checks. `current` = min of the latest `check_cycle` (cheapest source at the last check); falls back to the last entry for legacy rows without cycle stamps. `min` is the all-time minimum.
- **Timezone gotcha:** `_convert_to_local_time()` converts UTC → local TZ then **strips tzinfo** (returns naive) so matplotlib doesn't double-apply UTC conversion. `plt.rcParams['timezone']` set from `TZ` env.

## 10. Known Drift / Gotchas (verified against code)
- **README §"Price Extraction Strategies" is outdated** — it lists only JSON-LD, meta, CSS, microdata. The actual code has 7 strategies with embedded-JSON and data-testid *ahead* of JSON-LD, plus Amazon-specific handling.
- **`auth.py` `require_auth()` is dead code** — it references `HTTPException`, which is **not imported** in `auth.py` (only `Request` and `JSONResponse` are). It would raise `NameError` if called, but it's never used: `api.py` imports `get_current_user` (not `require_auth`) and authenticates via `get_user_from_request()` reading `request.state.user` (set by `AuthMiddleware`). Latent bug, never triggered.
- **Passwords use unsalted SHA-256** — fine for a personal tool, not for production multi-tenant use.
- **`telegram_notifier.py`** `send_message()` has an unreachable `return True` after `response.raise_for_status()` in the non-200 branch (lines 161-162). `raise_for_status()` raises `HTTPError`, caught by the `except requests.HTTPError` handler (returns `False`). So the `return True` is dead code; behavior is correct (returns `False` on HTTP error) but the line is misleading.
- **`python-telegram-bot`** is in `requirements.txt` but **unused** — the notifier uses raw `requests` against the Bot API. Only the local `telegram_notifier` module is imported.
- **Port inconsistency** — `Dockerfile` `EXPOSE 8000` + its `HEALTHCHECK` use port 8000, but `docker-compose.yml` sets `PORT=3000` (in-container), maps `13020:3000`, and overrides the healthcheck to 3000. The app reads `PORT` env (default 8000 in `main.py`). Under compose the app runs on 3000 inside the container (reachable from the host at 13020) and the compose healthcheck (3000) works. If you run the image standalone with `PORT=3000`, the Dockerfile's built-in healthcheck (8000) would fail. EXPOSE is cosmetic; the real port is whatever `PORT` is set to.
- **`models.py`** defines `TelegramNotification` and `AppSettings` but **neither is referenced anywhere** in the codebase — dead code. The models actually in use are `User`, `Product`, `PriceEntry`.
- **`.env.example` omits `TELEGRAM_CHAT_ID`** — intentional, since chat IDs are now per-user (set via the web settings page). The env var still works as a global fallback in `telegram_notifier.py` / `scheduler.py`.
- **`run_all_price_checks()` is dead code** — defined in `price_checker.py` and imported in both `api.py` and `scheduler.py`, but **never called**. The scheduler uses its own `scheduled_price_check()` (which loops `check_product_price` per product and dispatches notifications), and the manual "check all" endpoint (`POST /api/check-all`) calls `check_product_price` directly. The unused import in `api.py`/`scheduler.py` is a leftover.
- **`PriceEntry.raw_html` is a dead column** — declared in `models.py` but never assigned a value anywhere.
- **`DATABASE_URL` default differs by layer** — the code fallback in `database.py` is `sqlite:///./price_monitor.db` (current working dir), but `.env`/`docker-compose.yml` override it to `sqlite:///data/price_monitor.db` (the `/app/data` volume). In practice the DB lives in `data/`.
- **Auto-migration on startup** — `api.py` inspects the schema at startup and adds missing columns: `users.telegram_chat_id` / `users.telegram_notifications_enabled`, `products.alternative_urls TEXT`, `products.auto_alternate_links BOOLEAN DEFAULT 1`, `price_history.source VARCHAR(255)`, and `price_history.check_cycle DATETIME`. Existing rows get NULL `source` (chart data falls back to the product's main domain), default-1 `auto_alternate_links`, and NULL `check_cycle` (current-price lookups fall back to the latest entry; the next check starts proper cycles).
- **`RESTORE_LATEST_BACKUP` is one-shot** — while set to `true`, the newest Dropbox backup overwrites the database on **every** startup (see §15). Set it, let it restore, then set it back to `false`. A failed restore (or a schema-init failure after restore with no usable safety copy) aborts startup.
- **Backup snapshots use the SQLite online backup API** — `backup.py` never copies the `.db` file by hand; `sqlite3.Connection.backup()` is used so snapshots are consistent even while the app is writing. The physical DB path is parsed from `DATABASE_URL` (`make_url(...).database`); non-SQLite URLs are rejected (`BackupNotSupportedError`).

## 11. Configuration (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `TZ` | `Europe/Bucharest` | Scheduling + chart timezone |
| `DATABASE_URL` | `sqlite:///./price_monitor.db` (code) / `data/price_monitor.db` (env) | DB connection (SQLite or Postgres) |
| `TELEGRAM_BOT_TOKEN` | *(empty)* | Bot token from @BotFather (required for notifications) |
| `TELEGRAM_CHAT_ID` | *(empty)* | Global fallback chat ID(s), comma-separated |
| `PRICE_CHECK_TIMES` | `09:00,14:00` | Comma-separated 24h check times |
| `ALTERNATE_LINK_TIMES` | *(empty = disabled)* | Comma-separated 24h times for scheduled alternate-link discovery (§7) |
| `ALTERNATE_LINKS_MAX` | `3` | Max alternate links kept per product (min 1) |
| `PORT` | `3000` | HTTP server port (in-container) |
| `HOST` | `0.0.0.0` | Bind address |
| `ENABLE_SIGNUP` | `true` | Allow new registrations |
| `LOG_LEVEL` | `INFO` | Logging level |
| `BACKUP_ENABLED` | `true` | Enable/disable database backups (manual + scheduled) |
| `BACKUP_DROPBOX_REFRESH_TOKEN` | *(empty)* | Dropbox OAuth2 refresh token (long-lived) |
| `BACKUP_DROPBOX_APP_KEY` / `BACKUP_DROPBOX_APP_SECRET` | *(empty)* | Dropbox app credentials for token refresh |
| `BACKUP_DROPBOX_FOLDER` | `/Backup` | Remote folder for backups |
| `BACKUP_RETENTION_DAYS` | `30` | Prune local+remote backups older than N days |
| `BACKUP_SCHEDULE` | `0 2 * * *` | Cron expression for the scheduled backup (empty = disabled) |
| `RESTORE_LATEST_BACKUP` | `false` | One-shot startup restore of the newest Dropbox backup (§15) |

> Note: `TELEGRAM_CHAT_ID` is a global fallback only — it's not in `.env.example` because chat IDs are now per-user. `PORT` defaults to 8000 in `main.py` but compose/`.env` set it to 3000. Dropbox credentials are never exposed by the API (the status endpoint only reports configured/not).

## 12. API Surface (verified against `api.py`)

**Auth:** `GET /api/auth/status`, `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`

**Products:** `GET /api/health`, `GET/POST /api/products`, `GET/PUT/DELETE /api/products/{id}`, `POST /api/products/{id}/check`, `POST /api/products/{id}/find-alternates` (manual alternate-link discovery, §7), `GET /api/products/{id}/prices`, `GET /api/products/{id}/prices/reverse`, `GET /api/products/{id}/chart`, `GET /api/products/{id}/statistics`, `POST /api/check-all`

Note: `POST /api/products` runs an **initial price check immediately** (best-effort; failure doesn't block creation) and the response includes `initial_check_success`, `initial_check_price`, `initial_check_message`.

**Telegram:** `GET/PUT /api/telegram/settings`, `POST /api/telegram/test`

**Backup:** `GET /api/backup/status` (config without secrets), `POST /api/backup/run` (manual backup now; sync `def` → thread pool), `GET /api/backup/list` (local + Dropbox, newest first). No runtime restore endpoint — restore is startup-only (see §15).

**Config:** `GET /api/config/timezone`

## 13. Running It

```bash
# Docker (recommended)
cp .env.example .env   # edit as needed
docker-compose up --build
# → http://localhost:13020

# Local dev (no Docker)
cd backend
python -m venv venv && source venv/bin/activate   # venv\Scripts\activate on Windows
pip install -r requirements.txt
python main.py
```

## 14. Conventions for Future Edits
- **Alternative sources:** the product add/edit forms have an "Alternative Price Sources" section (`renderAltUrlRows`/`collectAltUrls` helpers, one input row per URL) plus an **"Auto find alternate links"** checkbox (`autoAlternates` / `editAutoAlternates`, bound to `auto_alternate_links`, checked by default). The price-history table has a **Source** column (domain badge; alternative domains get an amber badge vs. blue for the main one), and the in-browser Chart.js chart mirrors the server PNG: one dataset per source domain with matching colors (`#3b82f6` main, then `#f59e0b`, `#10b981`, …).
- **Alternate-link UI:** the product detail header has a **"🔎 Find Links"** button (`findCurrentAlternates()` → `POST /api/products/{id}/find-alternates`); it shows a toast with the saved count and the matched code (`Saved N alternate link(s) via EAN 4744131012001`) or the reason nothing was saved. Discovery is slow (several page fetches) — keep its endpoints as sync `def`s so they run in the thread pool, and keep the politeness delays in `alternate_links.py`.
- **Product list ordering:** the API returns products by `created_at DESC`; the frontend re-sorts for display with **enabled products on top**, disabled at the bottom (stable within each group, `index.html` around line 1036).
- **Product detail links:** the detail header shows every product-page link (main URL first, then alternatives) as a stacked block — source domain label on top, full clickable URL below it (`renderDetailLinks` + `.link-block`/`.link-source` CSS in `index.html`).
- Frontend is a **single HTML file** — no build tooling; keep changes self-contained in `index.html`.
- All API calls from the frontend send `X-PM-Token` header (see the `apiFetch`-style helper in `index.html`).
- New price-extraction heuristics: add a strategy function in `price_checker.py` and wire it into `extract_price_auto()` at the correct priority position; gate site-specific logic by host (like the Amazon check).
- New notification types: add a sender in `telegram_notifier.py` + a branch in `scheduler._send_notifications_for_product` (base every type on the current price = min of the latest `check_cycle`, per §8).
- Extending alternate-link discovery (§7): new search engines go in the `_search_engines` generator; new aggregator exclusions go in `AGGREGATOR_HOSTS`. Never weaken the same-product proof in `_verify_candidate` (code present in URL/HTML + extractable price) — that's what stops wrong products from being saved.
- Timezone-sensitive code: always convert via the `TZ` env and remember the naive-datetime requirement for matplotlib.

## 15. Dropbox Backup & Restore (`backup.py`)

Backups of the whole SQLite database to Dropbox — same functional pattern as the HouseholdReplacementTracker app, re-implemented in Python (raw `requests` against the Dropbox HTTP API; **no SDK dependency**).

- **Authentication:** long-lived OAuth2 **refresh token** + app key/secret (`BACKUP_DROPBOX_*`). `init_dropbox()` is called **lazily per operation** (never at import/startup): it guards on `BACKUP_ENABLED`/`RESTORE_LATEST_BACKUP` + credentials present, does a fresh `POST /oauth2/token` exchange, then verifies in two steps — `users/get_current_account` **best-effort** (logs account name/email; a failure here is only a warning, because some account setups — e.g. Dropbox Business/team accounts with a personal app — return `HTTP 500 "unexpected error occurred"` on that endpoint while file access still works) and a **functional probe** `ensure_dropbox_folder()` (`files/get_metadata` on the backup folder, auto-creating it via `files/create_folder_v2` on first run; a non-`path_not_found` failure — e.g. 401/403 — makes init return False). Masked credentials in diagnostics. Returns a bool, never raises. `_dropbox_post()` caches the token for subsequent calls in the same operation and retries once with a refreshed token on 401/403.
- **Endpoints:** `api.dropboxapi.com/2/...` (JSON: `files/list_folder` with cursor pagination, `files/delete_v2`, `files/get_metadata`, `files/create_folder_v2`, `users/get_current_account`) and `content.dropboxapi.com/2/...` (binary: `files/upload` with `Dropbox-API-Arg` header + JSON body args, `files/download` → raw bytes).
- **Backup file naming:** `pm_backup_YYYY-MM-DD_HHMMSS.sqlite` (local time), stored in `<db_dir>/backups/` alongside the DB file (so with the default config they land on the `data/` volume). Upload mode is `overwrite`, `strict_conflict: false`; the remote folder is created explicitly by `ensure_dropbox_folder()` on first run.
- **Snapshot:** `create_local_backup()` uses the **SQLite online backup API** (`sqlite3.connect(src).backup(dst)`) — a consistent copy even while the app is writing. Never a raw file copy.
- **`perform_backup()`:** local snapshot → (if Dropbox initialized) upload → remote retention prune → local retention prune (always, even on Dropbox-only failure). Returns `{"local": {...}, "dropbox": metadata|None}` or `None` when `BACKUP_ENABLED=false`. Raises on failure (endpoint returns 500 `{success: false, error}`; the cron wrapper logs).
- **Retention:** both remote and local `pm_backup_*` / `pm_pre_restore_*` files older than `BACKUP_RETENTION_DAYS` are deleted after each run (date parsed from the filename).
- **Schedule:** `init_scheduler()` adds a `database_backup` job from the `BACKUP_SCHEDULE` cron expression (`CronTrigger.from_crontab`, standard 5-field crontab, scheduler timezone, `max_instances=1, coalesce=True`); invalid expressions are logged and skipped. `scheduled_backup()` wraps `perform_backup()` with logging (never raises).
- **Startup restore (`RESTORE_LATEST_BACKUP=true`, one-shot):** at the top of `api.py`'s `startup()`, **before** `Base.metadata.create_all()`, `backup.restore_latest_backup()` runs: `init_dropbox()` (must succeed — otherwise startup aborts) → `find_newest_dropbox_backup()` (newest by `client_modified`) → download to `backups/_restore_temp.sqlite` → `validate_backup_sqlite()` (fresh standalone connection; requires `users`/`products`/`price_history` tables) → **safety copy** of the current DB via online backup to `backups/pm_pre_restore_*.sqlite` → `temp.replace(db_path)` → temp removed in `finally`. Any pre-replacement failure raises → **startup aborts** (container restarts). `engine.dispose()` is called right before the replacement anyway (defensive: a pooled connection still holding the live file would block `os.replace` on Windows).
- **Rollback:** the schema-init block (`create_all` + the known auto-migration ALTERs) was extracted to `_init_schema()` inside `startup()`. After a restore, if `_init_schema()` fails, the safety copy is copied back over the DB file, `engine.dispose()` drops pooled connections to the old (now stale) file, and `_init_schema()` retries once. Failure of the retry (or no safety copy) re-raises → startup aborts.
- **UI:** settings view has a "Database Backup & Restore (Dropbox)" card — status rows (backup enabled/disabled, dropbox configured + folder, retention • schedule • active-restore warning), "Backup Now" and "Refresh Backup List" buttons, and a merged local+Dropbox list (top 20). Status is loaded once on first visit to the settings view (`loadBackupStatus`), the list re-merges newest-first with shared ISO timestamps.
- **API shape:** `{"success": true, "data": ...}` on success; `500 {"success": false, "error": "..."}` on failure. `GET /api/backup/status` never includes secrets.