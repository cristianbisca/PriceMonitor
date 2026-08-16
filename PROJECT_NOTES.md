# PriceMonitor — Project Notes

> Reference documentation for AI-assisted development. Captures the architecture,
> data model, key flows, and non-obvious implementation details so future
> conversations can pick up context quickly. Keep this file updated as the codebase evolves.

## 1. What It Is

A **multi-user web application** that monitors e-commerce product prices. It:
- Checks prices on a schedule (default 09:00 & 14:00, TZ-aware)
- Records full price history per product
- Tracks all-time minimums
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
│   ├── price_checker.py     # Page fetch + 7-tier price extraction strategy
│   ├── scheduler.py         # APScheduler cron jobs; per-product notification dispatch
│   ├── telegram_notifier.py # Telegram Bot API message senders (per-user chat IDs)
│   ├── graph_generator.py   # matplotlib price history / comparison charts → base64 PNG
│   ├── requirements.txt
│   └── static/
│       ├── index.html       # The entire frontend SPA
│       ├── manifest.json    # PWA manifest
│       └── icon-*.png       # PWA icons (16/32/192/512)
├── Dockerfile               # python:3.12-slim, installs matplotlib system deps, EXPOSE 8000
├── docker-compose.yml       # Port 4300, network_mode: host, pricemonitor_data volume
├── .env.example             # Env var template
└── README.md                # User-facing docs (see §9 for known drift)
```

## 4. Data Model

```
User (id, username, password_hash, created_at,
      telegram_chat_id, telegram_notifications_enabled)
  └── Product (id, user_id, name, url, price_field, currency, enabled,
               created_at, updated_at, scraper_type, custom_selector)
        └── PriceEntry (id, product_id, price, currency, checked_at,
                        is_minimum, raw_html)

TelegramNotification (id, product_id, entry_id, chat_id, message_type,
                      sent_at, message_text)          # DEAD CODE — defined, never used
AppSettings (id, key, value)                          # DEAD CODE — defined, never used
```

- **`User`** — owns products; stores per-user Telegram settings (chat ID + enable toggle).
- **`Product`** — `scraper_type` is `"auto"` (default) or `"custom"`; `custom_selector` holds a user CSS selector when `scraper_type == "custom"`. `enabled` gates scheduled checks. `price_field` is a legacy hint column (unused by the current extraction logic). URL is unique per user (`uq_user_url`).
- **`PriceEntry`** — one row per check. `is_minimum` = True when this price is a new all-time low for the product. `checked_at` stored as UTC. `raw_html` is a `Text` column (comment says "for debugging/retries") but is **never populated** — `check_product_price()` creates the entry without it. Dead column.
- **`TelegramNotification`** — defined to track sent notifications (avoid duplicates) but **never referenced** anywhere in the codebase. Dead code.
- **`AppSettings`** — defined as a key/value store but **never referenced** anywhere in the codebase. Dead code.

## 5. Authentication

- **Tokens:** base64-encoded JSON `{user_id, username, ts}`. TTL = 7 days (`TOKEN_TTL_SECONDS = 604800`).
- **Password hashing:** SHA-256 hex digest (no salt — see §9).
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
1. Load product, fetch HTML via `_fetch_page()`.
2. If `scraper_type == "custom"` and `custom_selector` set → use that selector.
   Otherwise → `extract_price_auto(html, url)`.
3. Compute `is_minimum` (compare against current min for the product).
4. Insert `PriceEntry`, commit, return entry.

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

## 7. Scheduling & Notifications

### `scheduler.py`
- `BackgroundScheduler(timezone=TZ env, default Europe/Bucharest)`.
- `init_scheduler()` parses `PRICE_CHECK_TIMES` (comma-separated `HH:MM`, default `09:00,14:00`) into cron jobs.
- `scheduled_price_check()` is **global**: checks **all** enabled products across **all** users, then dispatches notifications per product.

### Notification dispatch (`_send_notifications_for_entry`)
For each successfully-checked product, determines type by history:
- **First price** (only 1 entry) → `send_first_price_notification`
- **New minimum** (`entry.is_minimum`) → `send_new_minimum_notification` (includes old min)
- **Price drop** (current < last checked) → `send_price_drop_notification`

### `telegram_notifier.py`
- `is_configured()` = bot token present (chat ID is now per-user, not global).
- Chat ID resolution: product owner's `User.telegram_chat_id` (if notifications enabled), else fallback to global `TELEGRAM_CHAT_ID` env (comma-separated).
- Messages are HTML-formatted (`parse_mode="HTML"`) with product link.
- `verify_telegram_connection()` calls `getMe`; `test_notification()` for the settings-page test button.
- Extensive diagnostic logging for common failures (invalid token, chat not found, bot not in group).

## 8. Charts (`graph_generator.py`)
- `generate_price_chart()` — single-product history line chart; red stars for minimums; min line; current-price + change annotations; stats box (min/max/avg). Returns base64 PNG.
- `generate_comparison_chart()` — multi-product overlay.
- `get_price_statistics()` — min/max/avg/current/first/change/change_percent/total_checks.
- **Timezone gotcha:** `_convert_to_local_time()` converts UTC → local TZ then **strips tzinfo** (returns naive) so matplotlib doesn't double-apply UTC conversion. `plt.rcParams['timezone']` set from `TZ` env.

## 9. Known Drift / Gotchas (verified against code)
- **README §"Price Extraction Strategies" is outdated** — it lists only JSON-LD, meta, CSS, microdata. The actual code has 7 strategies with embedded-JSON and data-testid *ahead* of JSON-LD, plus Amazon-specific handling.
- **`auth.py` `require_auth()` is dead code** — it references `HTTPException`, which is **not imported** in `auth.py` (only `Request` and `JSONResponse` are). It would raise `NameError` if called, but it's never used: `api.py` imports `get_current_user` (not `require_auth`) and authenticates via `get_user_from_request()` reading `request.state.user` (set by `AuthMiddleware`). Latent bug, never triggered.
- **Passwords use unsalted SHA-256** — fine for a personal tool, not for production multi-tenant use.
- **`scheduler.py`** has a redundant/unused `last_price` computation (lines 106-111) before using `last_entry` (line 113) — dead code.
- **`telegram_notifier.py`** `send_message()` has an unreachable `return True` after `response.raise_for_status()` in the non-200 branch (lines 161-162). `raise_for_status()` raises `HTTPError`, caught by the `except requests.HTTPError` handler (returns `False`). So the `return True` is dead code; behavior is correct (returns `False` on HTTP error) but the line is misleading.
- **`python-telegram-bot`** is in `requirements.txt` but **unused** — the notifier uses raw `requests` against the Bot API. Only the local `telegram_notifier` module is imported.
- **Port inconsistency** — `Dockerfile` `EXPOSE 8000` + its `HEALTHCHECK` use port 8000, but `docker-compose.yml` sets `PORT=4300` and overrides the healthcheck to 4300. The app reads `PORT` env (default 8000 in `main.py`). Under compose the app runs on 4300 and the compose healthcheck (4300) works. If you run the image standalone with `PORT=4300`, the Dockerfile's built-in healthcheck (8000) would fail. EXPOSE is cosmetic; the real port is whatever `PORT` is set to.
- **`models.py`** defines `TelegramNotification` and `AppSettings` but **neither is referenced anywhere** in the codebase — dead code. The models actually in use are `User`, `Product`, `PriceEntry`.
- **`.env.example` omits `TELEGRAM_CHAT_ID`** — intentional, since chat IDs are now per-user (set via the web settings page). The env var still works as a global fallback in `telegram_notifier.py` / `scheduler.py`.
- **`run_all_price_checks()` is dead code** — defined in `price_checker.py` and imported in both `api.py` and `scheduler.py`, but **never called**. The scheduler uses its own `scheduled_price_check()` (which loops `check_product_price` per product and dispatches notifications), and the manual "check all" endpoint (`POST /api/check-all`) calls `check_product_price` directly. The unused import in `api.py`/`scheduler.py` is a leftover.
- **`PriceEntry.raw_html` is a dead column** — declared in `models.py` but never assigned a value anywhere.
- **`DATABASE_URL` default differs by layer** — the code fallback in `database.py` is `sqlite:///./price_monitor.db` (current working dir), but `.env`/`docker-compose.yml` override it to `sqlite:///data/price_monitor.db` (the `/app/data` volume). In practice the DB lives in `data/`.

## 10. Configuration (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `TZ` | `Europe/Bucharest` | Scheduling + chart timezone |
| `DATABASE_URL` | `sqlite:///./price_monitor.db` (code) / `data/price_monitor.db` (env) | DB connection (SQLite or Postgres) |
| `TELEGRAM_BOT_TOKEN` | *(empty)* | Bot token from @BotFather (required for notifications) |
| `TELEGRAM_CHAT_ID` | *(empty)* | Global fallback chat ID(s), comma-separated |
| `PRICE_CHECK_TIMES` | `09:00,14:00` | Comma-separated 24h check times |
| `PORT` | `4300` | HTTP server port |
| `HOST` | `0.0.0.0` | Bind address |
| `ENABLE_SIGNUP` | `true` | Allow new registrations |
| `LOG_LEVEL` | `INFO` | Logging level |

> Note: `TELEGRAM_CHAT_ID` is a global fallback only — it's not in `.env.example` because chat IDs are now per-user. `PORT` defaults to 8000 in `main.py` but compose/`.env` set it to 4300.

## 11. API Surface (verified against `api.py`)

**Auth:** `GET /api/auth/status`, `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`

**Products:** `GET /api/health`, `GET/POST /api/products`, `GET/PUT/DELETE /api/products/{id}`, `POST /api/products/{id}/check`, `GET /api/products/{id}/prices`, `GET /api/products/{id}/prices/reverse`, `GET /api/products/{id}/chart`, `GET /api/products/{id}/statistics`, `POST /api/check-all`

**Telegram:** `GET/PUT /api/telegram/settings`, `POST /api/telegram/test`

**Config:** `GET /api/config/timezone`

## 12. Running It

```bash
# Docker (recommended)
cp .env.example .env   # edit as needed
docker-compose up --build
# → http://localhost:4300

# Local dev (no Docker)
cd backend
python -m venv venv && source venv/bin/activate   # venv\Scripts\activate on Windows
pip install -r requirements.txt
python main.py
```

## 13. Conventions for Future Edits
- Frontend is a **single HTML file** — no build tooling; keep changes self-contained in `index.html`.
- All API calls from the frontend send `X-PM-Token` header (see the `apiFetch`-style helper in `index.html`).
- New price-extraction heuristics: add a strategy function in `price_checker.py` and wire it into `extract_price_auto()` at the correct priority position; gate site-specific logic by host (like the Amazon check).
- New notification types: add a sender in `telegram_notifier.py` + a branch in `scheduler._send_notifications_for_entry`.
- Timezone-sensitive code: always convert via the `TZ` env and remember the naive-datetime requirement for matplotlib.