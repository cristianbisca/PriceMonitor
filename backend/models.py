from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, create_engine, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime, timezone

Base = declarative_base()


class User(Base):
    """Represents a registered user."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)  # SHA-256 hash of the password
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Telegram notification settings (per-user)
    telegram_chat_id = Column(String(100), nullable=True)  # User's Telegram chat/group ID
    telegram_notifications_enabled = Column(Boolean, default=False)  # Whether user wants notifications

    # Relationships
    products = relationship("Product", back_populates="user", cascade="all, delete-orphan")


class Product(Base):
    """Represents a product being monitored."""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)  # User-defined or scraped product name
    url = Column(String(2048), nullable=False, index=True)
    price_field = Column(String(100), nullable=True)  # Hint for which field contains price (e.g., "meta[property='product:price:amount']")
    currency = Column(String(10), default="RON")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Scraping configuration
    scraper_type = Column(String(50), default="auto")  # "auto", "custom", "amazon", "ecommerce", etc.
    custom_selector = Column(String(255), nullable=True)  # CSS selector for custom price extraction

    # Alternative price source URLs (JSON array of strings, e.g., '["https://...", "..."]')
    alternative_urls = Column(Text, nullable=True)

    # Whether the scheduled alternate-link discovery may scan this product and propose candidate links
    auto_alternate_links = Column(Boolean, default=True)

    # Relationships
    user = relationship("User", back_populates="products")
    price_history = relationship("PriceEntry", back_populates="product", cascade="all, delete-orphan")
    link_candidates = relationship("LinkCandidate", back_populates="product", cascade="all, delete-orphan")

    # URL must be unique per user
    __table_args__ = (
        UniqueConstraint("user_id", "url", name="uq_user_url"),
    )


class PriceEntry(Base):
    """Represents a single price check result."""
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String(10), default="RON")
    checked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    is_minimum = Column(Boolean, default=False)  # True if this is the new minimum price
    source = Column(String(255), nullable=True)  # Main domain of the price source (e.g., "notino.ro")
    # All entries recorded during the same check run share this timestamp, grouping them
    # into a "check cycle". Current price = MIN(price) of the latest cycle. NULL on legacy rows.
    check_cycle = Column(DateTime, nullable=True, index=True)
    raw_html = Column(Text, nullable=True)  # Stored for debugging/retries

    # Relationships
    product = relationship("Product", back_populates="price_history")


class LinkCandidate(Base):
    """A same-product link proposed by the discovery engine, awaiting user review.

    Discovery no longer attaches links to the product directly: verified cheaper
    candidates are stored here with status "pending" and shown in the UI, where the
    user approves them (link is then appended to Product.alternative_urls) or
    dismisses them (URL is never suggested again for that product).
    """
    __tablename__ = "link_candidates"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    url = Column(String(2048), nullable=False)  # Candidate store URL (normalized, no trailing slash)
    price = Column(Float, nullable=True)  # Price found on the candidate page at discovery time
    match_method = Column(String(20), nullable=True)  # "code" (EAN/UPC/GTIN/ASIN), "model" (MPN/model no), "name"
    status = Column(String(20), nullable=False, default="pending", index=True)  # pending / approved / dismissed
    found_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    decided_at = Column(DateTime, nullable=True)  # Set when the user approves or dismisses

    # Relationships
    product = relationship("Product", back_populates="link_candidates")

    # One candidate per URL per product (re-runs refresh the existing row)
    __table_args__ = (
        UniqueConstraint("product_id", "url", name="uq_candidate_product_url"),
    )


class TelegramNotification(Base):
    """Tracks notification history to avoid duplicates."""
    __tablename__ = "telegram_notifications"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    entry_id = Column(Integer, ForeignKey("price_history.id", ondelete="CASCADE"), nullable=False)
    chat_id = Column(String(50), nullable=False)
    message_type = Column(String(20), nullable=False)  # "drop" or "minimum"
    sent_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    message_text = Column(Text, nullable=True)


class AppSettings(Base):
    """Application-wide settings."""
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)