# AGENTS.md — Guide for AI-Assisted Development

Price Monitor is a **multi-user FastAPI + SQLite web app** that scrapes e-commerce product prices on a schedule, keeps full price history per product (and per alternative store), and sends Telegram alerts on price drops / new minimums. The frontend is a **single-file vanilla-JS SPA** served by the same app.

Read `PROJECT_NOTES.md` before making changes — it documents the architecture, data model, key flows, and known gotchas in detail. `README.md` is user-facing documentation.

## Running it

```bash
# Docker (recommended; http://localhost:13020)
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
│   ├── models.py            # ORM: User, Product, PriceEntry, LinkCandidate (+ 2 dead models, see gotchas)
│   ├── price_checker.py     # Page fetch (curl_cffi browser impersonation) + 7-tier price extraction
│   ├── alternate_links.py   # Discovers same product on other stores (code/model/name/keyword) as reviewable candidates
│   ├── scheduler.py         # APScheduler: PRICE_CHECK_TIMES + ALTERNATE_LINK_TIMES + BACKUP_SCHEDULE cron jobs
│   ├── backup.py            # Dropbox backup/restore (raw requests, no SDK): snapshots, uploads, retention, boot-restore
│   ├── telegram_notifier.py # Telegram Bot API via raw requests (per-user chat IDs)
│   ├── graph_generator.py   # matplotlib (Agg) charts → base64 PNG, one line per source domain
│   ├── requirements.txt     # Python 3.12 deps (see unused deps in gotchas)
│   ├── price_monitor.db     # Local dev SQLite file (Docker uses /app/data/price_monitor.db)
│   └── static/
│       ├── index.html       # THE ENTIRE frontend SPA (vanilla JS, Chart.js, no build step)
│       ├── vendor/          # self-hosted chart.js / luxon / adapter (same-origin, see §security)
│       ├── manifest.json    # PWA manifest
│       └── icon-*.png       # PWA icons (16/32/192/512)
├── Dockerfile               # python:3.12-slim, EXPOSE 8000 (cosmetic; real port = PORT env)
├── docker-compose.yml       # Port 13020→3000 mapping, pricemonitor_data volume
├── .env.example             # Env var template
├── AGENTS.md                # This file
├── PROJECT_NOTES.md         # Architecture & implementation details (the deep reference)
└── README.md                # User-facing docs
```

## Key flows

- **Scheduled check** (`scheduler.scheduled_price_check`): all enabled products, all users → `price_checker.check_product_price(id)` per product (main URL + `alternative_urls`, one `PriceEntry` per successful source, tagged with `source` domain; all entries in a run share one `check_cycle` timestamp) → per-product Telegram dispatch (`_send_notifications_for_product`: first price / new minimum / drop). **Current price = min over all sources of the latest check cycle; minimum price = all-time min; notifications are based on the current price.**
- **Candidate-link discovery** (`alternate_links.find_alternate_links`): extract from the product page a globally unique code (EAN/GTIN/UPC check-digit validated / ASIN), model numbers (MPN), and a cleaned product name → probe up to four match methods in reliability order (`code` then `model` then `name` (quoted exact-phrase search) then `keyword` (unquoted, Google-style title search, probed only if `name` found no promising candidate); early-stop once a "promising" candidate exists) — code/model web-search the exact value (DDG html/lite + Bing RSS/HTML, same country TLD suffix only, aggregators excluded; ASINs go straight to `amazon.<tld>/dp/<ASIN>`); name/keyword compare the candidate page's extracted name for the same product (accessory, variant-tier, or capacity-mismatch, or different-product rejected — e.g. a 2x8GB kit for a 16GB single) → every candidate must fetch with an extractable price and be **cheaper** than the current price → stored as `LinkCandidate` rows (cheapest `ALTERNATE_LINKS_MAX`, upserted on product+url) **awaiting user review** — the user approves (URL appended to `alternative_urls`) or dismisses (never suggested again) in the UI. Triggered by `ALTERNATE_LINK_TIMES` schedule (products with `auto_alternate_links=True`) or the manual `find-alternates` endpoint / "🔎 Find Links" button. Product list/detail responses carry `candidate_count` (pending cheaper candidates, capped) for the "New Links Found" dashboard stat and per-product 🔗 badge.
- **Chart**: `graph_generator.generate_price_chart` — one line per source domain (chronologically-first source = primary blue + area fill), UTC→local conversion via `TZ` with naive datetimes for matplotlib.
- **Backup & restore** (`backup.py`): Dropbox OAuth2 refresh token + app key/secret (env) → fresh access token per operation (lazy, never at startup) → consistent SQLite snapshot (`sqlite3` online backup API, never a raw file copy) to `pm_backup_YYYY-MM-DD_HHMMSS.sqlite` next to the DB file → upload (`overwrite`) → retention prune (local + remote, date parsed from filename). `BACKUP_SCHEDULE` cron job; manual `POST /api/backup/run`; `GET /api/backup/status` (no secrets) / `GET /api/backup/list`. **Restore is startup-only**: `RESTORE_LATEST_BACKUP=true` → newest Dropbox backup downloaded + validated + DB file replaced with a `pm_pre_restore_*` safety copy kept — see gotchas.

## Conventions (follow these)

- **Frontend is one file** — `backend/static/index.html`. No build tooling; keep changes self-contained. All API calls send the `X-PM-Token` header (see the `window.api` helper in the file).
- **CSP forbids `unsafe-inline`** — the SPA's inline `<style>`/`<script>` get a per-request nonce injected by `serve_frontend` (see PROJECT_NOTES §security). So in `index.html`: **no inline `onclick`/`onchange`/… attributes and no inline `style="…"`**. Bind static elements with `addEventListener` in the IIFE's `bindStaticEvents()` (once, not in `initApp()`); for re-rendered table/candidate rows use `data-action`/`data-id` + container-level delegation; use the `.is-hidden`/`.text-*`/`.form-hint` utility classes for one-off styles. JS may still set `el.style.*` (CSSOM) — that's allowed.
- **Auth**: tokens are base64-JSON `{user_id, username, ts}`, 7-day TTL, validated against the DB on every request by `AuthMiddleware`. Endpoints read the user from `request.state.user` (set by middleware) via `get_user_from_request()` — never via `require_auth()` (dead, broken, see gotchas).
- **Per-user data isolation**: every product query filters by `user_id` from the token. New endpoints MUST enforce this.
- **Timezones**: store UTC in the DB; convert with the `TZ` env for display. For matplotlib, convert then **strip tzinfo** (naive).
- **New price-extraction heuristic**: add a strategy function in `price_checker.py` and wire it into `extract_price_auto()` at the right priority position; gate site-specific logic by host (like the Amazon check).
- **New notification type**: sender in `telegram_notifier.py` + branch in `scheduler._send_notifications_for_product` (base it on the current price = min of the latest `check_cycle`).
- **New extraction field on a model**: SQLAlchemy `create_all` doesn't alter existing tables — `api.py`'s startup hook only auto-migrates *specific known columns*. Add your column to that hook too.
- **Bootstrap order**: `api.py`'s startup hook does the one-shot Dropbox restore (`backup.restore_latest_backup()`) **before** `create_all`/auto-migration — the DB file must be replaced while the engine has made no connections yet. Preserve that order and the rollback path (restore → `_init_schema()` fails → copy back `pm_pre_restore_*` + `engine.dispose()` + retry).
- **Slow endpoints**: keep long-running endpoints (`find-alternates`, `backup/run`, `backup/list`) as sync `def`s so FastAPI runs them in the thread pool.
- Keep `README.md` and `PROJECT_NOTES.md` in sync when behavior changes (especially the API-surface and env-var tables).

## Known gotchas (verified; full list in PROJECT_NOTES.md §10)

- **Dead code**: `auth.require_auth()` (would raise `NameError` — `HTTPException` not imported), `price_checker.run_all_price_checks()`, `models.TelegramNotification`, `models.AppSettings`, `PriceEntry.raw_html` column, `Product.price_field`.
- **Unreachable line**: `telegram_notifier.send_message()` has `return True` after a `raise_for_status()` that always raises on that path — behavior is correct (returns False), line is misleading.
- **Unused deps**: `python-telegram-bot` in `requirements.txt` (notifier uses raw `requests`).
- **Passwords**: unsalted SHA-256 — fine for personal use, not production multi-tenant.
- **Port inconsistency**: Dockerfile EXPOSE/healthcheck say 8000; compose sets `PORT=3000` and maps `13020:3000`. The real port is always the `PORT` env.
- **`DATABASE_URL`** default differs by layer (code: `sqlite:///./price_monitor.db`; env/compose: `sqlite:///data/price_monitor.db` on the volume).
- **`PRICE_PATTERNS`** in `price_checker.py` is a vestigial module-level list — extraction doesn't use it.
- **Scraper types**: `scraper_type` is effectively only `"auto"` or `"custom"`; the comment listing "amazon", "ecommerce" etc. is aspirational.
- **`RESTORE_LATEST_BACKUP` is one-shot** — while `true`, the newest Dropbox backup overwrites the DB on every boot and a failed restore aborts startup. Set it, let it restore, set it back to `false` (see PROJECT_NOTES.md §15).
- **Backup path resolution**: `backup.py` parses the physical SQLite path from `DATABASE_URL` (`make_url(...).database`) and rejects non-SQLite URLs — keep it that way if you touch DB-location code.
