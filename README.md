# 📊 Price Monitor

A web application that monitors price changes for products across different e-commerce sites. It automatically checks prices at scheduled times, tracks history with interactive charts, and sends Telegram notifications when prices drop.

## ✨ Features

- **Multi-User Authentication** — Token-based auth with user registration, login, and logout
- **Per-User Data Isolation** — Each user has their own products, price history, and settings
- **Product Management** — Add products by URL with auto-detect or custom CSS selector price extraction
- **Multi-Source Price Comparison** — Each product can have alternative store URLs, checked alongside the main one and plotted as separate lines per store
- **Suggested Alternate Links (candidates)** — Finds the same product on other stores (same country domain) by product code (EAN/GTIN/UPC/ASIN), model number, or product name; suggested links are only candidates until you review them: cheaper ones are shown in the product detail page, one to approve (starts being tracked on every price check) or dismiss (never suggested again). A "New Links Found" dashboard stat and a per-product badge track pending candidates
- **Scheduled Price Checks** — Configurable check times (default: 9 AM and 2 PM)
- **Price History Charts** — Interactive Chart.js graphs showing price trends over time
- **Minimum Price Tracking** — Highlights the lowest price ever recorded for each product
- **Per-User Telegram Notifications** — Each user configures their own Chat ID via a settings page
- **PWA Support** — Installable on Android with home screen icons
- **Dark Theme UI** — Modern responsive web interface
- **Sign-Up Control** — Admin can disable new registrations via `ENABLE_SIGNUP` env variable
- **Database Backup & Restore** — Scheduled + manual database backups to Dropbox with retention cleanup, and one-shot restore from the newest backup
- **Docker Deployment** — Ready for Portainer or docker-compose

## 🏗 Architecture

```
├── backend/
│   ├── api.py              # FastAPI REST API endpoints (auth, products, telegram settings)
│   ├── auth.py             # Token-based authentication system (middleware, token generation/validation)
│   ├── database.py         # SQLAlchemy database setup
│   ├── models.py           # Database models (User, Product, PriceEntry)
│   │   └── (also declares TelegramNotification & AppSettings — currently unused)
│   ├── main.py             # Application entry point
│   ├── price_checker.py    # Web scraping & price extraction logic
│   ├── alternate_links.py  # Finds the same product on other stores (code / model number / name) as reviewable candidates
│   ├── graph_generator.py  # Matplotlib chart generation (PNG)
│   ├── telegram_notifier.py# Telegram bot notifications (per-user chat ID support)
│   ├── scheduler.py        # APScheduler for periodic checks (price checks + database backup)
│   ├── backup.py           # Dropbox database backup & restore
│   ├── requirements.txt    # Python dependencies
│   └── static/
│       └── index.html      # Frontend SPA (Chart.js, vanilla JS, PWA manifest)
├── Dockerfile              # Single-stage Docker build
├── docker-compose.yml      # Docker Compose (local dev & Portainer deployment)
├── .env.example            # Environment variables template
├── AGENTS.md               # Guide for AI-assisted development
├── PROJECT_NOTES.md        # Detailed architecture & implementation notes
└── README.md
```

## 🚀 Quick Start

### Local Development

1. **Clone and setup environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your Telegram credentials (optional)
   ```

2. **Run with Docker Compose:**
   ```bash
   docker-compose up --build
   ```

3. **Open the web interface:**
   ```
   http://localhost:13020
   ```

4. **Register a new account** and start adding products to monitor.

### Development without Docker

```bash
cd backend
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
python main.py
```

## ⚙️ Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `Europe/Bucharest` | Timezone for scheduling |
| `DATABASE_URL` | `sqlite:///data/price_monitor.db` | Database connection string |
| `TELEGRAM_BOT_TOKEN` | *(empty)* | Telegram bot token from @BotFather (required for notifications) |
| `PRICE_CHECK_TIMES` | `09:00,14:00` | Comma-separated check times in 24h format |
| `ALTERNATE_LINK_TIMES` | *(empty = disabled)* | Comma-separated 24h times for automatic alternate-link discovery |
| `ALTERNATE_LINKS_MAX` | `3` | Max candidate (suggested) links per product (min 1) |
| `PORT` | `3000`* | HTTP server port (in-container; exposed as `13020` via compose) |
| `HOST` | `0.0.0.0` | Bind address |
| `ENABLE_SIGNUP` | `true` | Allow new user registrations (`true`/`false`) |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `TELEGRAM_CHAT_ID` | *(empty)* | Global fallback chat ID(s), comma-separated — used only when a user hasn't set their own via the web UI |
| `BACKUP_ENABLED` | `true` | Enable/disable database backups |
| `BACKUP_DROPBOX_REFRESH_TOKEN` | *(empty)* | Dropbox OAuth2 refresh token (see Backup Setup below) |
| `BACKUP_DROPBOX_APP_KEY` | *(empty)* | Dropbox app key |
| `BACKUP_DROPBOX_APP_SECRET` | *(empty)* | Dropbox app secret |
| `BACKUP_DROPBOX_FOLDER` | `/Backup` | Dropbox folder where backups are stored |
| `BACKUP_RETENTION_DAYS` | `30` | Delete backups older than this many days |
| `BACKUP_SCHEDULE` | `0 2 * * *` | Cron expression for the scheduled backup (empty = disabled, runs daily at 02:00 by default) |
| `RESTORE_LATEST_BACKUP` | `false` | One-shot: restore the newest Dropbox backup on the next startup (see below) |

> \* In code the defaults are `PORT=8000` and `DATABASE_URL=sqlite:///./price_monitor.db`; the Docker Compose / `.env` values shown above (`3000` and `sqlite:///data/price_monitor.db`) are what apply in practice.

### Authentication

The application uses **token-based authentication**:

- Tokens are base64-encoded JSON payloads containing `user_id`, `username`, and a timestamp
- Token validity: **7 days** (tokens are persisted in browser localStorage)
- Tokens are passed via `X-PM-Token` header or `Authorization: Bearer <token>` header
- Sign-up can be disabled by setting `ENABLE_SIGNUP=false`

## 📱 Telegram Setup

### Administrator Setup (Bot Token)

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the instructions to create a bot
3. Copy the bot token and set it in `TELEGRAM_BOT_TOKEN` environment variable

### User Setup (Chat ID via Web Interface)

Each user configures their own Telegram notifications through the web interface:

1. Log in to the web application
2. Navigate to **Settings** page
3. Get your Chat ID:
   - Search for **@userinfobot** on Telegram to get your personal chat ID
   - Or add the bot to a group and use the group's chat ID (e.g., `-1001234567890`)
4. Enter your Chat ID in the settings form
5. Enable notifications by toggling **"Enable Telegram Notifications"**
6. Click **"Send Test Notification"** to verify everything works

### How "current price" works

A product can have several price sources (the main URL plus alternative stores). The **current price** is always the **cheapest price across all sources at the time of the last check** (the minimum of that check run). The **minimum price** shown for a product is the **all-time minimum** — the lowest of every price ever recorded.

### Notification Types

All alerts are based on the current price (cheapest source at the last check):

- **🔻 Price Drop Alert** — Sent when the current price is lower than the current price from the previous check
- **🎉 NEW MINIMUM PRICE!** — Sent when the current price is a new all-time low
- **✅ First Price Recorded** — Sent on the first successful check for a new product

## 📦 Backup & Restore (Dropbox)

The whole database is backed up to Dropbox on a schedule and on demand, so the entire dataset (all users, products, and price history) can be moved or recovered.

### Backup Setup (One-Time)

1. Open [https://www.dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) and click **Create app**
2. Choose **App Folder** (or Full Dropbox) and select the **Files.content, files.metadata** scopes (read + write)
3. In the app's **OAuth 2.0** settings, enable **Token refresh** and generate a long-lived **refresh token** (paste it once into your Dropbox session to link the app)
4. Copy the **Access token / refresh token**, **App key**, and **App secret** into your `.env`:

   ```
   BACKUP_DROPBOX_REFRESH_TOKEN=...
   BACKUP_DROPBOX_APP_KEY=...
   BACKUP_DROPBOX_APP_SECRET=...
   BACKUP_DROPBOX_FOLDER=/Backup    # optional
   ```

5. Restart the container — the app lazily refreshes a short-lived access token from the refresh token on every backup operation (the refresh token never expires as long as the app stays used), so no other maintenance is needed.

### How Backups Work

- A **consistent snapshot** of the SQLite database is written to `data/backups/pm_backup_YYYY-MM-DD_HHMMSS.sqlite` (next to the DB file, on the Docker volume) and uploaded to `BACKUP_DROPBOX_FOLDER` in Dropbox, overwriting any file with the same name.
- The scheduled backup runs per `BACKUP_SCHEDULE` (cron expression, default daily at 02:00, server `TZ`); manual backups can be triggered at any time from **Settings → Database Backup & Restore → "Backup Now"** in the web UI (or `POST /api/backup/run`).
- Backups older than `BACKUP_RETENTION_DAYS` (default 30) are automatically deleted from both Dropbox and the local `backups/` folder after each run.
- If Dropbox isn't configured, backups are still created locally — the UI and logs say "local only".

### Restoring a Backup

Restores happen at **startup**, never while the app is running (the DB file is replaced before any connection is opened):

1. Set `RESTORE_LATEST_BACKUP=true` in `.env`
2. Restart the container — the newest Dropbox backup is downloaded, validated in a separate connection, a safety copy of the current database is saved as `data/backups/pm_pre_restore_*.sqlite`, and the current database file is replaced with the backup
3. Set `RESTORE_LATEST_BACKUP=false` again (it runs on **every** startup while `true`)

If anything fails before the database file is replaced (bad credentials, no backups found, corrupted backup file), the container **refuses to start** — the current database is left untouched. If the restored file turns out to be unusable right after the restore, the app automatically rolls back to the pre-restore safety copy.

## 🐳 Portainer Deployment

1. Push your code to a GitHub repository
2. In Portainer, go to **Stacks → Add stack**
3. Fill in:
   - **Name:** `price-monitor`
   - **Web/Git:** Select "Git repository"
   - **Repository URL:** Your GitHub repo URL
   - **Reference:** `main` (or your branch)
   - **Compose file:** `docker-compose.yml`
4. Add environment variables in the **Advanced** section or via env_file
5. Click **Deploy the stack**

## 🔌 API Endpoints

### Authentication

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| `GET` | `/api/auth/status` | Check auth status & sign-up availability | No |
| `POST` | `/api/auth/register` | Register a new user | No |
| `POST` | `/api/auth/login` | Login and receive a token | No |
| `POST` | `/api/auth/logout` | Logout (client clears token) | No |
| `GET` | `/api/auth/me` | Get current user info | Yes |

### Products

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/products` | List all products for current user with stats (incl. `candidate_count` = pending cheaper suggestions) |
| `POST` | `/api/products` | Add a new product |
| `GET` | `/api/products/{id}` | Get product details |
| `PUT` | `/api/products/{id}` | Update a product |
| `DELETE` | `/api/products/{id}` | Delete a product |
| `POST` | `/api/products/{id}/check` | Trigger immediate price check |
| `POST` | `/api/products/{id}/find-alternates` | Trigger candidate-link discovery now (takes a couple of minutes) |
| `GET` | `/api/products/{id}/candidates` | List pending suggested (candidate) links for the product |
| `POST` | `/api/products/{id}/candidates/{cid}/approve` | Approve a candidate — its URL is added to the product's alternative links |
| `POST` | `/api/products/{id}/candidates/{cid}/dismiss` | Dismiss a candidate — it is never suggested again |
| `GET` | `/api/products/{id}/prices` | Get price history (newest first) |
| `GET` | `/api/products/{id}/prices/reverse` | Get price history (oldest first, for charts) |
| `GET` | `/api/products/{id}/chart` | Generate PNG chart image |
| `GET` | `/api/products/{id}/statistics` | Get price statistics |
| `POST` | `/api/check-all` | Check all enabled products for current user |

### Telegram Settings (Per-User)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/telegram/settings` | Get current user's Telegram settings |
| `PUT` | `/api/telegram/settings` | Update Telegram Chat ID and notification toggle |
| `POST` | `/api/telegram/test` | Send test notification to user's chat |

### Backup & Restore (Dropbox)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/backup/status` | Backup configuration & status (no secrets) |
| `POST` | `/api/backup/run` | Trigger a database backup now (local snapshot + Dropbox upload) |
| `GET` | `/api/backup/list` | List local and Dropbox backups (newest first) |

### Config

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| `GET` | `/api/config/timezone` | Current server timezone (for the UI clock) | No |

### Example: Register a User

```bash
curl -X POST http://localhost:13020/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "john_doe",
    "password": "secure_password"
  }'
```

Response includes a token for immediate login.

### Example: Login

```bash
curl -X POST http://localhost:13020/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "john_doe",
    "password": "secure_password"
  }'
```

### Example: Add a Product (with token)

```bash
curl -X POST http://localhost:13020/api/products \
  -H "Content-Type: application/json" \
  -H "X-PM-Token: <your_token_here>" \
  -d '{
    "name": "iPhone 15 Pro",
    "url": "https://example.com/iphone-15-pro",
    "currency": "RON",
    "scraper_type": "auto"
  }'
```

The server runs an initial price check right away (best-effort); the response includes `initial_check_success`, `initial_check_price`, and `initial_check_message`.

### Example: Update Telegram Settings

```bash
curl -X PUT http://localhost:13020/api/telegram/settings \
  -H "Content-Type: application/json" \
  -H "X-PM-Token: <your_token_here>" \
  -d '{
    "telegram_chat_id": "123456789",
    "telegram_notifications_enabled": true
  }'
```

### Example: Check Price Now

```bash
curl -X POST http://localhost:13020/api/products/1/check \
  -H "X-PM-Token: <your_token_here>"
```

## 📸 Screenshots

The web interface features:
- **Login/Register** — Themed authentication UI with sign-up toggle control
- **Dashboard** — Overview statistics (total products, active checks, new links found, price drops)
- **Product List** — Table with current/min/avg prices, a "New Links" pending-candidate badge, and quick actions
- **Product Detail** — Interactive Chart.js price history graph plus a "Suggested Links" panel with approve / dismiss actions
- **Settings Page** — Per-user Telegram notification configuration with test button
- **Sign Out** — Button to clear session token

## 🔧 Price Extraction Strategies

Pages are fetched with browser-impersonated requests (`curl_cffi`) to get past Akamai/Cloudflare bot walls (e.g. Amazon), falling back to a plain `requests` session if impersonation isn't available.

The auto-detect scraper then tries these methods in order (most → least reliable):

1. **Embedded SSR JSON** — parses `__APOLLO_STATE__` / `__NEXT_DATA__` scripts and targets the right variant via the product ID in the URL (e.g. `/p-12345/`); recursive search for `{value, currency}` price objects. Best for SPAs like Notino.
2. **`data-testid` attributes** — `pd-price`, `price-variant`, `product-price`, `current-price` (SPAs use these for test automation).
3. **JSON-LD structured data** — `schema.org/Product` `offers.price`, with URL / `@id` matching to pick the correct variant when multiple products are present.
4. **Meta tags** — Open Graph `product:price:amount`, schema.org `itemprop="price"`.
5. **Amazon-specific selectors** — `.priceToPay`, `.apex-pricetopay-value`, `corePriceDisplay_desktop_feature_div .a-offscreen`, and a generic `.a-offscreen` scan (skipping "was:" / "rrp:" / null values). Gated to `amazon.*` hosts because Amazon splits its price into sub-spans that would drop decimals.
6. **Generic CSS selectors** — elements with "price" in class/id, `[data-price]` attributes, `.product-price`, `.current-price`, `.selling-price`, etc.
7. **Microdata** — HTML5 microdata `itemprop="price"`.

Price strings handle both EU (`1.234,56`) and US (`1,234.56`) number formats, and a sanity cap of `< 100000` is applied to reject false positives.

If auto-detection fails, you can set a **custom CSS selector** when adding the product (`scraper_type: "custom"`).

## 🔎 Suggested Store Links (Alternate-Link Discovery)

For each product the app can find the **same product on other stores** in the same country. Found links are **candidates** — they are not attached to the product automatically; you review them in the product detail page and **approve** (✓ Add Link → the URL becomes a tracked alternative source, checked on every price run and plotted as one line per store on the graph) or **dismiss** (✖ Dismiss → never suggested again). A "New Links Found" dashboard stat and a per-product 🔗 badge track pending candidates.

Three matching methods are tried in order of reliability, and discovery stops as soon as at least one promising (cheaper) candidate is found:

1. **Product code** — EAN/GTIN/UPC barcode (check-digit validated) or Amazon ASIN, taken from the product page metadata (JSON-LD / meta tags / `content` attributes) or URL slug. For ASINs the candidate is built directly as `amazon.<country>/dp/<ASIN>` (no web search); otherwise the exact code is web-searched (DuckDuckGo + Bing). Store-internal IDs are rejected on purpose.
2. **Model number (MPN)** — letters+digits model strings from page metadata (e.g. `GSR18V-150`); the search result page must contain the exact model string.
3. **Product name** — a cleaned, store-brand-free product title from JSON-LD / meta tags / `<h1>`; the candidate page's name must be the same product (token recall / string similarity, with rejection of accessory or different-product names).

For methods 1 and 3, only results from **other stores with the same country domain suffix** are kept (e.g. a `.ro` product only finds other `.ro` sites) and price-comparison aggregators (price.ro, compari.ro, idealo, …) are never kept. Every candidate page is fetched and its price extracted (only candidates cheaper than the current price are suggested; up to `ALTERNATE_LINKS_MAX` at a time).

**How to trigger it:**
- **Manually** — "🔎 Find Links" button on the product detail page (`POST /api/products/{id}/find-alternates`).
- **On a schedule** — set `ALTERNATE_LINK_TIMES` (e.g. `02:00`); it runs for every enabled product where the per-product **"Auto find alternate links"** toggle is on (on by default).

> Discovery makes several real web requests per product and can take a couple of minutes — that's expected.

## 📝 License

MIT