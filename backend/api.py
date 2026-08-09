"""
FastAPI application with all API endpoints.
"""

import logging
from typing import List, Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db, init_db, engine, SessionLocal
from models import Product, PriceEntry, AppSettings, Base, User
from price_checker import check_product_price, run_all_price_checks
from graph_generator import generate_price_chart, get_price_statistics
from telegram_notifier import (
    test_notification,
    is_configured as telegram_is_configured,
    _log_configuration_status,
    verify_telegram_connection,
)
from scheduler import init_scheduler, shutdown_scheduler
from auth import AuthMiddleware, hash_password, generate_token, get_current_user

logger = logging.getLogger(__name__)

# Create the app
app = FastAPI(
    title="Price Monitor API",
    description="Monitor product prices and get notified on drops",
    version="1.0.0",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add authentication middleware (after CORS so headers are included)
app.add_middleware(AuthMiddleware)


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


class ProductUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    url: Optional[str] = Field(None, min_length=1, max_length=2048)
    currency: Optional[str] = Field(None, max_length=10)
    enabled: Optional[bool] = None
    scraper_type: Optional[str] = None
    custom_selector: Optional[str] = None


class ProductResponse(BaseModel):
    id: int
    user_id: int
    name: str
    url: str
    currency: str
    enabled: bool
    scraper_type: str
    custom_selector: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PriceEntryResponse(BaseModel):
    id: int
    product_id: int
    price: float
    currency: str
    checked_at: datetime
    is_minimum: bool

    class Config:
        from_attributes = True


class ProductDetailResponse(ProductResponse):
    current_price: Optional[float]
    min_price: Optional[float]
    max_price: Optional[float]
    avg_price: Optional[float]
    price_count: int

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
    # Create tables
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created/verified")

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
    """Check if authentication is enabled. Always required in multi-user mode."""
    return {"authRequired": True}


@app.post("/api/auth/register")
async def register(request: RegisterRequest):
    """Register a new user. Public endpoint."""
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


@app.get("/api/auth/me")
async def get_current_user_info(request: Request):
    """Get current user info."""
    user = get_user_from_request(request)
    return {
        "user_id": user["user_id"],
        "username": user["username"],
    }


# ============ Health ============

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

        # Get current price (latest entry)
        latest = (
            db.query(PriceEntry)
            .filter(PriceEntry.product_id == product.id)
            .order_by(PriceEntry.checked_at.desc())
            .first()
        )

        result.append(ProductDetailResponse(
            id=product.id,
            user_id=product.user_id,
            name=product.name,
            url=product.url,
            currency=product.currency,
            enabled=product.enabled,
            scraper_type=product.scraper_type,
            custom_selector=product.custom_selector,
            created_at=product.created_at,
            updated_at=product.updated_at,
            current_price=latest.price if latest else None,
            min_price=min_p,
            max_price=max_p,
            avg_price=avg_p,
            price_count=count,
        ))

    return result


@app.post("/api/products", response_model=ProductResponse, status_code=201)
async def create_product(product: ProductCreate, request: Request, db: Session = Depends(get_db)):
    """Add a new product to monitor (for current user)."""
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
    )
    db.add(db_product)
    db.commit()
    db.refresh(db_product)

    logger.info(f"Product added by user {user['username']}: {product.name}")
    return db_product


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

    latest = (
        db.query(PriceEntry)
        .filter(PriceEntry.product_id == product_id)
        .order_by(PriceEntry.checked_at.desc())
        .first()
    )

    return ProductDetailResponse(
        id=product.id,
        user_id=product.user_id,
        name=product.name,
        url=product.url,
        currency=product.currency,
        enabled=product.enabled,
        scraper_type=product.scraper_type,
        custom_selector=product.custom_selector,
        created_at=product.created_at,
        updated_at=product.updated_at,
        current_price=latest.price if latest else None,
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
    for field, value in update_data.items():
        setattr(db_product, field, value)

    db.commit()
    db.refresh(db_product)
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
            "checked_at": entry.checked_at.isoformat(),
        }
    else:
        return {
            "success": False,
            "message": "Could not extract price from the page",
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

    # Prepare data for chart generation
    price_data = [
        {
            "price": e.price,
            "checked_at": e.checked_at,
            "is_minimum": e.is_minimum,
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

    price_data = [
        {
            "price": e.price,
            "checked_at": e.checked_at,
            "is_minimum": e.is_minimum,
        }
        for e in entries
    ]

    stats = get_price_statistics(price_data)
    stats["currency"] = product.currency
    return stats


# ============ Telegram ============

@app.post("/api/telegram/test")
async def test_telegram():
    """Send a test Telegram notification."""
    if not telegram_is_configured():
        raise HTTPException(status_code=400, detail="Telegram is not configured")

    success = test_notification()
    return {
        "success": success,
        "message": "Test notification sent" if success else "Failed to send notification",
    }


# ============ Frontend ============

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the main frontend page."""
    try:
        with open("static/index.html", "r") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Price Monitor</h1><p>Frontend not found. Please build the static files.</p>",
            status_code=503,
        )


# Mount static files if directory exists
import os
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")