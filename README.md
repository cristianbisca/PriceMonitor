# 📊 Price Monitor

A web application that monitors price changes for products across different e-commerce sites. It automatically checks prices at scheduled times, tracks history with interactive charts, and sends Telegram notifications when prices drop.

## ✨ Features

- **Product Management** — Add products by URL with auto-detect or custom CSS selector price extraction
- **Scheduled Price Checks** — Configurable check times (default: 9 AM and 2 PM)
- **Price History Charts** — Interactive Chart.js graphs showing price trends over time
- **Minimum Price Tracking** — Highlights the lowest price ever recorded for each product
- **Telegram Notifications** — Get alerted when prices drop, with special messages for new minimums
- **Dark Theme UI** — Modern responsive web interface
- **Docker Deployment** — Ready for Portainer stack deployment or docker-compose

## 🏗 Architecture

```
├── backend/
│   ├── api.py              # FastAPI REST API endpoints
│   ├── database.py         # SQLAlchemy database setup
│   ├── models.py           # Database models (Product, PriceEntry, etc.)
│   ├── main.py             # Application entry point
│   ├── price_checker.py    # Web scraping & price extraction logic
│   ├── graph_generator.py  # Matplotlib chart generation (PNG)
│   ├── telegram_notifier.py# Telegram bot notifications
│   ├── scheduler.py        # APScheduler for periodic checks
│   ├── requirements.txt    # Python dependencies
│   └── static/
│       └── index.html      # Frontend SPA (Chart.js, vanilla JS)
├── Dockerfile              # Multi-stage Docker build
├── docker-compose.yml      # Local development compose file
├── docker-stack.yml        # Portainer Swarm stack file
├── .env.example            # Environment variables template
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
   http://localhost:4300
   ```

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
| `TELEGRAM_BOT_TOKEN` | *(empty)* | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | *(empty)* | Your Telegram chat ID (comma-separated for multiple) |
| `PRICE_CHECK_TIMES` | `09:00,14:00` | Comma-separated check times in 24h format |
| `PORT` | `4300` | HTTP server port (external) |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## 📱 Telegram Setup

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the instructions to create a bot
3. Copy the bot token and set it in `TELEGRAM_BOT_TOKEN`
4. Start a chat with your bot and send any message
5. Search for **@userinfobot** to get your chat ID
6. Set your chat ID in `TELEGRAM_CHAT_ID`

### Notification Types

- **🔻 Price Drop Alert** — Sent when the current price is lower than the last checked price
- **🎉 NEW MINIMUM PRICE!** — Sent when a new all-time low price is detected
- **✅ First Price Recorded** — Sent on the first successful check for a new product

## 🐳 Portainer Deployment

### Option 1: Git Repository (Recommended)

1. Push your code to a GitHub repository
2. In Portainer, go to **Stacks → Add stack**
3. Fill in:
   - **Name:** `price-monitor`
   - **Type:** Swarm (or Standalone)
   - **Web/Git:** Select "Git repository"
   - **Repository URL:** Your GitHub repo URL
   - **Reference:** `main` (or your branch)
   - **Compose file:** `docker-stack.yml`
4. Add environment variables in the **Advanced** section or via env_file
5. Click **Deploy the stack**

### Option 2: Pre-built Image

1. Build and push the image to a registry:
   ```bash
   docker build -t your-registry/price-monitor:latest .
   docker push your-registry/price-monitor:latest
   ```
2. In `docker-stack.yml`, uncomment the `image:` line and set your registry URL
3. Deploy via Portainer as above

## 🔌 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/products` | List all products with stats |
| `POST` | `/api/products` | Add a new product |
| `GET` | `/api/products/{id}` | Get product details |
| `PUT` | `/api/products/{id}` | Update a product |
| `DELETE` | `/api/products/{id}` | Delete a product |
| `POST` | `/api/products/{id}/check` | Trigger immediate price check |
| `GET` | `/api/products/{id}/prices` | Get price history (newest first) |
| `GET` | `/api/products/{id}/prices/reverse` | Get price history (oldest first, for charts) |
| `GET` | `/api/products/{id}/chart` | Generate PNG chart image |
| `GET` | `/api/products/{id}/statistics` | Get price statistics |
| `POST` | `/api/check-all` | Check all enabled products |
| `POST` | `/api/telegram/test` | Send test Telegram notification |

### Example: Add a Product via API

```bash
curl -X POST http://localhost:4300/api/products \
  -H "Content-Type: application/json" \
  -d '{
    "name": "iPhone 15 Pro",
    "url": "https://example.com/iphone-15-pro",
    "currency": "RON",
    "scraper_type": "auto"
  }'
```

### Example: Check Price Now

```bash
curl -X POST http://localhost:4300/api/products/1/check
```

## 📸 Screenshots

The web interface features:
- Dashboard with overview statistics (total products, active checks, price drops)
- Product list table with current/min/avg prices and quick actions
- Detail view with interactive Chart.js price history graph
- Toggle switches to enable/disable monitoring per product
- Manual "Check Now" button for immediate price updates

## 🔧 Price Extraction Strategies

The auto-detect scraper tries these methods in order:

1. **JSON-LD structured data** — `schema.org/Product` with `offers.price`
2. **Meta tags** — Open Graph `product:price:amount`, schema.org `itemprop="price"`
3. **CSS selectors** — Elements with "price" in class/id, `[data-price]` attributes
4. **Microdata** — HTML5 microdata `itemprop="price"`

If auto-detection fails, you can set a custom CSS selector when adding the product.

## 📝 License

MIT