"""
FastAPI application with all API endpoints.
"""

import base64
import json
import logging
import re
import secrets
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from sqlalchemy import func


class DateTimeEncoder(json.JSONEncoder):
    """JSON encoder that ensures all datetimes include UTC timezone info."""
    def default(self, obj):
        if isinstance(obj, datetime):
            # Ensure aware datetime in UTC
            if obj.tzinfo is None:
                obj = obj.replace(tzinfo=timezone.utc)
            return obj.isoformat()
        return super().default(obj)

from database import get_db, init_db, engine, SessionLocal
from models import Product, PriceEntry, AppSettings, Base, User, LinkCandidate


class TelegramSettingsRequest(BaseModel):
    telegram_chat_id: Optional[str] = Field(None, min_length=1, max_length=100)
    telegram_notifications_enabled: Optional[bool] = None
from price_checker import check_product_price, run_all_price_checks, get_current_price
from alternate_links import find_alternate_links, METHOD_PRIORITY, _max_links
from graph_generator import generate_price_chart, get_price_statistics
from telegram_notifier import (
    test_notification,
    is_configured as telegram_is_configured,
    _log_configuration_status,
    verify_telegram_connection,
)
from scheduler import init_scheduler, shutdown_scheduler
from auth import AuthMiddleware, hash_password, generate_token, get_current_user
import backup

import os

logger = logging.getLogger(__name__)

# Create the app
app = FastAPI(
    title="Price Monitor API",
    description="Monitor product prices and get notified on drops",
    version="1.0.0",
)

# Middleware order matters: the LAST app.add_middleware() call becomes the
# OUTERMOST layer. Desired order (outer → inner):
#   SecurityHeadersMiddleware → CORSMiddleware (only if PM_CORS_ORIGINS is set) → AuthMiddleware
# Security headers must be outermost so they are present on every response,
# including the 401s AuthMiddleware returns without reaching the app.

# No 'unsafe-inline': the SPA's inline <style>/<script> tags are allowed via a
# per-request nonce that serve_frontend injects into index.html (and stores on
# request.state.csp_nonce). The chart libraries are self-hosted under
# /static/vendor/, so no external script host is allowed.
CSP_HTML_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self' 'nonce-{nonce}'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
# Same policy for non-HTML responses (API JSON, static assets) — no nonce.
CSP_DEFAULT = re.sub(r"\s*'nonce-\{nonce\}'", "", CSP_HTML_TEMPLATE)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds Content-Security-Policy and anti-clickjacking headers to all responses."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        response = await call_next(request)
        nonce = getattr(request.state, "csp_nonce", None)
        response.headers["Content-Security-Policy"] = (
            CSP_HTML_TEMPLATE.format(nonce=nonce) if nonce else CSP_DEFAULT
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


app.add_middleware(AuthMiddleware)

# CORS only for explicitly allowed origins (PM_CORS_ORIGINS, comma-separated).
# The SPA is served same-origin, so the default is no CORS headers at all —
# a wildcard origin with credentials would let any site read the API responses.
cors_origins = [o.strip() for o in os.getenv("PM_CORS_ORIGINS", "").split(",") if o.strip()]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Added last → outermost (see note above)
app.add_middleware(SecurityHeadersMiddleware)


def get_user_from_request(request: Request) -> dict:
    """Extract current user from request state (set by middleware)."""
    if not hasattr(request.state, "user") or not request.state.user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return request.state.user


# ============ Pydantic Models ============

class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Product name")
    url: str = Field(..., min_length=1, max_length=2048, description="Product URL")
    currency: str = Field(default="RON", max_length=10, description="Currency code")
    scraper_type: str = Field(default="auto", description="Scraper type: auto or custom")
    custom_selector: Optional[str] = Field(None, max_length=255, description="CSS selector for price")
    alternative_urls: Optional[List[str]] = Field(None, description="Alternative product URLs to check as well")
    auto_alternate_links: bool = Field(default=True, description="Allow the scheduler to auto-discover alternative links")


class ProductUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    url: Optional[str] = Field(None, min_length=1, max_length=2048)
    currency: Optional[str] = Field(None, max_length=10)
    enabled: Optional[bool] = None
    scraper_type: Optional[str] = None
    custom_selector: Optional[str] = None
    alternative_urls: Optional[List[str]] = Field(None, description="Alternative product URLs to check as well")
    auto_alternate_links: Optional[bool] = Field(None, description="Allow the scheduler to auto-discover alternative links")


class ProductResponse(BaseModel):
    id: int
    user_id: int
    name: str
    url: str
    currency: str
    enabled: bool
    scraper_type: str
    custom_selector: Optional[str]
    alternative_urls: List[str] = []
    auto_alternate_links: bool = True
    candidate_count: int = 0  # Pending (unapproved/undismissed) candidate links awaiting review
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @field_validator("alternative_urls", mode="before")
    @classmethod
    def parse_alternative_urls(cls, v):
        """Parse the JSON-encoded alternative URLs stored in the DB into a list."""
        if not v:
            return []
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                return []
        return v


def _parse_alternative_urls(raw) -> List[str]:
    """Parse the JSON-encoded alternative URLs stored in the DB into a list."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return [u for u in parsed if isinstance(u, str)] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(raw, list):
        return [u for u in raw if isinstance(u, str)]
    return []


def _pending_candidate_count(db: Session, product_id: int, current_price: Optional[float] = None) -> int:
    """Number of pending candidates that the review panel would show for this product.

    Counts only candidates cheaper than the current price (when one is known), so the
    badge/stat always matches what the candidate list actually displays.
    """
    query = db.query(func.count(LinkCandidate.id)).filter(
        LinkCandidate.product_id == product_id,
        LinkCandidate.status == "pending",
    )
    if current_price is not None:
        query = query.filter(LinkCandidate.price < current_price)
    return min(query.scalar() or 0, max(1, _max_links()))


def _extract_main_domain(url: str) -> str:
    """Extract the main domain from a URL (e.g., 'https://www.notino.ro/x' -> 'notino.ro')."""
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        # Strip leading "www." for a cleaner label
        if host.startswith("www."):
            host = host[4:]
        return host or url
    except Exception:
        return url


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime has UTC timezone info.

    SQLAlchemy with SQLite strips tzinfo when reading back, returning naive datetimes.
    Since we always store UTC timestamps, treat naive ones as UTC so the frontend
    can display them correctly in the user's local timezone.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class PriceEntryResponse(BaseModel):
    id: int
    product_id: int
    price: float
    currency: str
    checked_at: datetime
    is_minimum: bool
    source: Optional[str] = None  # Main domain of the price source (e.g., "notino.ro")

    model_config = {"from_attributes": True}

    @field_validator("checked_at", mode="before")
    @classmethod
    def ensure_checked_at_utc(cls, v):
        return _ensure_utc(v)


class ProductDetailResponse(ProductResponse):
    current_price: Optional[float]
    min_price: Optional[float]
    max_price: Optional[float]
    avg_price: Optional[float]
    price_count: int

    class Config:
        from_attributes = True


class ProductCreateResponse(ProductResponse):
    """Product response that also reports the result of the initial price check."""
    initial_check_success: Optional[bool] = None
    initial_check_price: Optional[float] = None
    initial_check_message: Optional[str] = None

    class Config:
        from_attributes = True


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=4, max_length=255)


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


# ============ Lifecycle ============

@app.on_event("startup")
async def startup():
    """Initialize database and scheduler on startup."""
    import shutil
    from pathlib import Path
    from sqlalchemy import inspect, text

    # ── One-shot restore from Dropbox (must happen BEFORE the DB is touched) ──
    restore_result = None
    if backup.RESTORE_LATEST_BACKUP:
        logger.info("RESTORE_LATEST_BACKUP is set - restoring database from the newest Dropbox backup...")
        try:
            restore_result = backup.restore_latest_backup()
        except Exception as e:
            logger.error(f"Startup restore FAILED: {e} - the app will not start. "
                         f"Fix the issue or set RESTORE_LATEST_BACKUP=false.")
            raise
        if restore_result:
            logger.info(
                f"Database restored from {restore_result['source']} "
                f"({restore_result['user_count']} users, {restore_result['product_count']} products)"
                + (f"; safety copy: {restore_result['safety_backup']}" if restore_result.get("safety_backup") else "")
            )

    def _init_schema():
        # Create tables
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables created/verified")

        # Auto-migrate: add telegram columns to users table if missing
        inspector = inspect(engine)
        if "users" in inspector.get_table_names():
            existing_cols = {col["name"] for col in inspector.get_columns("users")}
            with engine.begin() as conn:
                if "telegram_chat_id" not in existing_cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN telegram_chat_id VARCHAR(100)"))
                    logger.info("Added column telegram_chat_id to users table")
                if "telegram_notifications_enabled" not in existing_cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN telegram_notifications_enabled BOOLEAN DEFAULT 0"))
                    logger.info("Added column telegram_notifications_enabled to users table")

        # Auto-migrate: add alternative_urls column to products table if missing
        if "products" in inspector.get_table_names():
            product_cols = {col["name"] for col in inspector.get_columns("products")}
            with engine.begin() as conn:
                if "alternative_urls" not in product_cols:
                    conn.execute(text("ALTER TABLE products ADD COLUMN alternative_urls TEXT"))
                    logger.info("Added column alternative_urls to products table")
                if "auto_alternate_links" not in product_cols:
                    conn.execute(text("ALTER TABLE products ADD COLUMN auto_alternate_links BOOLEAN DEFAULT 1"))
                    logger.info("Added column auto_alternate_links to products table")

        # Auto-migrate: add source column to price_history table if missing
        if "price_history" in inspector.get_table_names():
            history_cols = {col["name"] for col in inspector.get_columns("price_history")}
            with engine.begin() as conn:
                if "source" not in history_cols:
                    conn.execute(text("ALTER TABLE price_history ADD COLUMN source VARCHAR(255)"))
                    logger.info("Added column source to price_history table")
                if "check_cycle" not in history_cols:
                    conn.execute(text("ALTER TABLE price_history ADD COLUMN check_cycle DATETIME"))
                    logger.info("Added column check_cycle to price_history table")

    if restore_result:
        # Schema init after a restore: roll back to the pre-restore safety copy
        # (and retry) if the restored file turns out to be unusable
        try:
            _init_schema()
        except Exception:
            safety_path = Path(restore_result["safety_backup"]) if restore_result.get("safety_backup") else None
            if safety_path and safety_path.exists():
                logger.error(f"Schema init failed after restore - rolling back to safety copy {safety_path}")
                engine.dispose()
                shutil.copy2(safety_path, backup.get_db_path())
                try:
                    _init_schema()
                except Exception:
                    logger.critical("Rollback to the pre-restore safety copy failed - aborting startup")
                    raise
            else:
                raise
    else:
        _init_schema()

    # Log Telegram configuration status
    _log_configuration_status()

    # Verify Telegram bot token by calling getMe API
    if telegram_is_configured():
        logger.info("Verifying Telegram connection...")
        verify_result = verify_telegram_connection()
        if verify_result["token_valid"]:
            bot_info = verify_result["bot_info"]
            logger.info(
                f"Telegram bot connected: {bot_info['first_name']} "
                f"(@{bot_info.get('username', 'no username')}, ID: {bot_info['id']})"
            )
        else:
            logger.warning(
                f"Telegram connection verification failed: {verify_result.get('error')}. "
                f"Notifications may not work. Check your TELEGRAM_BOT_TOKEN."
            )
    else:
        logger.warning(
            "Telegram notifications are NOT configured. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables to enable them."
        )

    # Initialize scheduler
    init_scheduler()
    logger.info("Application started successfully")


@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown."""
    shutdown_scheduler()
    logger.info("Application shut down")


# ============ Authentication ============

@app.get("/api/auth/status")
async def auth_status():
    """Check if authentication is enabled and whether sign-up is allowed."""
    signup_enabled = os.getenv("ENABLE_SIGNUP", "true").lower() not in ("false", "0", "")
    return {
        "authRequired": True,
        "signupEnabled": signup_enabled,
    }


@app.post("/api/auth/register")
async def register(request: RegisterRequest):
    """Register a new user. Public endpoint. Can be disabled via ENABLE_SIGNUP env var."""
    # Check if sign-up is enabled
    signup_enabled = os.getenv("ENABLE_SIGNUP", "true").lower() not in ("false", "0", "")
    if not signup_enabled:
        return JSONResponse(
            status_code=403,
            content={"error": "Sign up is currently disabled"},
        )

    db = SessionLocal()
    try:
        # Check if username already exists
        existing_user = db.query(User).filter(
            User.username == request.username.lower().strip()
        ).first()
        if existing_user:
            return JSONResponse(
                status_code=409,
                content={"error": "Username already taken"},
            )

        # Validate username (alphanumeric and underscores only)
        import re
        if not re.match(r'^[a-zA-Z0-9_]+$', request.username):
            return JSONResponse(
                status_code=400,
                content={"error": "Username can only contain letters, numbers, and underscores"},
            )

        # Create new user with hashed password
        new_user = User(
            username=request.username.lower().strip(),
            password_hash=hash_password(request.password),
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        logger.info(f"User registered: {new_user.username}")

        # Generate token for auto-login after registration
        token = generate_token(new_user.id, new_user.username)
        return {"success": True, "token": token}
    finally:
        db.close()


@app.post("/api/auth/login")
async def login(request: LoginRequest):
    """Login endpoint - returns a token for session-based auth. Public endpoint."""
    if not request.username or not request.password:
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid credentials"},
        )

    db = SessionLocal()
    try:
        user = db.query(User).filter(
            User.username == request.username.lower().strip()
        ).first()

        if user and hash_password(request.password) == user.password_hash:
            token = generate_token(user.id, user.username)
            return {"success": True, "token": token}
        else:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid credentials"},
            )
    finally:
        db.close()


@app.post("/api/auth/logout")
async def logout():
    """Logout endpoint. Client-side token is invalidated by removing it from localStorage."""
    return {"success": True}


@app.get("/api/auth/me")
async def get_current_user_info(request: Request):
    """Get current user info."""
    user = get_user_from_request(request)
    return {
        "user_id": user["user_id"],
        "username": user["username"],
    }


# ============ Health ============

@app.get("/api/config/timezone")
async def get_timezone_config():
    """Get the configured timezone for frontend display."""
    tz_name = os.getenv("TZ", "Europe/Bucharest")
    return {"timezone": tz_name}


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "telegram_configured": telegram_is_configured(),
        "timestamp": datetime.utcnow().isoformat(),
    }


# ============ Products CRUD (per-user) ============

@app.get("/api/products", response_model=List[ProductDetailResponse])
async def list_products(request: Request, db: Session = Depends(get_db)):
    """List all products for the current user with their price statistics."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    products = db.query(Product).filter(
        Product.user_id == user_id
    ).order_by(Product.created_at.desc()).all()

    result = []
    for product in products:
        # Get price stats
        stats = (
            db.query(PriceEntry)
            .filter(PriceEntry.product_id == product.id)
            .with_entities(
                func.min(PriceEntry.price),
                func.max(PriceEntry.price),
                func.avg(PriceEntry.price),
                func.count(PriceEntry.id),
            )
            .first()
        )

        min_p, max_p, avg_p, count = stats if stats else (None, None, None, 0)

        # Current price = minimum over all sources checked in the latest check cycle
        current_price = get_current_price(db, product.id)
        candidate_count = _pending_candidate_count(db, product.id, current_price)

        result.append(ProductDetailResponse(
            id=product.id,
            user_id=product.user_id,
            name=product.name,
            url=product.url,
            currency=product.currency,
            enabled=product.enabled,
            scraper_type=product.scraper_type,
            custom_selector=product.custom_selector,
            alternative_urls=_parse_alternative_urls(product.alternative_urls),
            auto_alternate_links=bool(product.auto_alternate_links),
            candidate_count=candidate_count,
            created_at=product.created_at,
            updated_at=product.updated_at,
            current_price=current_price,
            min_price=min_p,
            max_price=max_p,
            avg_price=avg_p,
            price_count=count,
        ))

    return result


@app.post("/api/products", response_model=ProductCreateResponse, status_code=201)
async def create_product(product: ProductCreate, request: Request, db: Session = Depends(get_db)):
    """Add a new product to monitor (for current user).

    After the product is created we immediately run an initial price check so the
    user has a first data point without waiting for the next scheduled run or having
    to click "Check Now" manually. A failed extraction does not fail the request —
    the product is still created and reported via `initial_check_success`.
    """
    user = get_user_from_request(request)
    user_id = user["user_id"]

    # Check for duplicate URL within this user's products
    existing = db.query(Product).filter(
        Product.user_id == user_id,
        Product.url == product.url
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="A product with this URL already exists")

    db_product = Product(
        user_id=user_id,
        name=product.name,
        url=product.url,
        currency=product.currency,
        scraper_type=product.scraper_type,
        custom_selector=product.custom_selector,
        alternative_urls=json.dumps(product.alternative_urls) if product.alternative_urls else None,
        auto_alternate_links=product.auto_alternate_links,
    )
    db.add(db_product)
    db.commit()
    db.refresh(db_product)

    logger.info(f"Product added by user {user['username']}: {product.name}")

    # Perform the first price check right away (best-effort; never blocks product creation).
    entry = check_product_price(db_product.id)

    response = ProductCreateResponse(
        id=db_product.id,
        user_id=db_product.user_id,
        name=db_product.name,
        url=db_product.url,
        currency=db_product.currency,
        enabled=db_product.enabled,
        scraper_type=db_product.scraper_type,
        custom_selector=db_product.custom_selector,
        alternative_urls=_parse_alternative_urls(db_product.alternative_urls),
        auto_alternate_links=bool(db_product.auto_alternate_links),
        created_at=db_product.created_at,
        updated_at=db_product.updated_at,
    )
    if entry:
        response.initial_check_success = True
        response.initial_check_price = entry.price
        logger.info(f"Initial price check for {product.name}: {entry.price} {entry.currency}")
    else:
        response.initial_check_success = False
        response.initial_check_message = "Could not extract price from the page yet. Try 'Check Now'."
        logger.warning(f"Initial price check failed for {product.name} ({product.url})")

    return response


@app.get("/api/products/{product_id}", response_model=ProductDetailResponse)
async def get_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    """Get a single product with statistics (only if it belongs to current user)."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == user_id
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    stats = (
        db.query(PriceEntry)
        .filter(PriceEntry.product_id == product_id)
        .with_entities(
            func.min(PriceEntry.price),
            func.max(PriceEntry.price),
            func.avg(PriceEntry.price),
            func.count(PriceEntry.id),
        )
        .first()
    )
    min_p, max_p, avg_p, count = stats if stats else (None, None, None, 0)

    # Current price = minimum over all sources checked in the latest check cycle
    current_price = get_current_price(db, product_id)
    candidate_count = _pending_candidate_count(db, product_id, current_price)

    return ProductDetailResponse(
        id=product.id,
        user_id=product.user_id,
        name=product.name,
        url=product.url,
        currency=product.currency,
        enabled=product.enabled,
        scraper_type=product.scraper_type,
        custom_selector=product.custom_selector,
        alternative_urls=_parse_alternative_urls(product.alternative_urls),
        auto_alternate_links=bool(product.auto_alternate_links),
        candidate_count=candidate_count,
        created_at=product.created_at,
        updated_at=product.updated_at,
        current_price=current_price,
        min_price=min_p,
        max_price=max_p,
        avg_price=avg_p,
        price_count=count,
    )


@app.put("/api/products/{product_id}", response_model=ProductResponse)
async def update_product(product_id: int, product: ProductUpdate, request: Request, db: Session = Depends(get_db)):
    """Update a product (only if it belongs to current user)."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    db_product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == user_id
    ).first()
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    update_data = product.model_dump(exclude_unset=True)

    # If the URL is being changed, make sure it doesn't collide with another of this user's products
    if "url" in update_data and update_data["url"] != db_product.url:
        existing = db.query(Product).filter(
            Product.user_id == user_id,
            Product.url == update_data["url"],
            Product.id != product_id,
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="A product with this URL already exists")

    # Validate alternative URLs (must be valid http(s) URLs and not duplicate the main URL or each other)
    if "alternative_urls" in update_data:
        alt_urls = [u.strip() for u in (update_data["alternative_urls"] or []) if u and u.strip()]
        seen = set()
        for alt_url in alt_urls:
            try:
                parsed = urlparse(alt_url)
            except Exception:
                raise HTTPException(status_code=400, detail=f"Invalid alternative URL: {alt_url}")
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise HTTPException(status_code=400, detail=f"Alternative URL must be a valid http(s) URL: {alt_url}")
            normalized = alt_url.rstrip("/")
            if normalized == db_product.url.rstrip("/"):
                raise HTTPException(status_code=400, detail="An alternative URL is the same as the main product URL")
            if normalized in seen:
                raise HTTPException(status_code=400, detail=f"Duplicate alternative URL: {alt_url}")
            seen.add(normalized)
        update_data["alternative_urls"] = json.dumps(alt_urls) if alt_urls else None

    for field, value in update_data.items():
        setattr(db_product, field, value)

    db.commit()
    db.refresh(db_product)
    # Not a mapped column: expose the pending-candidate count for this response
    # (from_attributes has no ORM attribute for it, so attach it here).
    db_product.candidate_count = _pending_candidate_count(
        db, db_product.id, get_current_price(db, db_product.id)
    )
    return db_product


@app.delete("/api/products/{product_id}", status_code=204)
async def delete_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a product and all its price history (only if it belongs to current user)."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    db_product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == user_id
    ).first()
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    db.delete(db_product)
    db.commit()
    logger.info(f"Product deleted by user {user['username']}: {db_product.name}")


# ============ Price History (per-user) ============

@app.get("/api/products/{product_id}/prices", response_model=List[PriceEntryResponse])
async def get_price_history(
    product_id: int,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Get price history for a product (only if it belongs to current user)."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == user_id
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    entries = (
        db.query(PriceEntry)
        .filter(PriceEntry.product_id == product_id)
        .order_by(PriceEntry.checked_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return entries


@app.get("/api/products/{product_id}/prices/reverse", response_model=List[PriceEntryResponse])
async def get_price_history_asc(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Get price history in ascending order (for charts)."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == user_id
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    entries = (
        db.query(PriceEntry)
        .filter(PriceEntry.product_id == product_id)
        .order_by(PriceEntry.checked_at.asc())
        .all()
    )

    return entries


# ============ Price Check Actions (per-user) ============

@app.post("/api/products/{product_id}/check")
async def check_product_now(product_id: int, request: Request, db: Session = Depends(get_db)):
    """Manually trigger a price check for a product (only if it belongs to current user)."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == user_id
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    entry = check_product_price(product_id)

    if entry:
        return {
            "success": True,
            "price": entry.price,
            "currency": entry.currency,
            "is_minimum": entry.is_minimum,
            "source": entry.source,
            "checked_at": entry.checked_at.isoformat(),
        }
    else:
        return {
            "success": False,
            "message": "Could not extract price from the page",
        }


@app.post("/api/products/{product_id}/find-alternates")
def find_alternates_now(product_id: int, request: Request, db: Session = Depends(get_db)):
    """Manually trigger alternate-link discovery for a product (only if it belongs to current user).

    Defined as a sync def on purpose: the discovery fetches several web pages and can
    take a couple of minutes, so FastAPI runs it in the worker thread pool instead of
    blocking the event loop.
    """
    user = get_user_from_request(request)
    user_id = user["user_id"]

    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == user_id
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    return find_alternate_links(product_id)


# ============ Candidate links (per-user) ============

class LinkCandidateResponse(BaseModel):
    id: int
    url: str
    price: Optional[float]
    match_method: Optional[str]  # "code" / "model" / "name" / "keyword"
    status: str
    savings: Optional[float] = None  # current_price - candidate.price
    found_at: datetime
    decided_at: Optional[datetime]

    class Config:
        from_attributes = True

    @field_validator("found_at", "decided_at", mode="before")
    @classmethod
    def _utc(cls, v):
        return _ensure_utc(v)


def _candidate_sort_key(candidate: LinkCandidate):
    """Rank by match reliability (code < model < name), then by price (cheapest first)."""
    method_rank = METHOD_PRIORITY.get(candidate.match_method, 99)
    price = candidate.price if candidate.price is not None else float("inf")
    return (method_rank, price)


def _load_owned_product(product_id: int, request: Request, db: Session) -> Product:
    user = get_user_from_request(request)
    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == user["user_id"]
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@app.get("/api/products/{product_id}/candidates", response_model=List[LinkCandidateResponse])
async def list_candidate_links(product_id: int, request: Request, db: Session = Depends(get_db)):
    """List pending candidate links for a product (cheaper than the current price).

    Ranked by match reliability then by price; at most ALTERNATE_LINKS_MAX are
    returned so the UI always shows a short, prioritised review list.
    """
    product = _load_owned_product(product_id, request, db)
    current_price = get_current_price(db, product_id)

    pending = (
        db.query(LinkCandidate)
        .filter(
            LinkCandidate.product_id == product_id,
            LinkCandidate.status == "pending",
        )
        .all()
    )
    # Only candidates that still beat the current price (or all, when there is no
    # recorded price yet to compare against).
    if current_price is not None:
        pending = [c for c in pending if c.price is not None and c.price < current_price]

    pending.sort(key=_candidate_sort_key)

    limit = max(1, _max_links())
    result = []
    for candidate in pending[:limit]:
        response = LinkCandidateResponse.model_validate(candidate)
        if current_price is not None and candidate.price is not None:
            response.savings = round(current_price - candidate.price, 2)
        result.append(response)
    return result


@app.post("/api/products/{product_id}/candidates/{candidate_id}/approve")
async def approve_candidate_link(
    product_id: int,
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Approve a candidate: mark it approved and attach its URL to the product.

    The approved URL becomes a real alternate price source checked on every run.
    """
    product = _load_owned_product(product_id, request, db)
    candidate = db.query(LinkCandidate).filter(
        LinkCandidate.id == candidate_id,
        LinkCandidate.product_id == product_id,
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if candidate.status != "pending":
        raise HTTPException(status_code=400, detail=f"Candidate is not pending (status: {candidate.status})")

    # Attach the URL to the product (deduplicated, skipping the main URL)
    urls = _parse_alternative_urls(product.alternative_urls)
    candidate_url = candidate.url.rstrip("/")
    if candidate.url != product.url and candidate_url not in {u.rstrip("/") for u in urls}:
        urls.append(candidate.url)
        product.alternative_urls = json.dumps(urls)

    candidate.status = "approved"
    candidate.decided_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(product)
    logger.info(f"User {get_user_from_request(request)['username']} approved candidate {candidate.url} for product {product_id}")

    return {
        "success": True,
        "candidate": LinkCandidateResponse.model_validate(candidate),
        "alternative_urls": urls,
    }


@app.post("/api/products/{product_id}/candidates/{candidate_id}/dismiss")
async def dismiss_candidate_link(
    product_id: int,
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Dismiss a candidate: mark it dismissed so discovery never suggests it again."""
    product = _load_owned_product(product_id, request, db)
    candidate = db.query(LinkCandidate).filter(
        LinkCandidate.id == candidate_id,
        LinkCandidate.product_id == product_id,
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if candidate.status != "pending":
        raise HTTPException(status_code=400, detail=f"Candidate is not pending (status: {candidate.status})")

    candidate.status = "dismissed"
    candidate.decided_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(candidate)
    logger.info(f"User {get_user_from_request(request)['username']} dismissed candidate {candidate.url} for product {product_id}")

    return {
        "success": True,
        "candidate": LinkCandidateResponse.model_validate(candidate),
    }


@app.post("/api/check-all")
async def check_all_products(request: Request, db: Session = Depends(get_db)):
    """Manually trigger price checks for all enabled products of current user."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    # Get only this user's enabled products
    products = db.query(Product).filter(
        Product.user_id == user_id,
        Product.enabled == True
    ).all()

    results = []
    for product in products:
        entry = check_product_price(product.id)
        results.append({
            "product_id": product.id,
            "name": product.name,
            "success": entry is not None,
            "price": entry.price if entry else None,
            "is_minimum": entry.is_minimum if entry else False,
        })

    return {
        "total": len(results),
        "successful": sum(1 for r in results if r["success"]),
        "failed": sum(1 for r in results if not r["success"]),
        "results": results,
    }


# ============ Charts (per-user) ============

@app.get("/api/products/{product_id}/chart")
async def get_price_chart(product_id: int, request: Request, db: Session = Depends(get_db)):
    """Generate a price history chart for a product."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == user_id
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    entries = (
        db.query(PriceEntry)
        .filter(PriceEntry.product_id == product_id)
        .order_by(PriceEntry.checked_at.asc())
        .all()
    )

    if not entries:
        raise HTTPException(status_code=404, detail="No price history available")

    # Prepare data for chart generation (include the source domain so each price
    # source can be plotted as its own line on the graph)
    main_domain = _extract_main_domain(product.url)
    price_data = [
        {
            "price": e.price,
            "checked_at": e.checked_at,
            "is_minimum": e.is_minimum,
            "source": e.source or main_domain,
        }
        for e in entries
    ]

    chart_base64 = generate_price_chart(
        price_history=price_data,
        product_name=product.name,
        currency=product.currency,
    )

    if not chart_base64:
        raise HTTPException(status_code=500, detail="Failed to generate chart")

    # Return PNG image
    import base64
    chart_bytes = base64.b64decode(chart_base64)
    return Response(content=chart_bytes, media_type="image/png")


@app.get("/api/products/{product_id}/statistics")
async def get_price_statistics_endpoint(product_id: int, request: Request, db: Session = Depends(get_db)):
    """Get price statistics for a product."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == user_id
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    entries = (
        db.query(PriceEntry)
        .filter(PriceEntry.product_id == product_id)
        .order_by(PriceEntry.checked_at.asc())
        .all()
    )

    if not entries:
        return {"message": "No price history available"}

    main_domain = _extract_main_domain(product.url)
    price_data = [
        {
            "price": e.price,
            "checked_at": e.checked_at,
            "is_minimum": e.is_minimum,
            "source": e.source or main_domain,
            "check_cycle": e.check_cycle,
        }
        for e in entries
    ]

    stats = get_price_statistics(price_data)
    stats["currency"] = product.currency
    return stats


# ============ Telegram (Per-User Settings) ============

@app.get("/api/telegram/settings")
async def get_telegram_settings(request: Request):
    """Get current user's Telegram notification settings."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    db = SessionLocal()
    try:
        user_obj = db.query(User).filter(User.id == user_id).first()
        return {
            "telegram_chat_id": user_obj.telegram_chat_id if user_obj else "",
            "telegram_notifications_enabled": user_obj.telegram_notifications_enabled if user_obj else False,
            "bot_configured": telegram_is_configured(),
        }
    finally:
        db.close()


@app.put("/api/telegram/settings")
async def update_telegram_settings(request: Request):
    """Update current user's Telegram notification settings."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    db = SessionLocal()
    try:
        user_obj = db.query(User).filter(User.id == user_id).first()
        if not user_obj:
            raise HTTPException(status_code=404, detail="User not found")

        if "telegram_chat_id" in body:
            chat_id = body["telegram_chat_id"]
            # Validate chat ID format (numeric, may start with - for groups)
            if chat_id is not None and chat_id != "":
                cleaned = chat_id.strip()
                if not cleaned.lstrip("-").isdigit():
                    raise HTTPException(
                        status_code=400,
                        detail="Chat ID must be a numeric value (e.g., 123456789 or -1001234567890)"
                    )
                user_obj.telegram_chat_id = cleaned
            else:
                user_obj.telegram_chat_id = None

        if "telegram_notifications_enabled" in body:
            user_obj.telegram_notifications_enabled = bool(body["telegram_notifications_enabled"])

        db.commit()
        db.refresh(user_obj)

        return {
            "success": True,
            "telegram_chat_id": user_obj.telegram_chat_id,
            "telegram_notifications_enabled": user_obj.telegram_notifications_enabled,
        }
    finally:
        db.close()


@app.post("/api/telegram/test")
async def test_telegram(request: Request):
    """Send a test Telegram notification to the current user's chat ID."""
    user = get_user_from_request(request)
    user_id = user["user_id"]

    db = SessionLocal()
    try:
        user_obj = db.query(User).filter(User.id == user_id).first()
        if not user_obj or not user_obj.telegram_chat_id:
            raise HTTPException(
                status_code=400,
                detail="Please set your Telegram Chat ID first in the settings"
            )

        if not telegram_is_configured():
            raise HTTPException(
                status_code=503,
                detail="Telegram bot is not configured by the administrator (TELEGRAM_BOT_TOKEN missing)"
            )

        success = test_notification(chat_id=user_obj.telegram_chat_id)
        return {
            "success": success,
            "message": "Test notification sent to your Telegram" if success else "Failed to send notification. Check your Chat ID.",
        }
    finally:
        db.close()


# ============ Backup & Restore (Dropbox) ============

@app.get("/api/backup/status")
async def backup_status():
    """Backup feature status and configuration (no secrets included)."""
    return {"success": True, "data": backup.get_status()}


@app.post("/api/backup/run")
def run_backup(request: Request):
    """Trigger a database backup now (consistent local snapshot + Dropbox upload)."""
    get_user_from_request(request)
    try:
        result = backup.perform_backup()
        if result:
            message = "Backup completed" + ("" if result["dropbox"] else " (local only - Dropbox not configured)")
        else:
            message = "Backup is disabled (BACKUP_ENABLED=false)"
        return {"success": True, "data": result, "message": message}
    except Exception as e:
        logger.error(f"Manual backup failed: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@app.get("/api/backup/list")
def list_backups_endpoint(request: Request):
    """List local and Dropbox backups (newest first)."""
    get_user_from_request(request)
    try:
        return {"success": True, "data": backup.list_backups()}
    except Exception as e:
        logger.error(f"Failed to list backups: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


# ============ Frontend ============

@app.get("/", response_class=HTMLResponse)
async def serve_frontend(request: Request):
    """Serve the main frontend page."""
    try:
        with open("static/index.html", "r") as f:
            html = f.read()
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Price Monitor</h1><p>Frontend not found. Please build the static files.</p>",
            status_code=503,
        )
    # Per-request CSP nonce: injected into the one inline <style> and the one
    # bare <script> tag so the policy can forbid 'unsafe-inline' (the
    # SecurityHeadersMiddleware reads the nonce from request.state).
    nonce = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    request.state.csp_nonce = nonce
    html = html.replace("<style>", f'<style nonce="{nonce}">', 1)
    html = html.replace("<script>", f'<script nonce="{nonce}">', 1)
    return html


# Mount static files if directory exists
import os
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")