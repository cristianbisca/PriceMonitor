# AGENTS.md — Guide for AI-Assisted Development

Price Monitor is a **multi-user FastAPI + SQLite web app** that scrapes e-commerce product prices on a schedule, keeps full price history per product (and per alternative store), and sends Telegram alerts on price drops / new minimums. The frontend is a **single-file vanilla-JS SPA** served by the same app.

Read `PROJECT_NOTES.md` before making changes — it documents the architecture, data model, key flows, and known gotchas in detail. `README.md` is user-facing documentation.

## Running it

```bash
# Docker (recommended; http://localhost:4300)
cp .env.example .env          # edit as needed
docker-compose up --build

# Local dev (no Docker)
cd backend
python -m venv venv           # venv\Scripts\activate on Windows
pip install -r requirements.txt
python main.py                # HOST/PORT/RELOAD/LOG_LEVEL env vars, default port 8000
```

There is **no test suite and no linter configured**. Verify changes by running the app and exercising the API (`/api/health`, Swagger at `/docs`) or by targeted re-runs of the affected module.

## Project structure

```
PriceMonitor/
├── backend/
│   ├── main.py              # Thin uvicorn launcher (reads HOST/PORT/RELOAD, runs "api:app")
│   ├── api.py               # THE FastAPI app: middleware, startup auto-migration, ALL endpoints
│   ├── auth.py              # Base64-JSON token auth, SHA-256 passwords, AuthMiddleware (X-PM-Token header)
│   ├── database.py          # SQLAlchemy engine/session (DATABASE_URL env, SQLite default)
│   ├── models.py            # ORM: User, Product, PriceEntry (+ 2 dead models, see gotchas)
│   ├── price_checker.py     # Page fetch (curl_cffi browser impersonation) + 7-tier price extraction
│   ├── alternate_links.py   # Auto-discovers same product on other stores (EAN/ASIN + web search)
│   ├── scheduler.py         # APScheduler: PRICE_CHECK_TIMES + ALTERNATE_LINK_TIMES cron jobs
│   ├── telegram_notifier.py # Telegram Bot API via raw requests (per-user chat IDs)
│   ├── graph_generator.py   # matplotlib (Agg) charts → base64 PNG, one line per source domain
│   ├── requirements.txt     # Python 3.12 deps (see unused deps in gotchas)
│   ├── price_monitor.db     # Local dev SQLite file (Docker uses /app/data/price_monitor.db)
│   └── static/
│       ├── index.html       # THE ENTIRE frontend SPA (vanilla JS, Chart.js, no build step)
│       ├── manifest.json    # PWA manifest
│       └── icon-*.png       # PWA icons (16/32/192/512)
├── Dockerfile               # python:3.12-slim, EXPOSE 8000 (cosmetic; real port = PORT env)
├── docker-compose.yml       # Port 4300, network_mode: host, pricemonitor_data volume
├── .env.example             # Env var template
├── AGENTS.md                # This file
├── PROJECT_NOTES.md         # Architecture & implementation details (the deep reference)
└── README.md                # User-facing docs
```

## Key flows

- **Scheduled check** (`scheduler.scheduled_price_check`): all enabled products, all users → `price_checker.check_product_price(id)` per product (main URL + `alternative_urls`, one `PriceEntry` per successful source, tagged with `source` domain) → per-product Telegram dispatch (first price / new minimum / drop).
- **Alternate-link discovery** (`alternate_links.find_alternate_links`): extract EAN/GTIN/UPC (check-digit validated) or ASIN from page metadata or URL slug → web-search the exact code (DDG html/lite + Bing RSS/HTML), same country TLD suffix only, aggregators excluded → fetch each candidate and require the same code on the page + an extractable price → keep cheapest `ALTERNATE_LINKS_MAX` links. Triggered by `ALTERNATE_LINK_TIMES` schedule (products with `auto_alternate_links=True`) or the manual `find-alternates` endpoint / "🔎 Find Links" button.
- **Chart**: `graph_generator.generate_price_chart` — one line per source domain (chronologically-first source = primary blue + area fill), UTC→local conversion via `TZ` with naive datetimes for matplotlib.

## Conventions (follow these)

- **Frontend is one file** — `backend/static/index.html`. No build tooling; keep changes self-contained. All API calls send the `X-PM-Token` header (see the `window.api` helper in the file).
- **Auth**: tokens are base64-JSON `{user_id, username, ts}`, 7-day TTL, validated against the DB on every request by `AuthMiddleware`. Endpoints read the user from `request.state.user` (set by middleware) via `get_user_from_request()` — never via `require_auth()` (dead, broken, see gotchas).
- **Per-user data isolation**: every product query filters by `user_id` from the token. New endpoints MUST enforce this.
- **Timezones**: store UTC in the DB; convert with the `TZ` env for display. For matplotlib, convert then **strip tzinfo** (naive).
- **New price-extraction heuristic**: add a strategy function in `price_checker.py` and wire it into `extract_price_auto()` at the right priority position; gate site-specific logic by host (like the Amazon check).
- **New notification type**: sender in `telegram_notifier.py` + branch in `scheduler._send_notifications_for_entry`.
- **New extraction field on a model**: SQLAlchemy `create_all` doesn't alter existing tables — `api.py`'s startup hook only auto-migrates *specific known columns*. Add your column to that hook too.
- Keep `README.md` and `PROJECT_NOTES.md` in sync when behavior changes (especially the API-surface and env-var tables).

## Known gotchas (verified; full list in PROJECT_NOTES.md §10)

- **Dead code**: `auth.require_auth()` (would raise `NameError` — `HTTPException` not imported), `price_checker.run_all_price_checks()`, `models.TelegramNotification`, `models.AppSettings`, `PriceEntry.raw_html` column, `Product.price_field`.
- **Unreachable line**: `telegram_notifier.send_message()` has `return True` after a `raise_for_status()` that always raises on that path — behavior is correct (returns False), line is misleading.
- **Unused deps**: `python-telegram-bot` in `requirements.txt` (notifier uses raw `requests`).
- **Passwords**: unsalted SHA-256 — fine for personal use, not production multi-tenant.
- **Port inconsistency**: Dockerfile EXPOSE/healthcheck say 8000; compose forces `PORT=4300`. The real port is always the `PORT` env.
- **`DATABASE_URL`** default differs by layer (code: `sqlite:///./price_monitor.db`; env/compose: `sqlite:///data/price_monitor.db` on the volume).
- **`PRICE_PATTERNS`** in `price_checker.py` is a vestigial module-level list — extraction doesn't use it.
- **Scraper types**: `scraper_type` is effectively only `"auto"` or `"custom"`; the comment listing "amazon", "ecommerce" etc. is aspirational.
