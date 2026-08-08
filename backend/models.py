from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, create_engine
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime, timezone

Base = declarative_base()


class Product(Base):
    """Represents a product being monitored."""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)  # User-defined or scraped product name
    url = Column(String(2048), nullable=False, unique=True, index=True)
    price_field = Column(String(100), nullable=True)  # Hint for which field contains price (e.g., "meta[property='product:price:amount']")
    currency = Column(String(10), default="RON")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Scraping configuration
    scraper_type = Column(String(50), default="auto")  # "auto", "custom", "amazon", "ecommerce", etc.
    custom_selector = Column(String(255), nullable=True)  # CSS selector for custom price extraction

    # Relationships
    price_history = relationship("PriceEntry", back_populates="product", cascade="all, delete-orphan")


class PriceEntry(Base):
    """Represents a single price check result."""
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String(10), default="RON")
    checked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    is_minimum = Column(Boolean, default=False)  # True if this is the new minimum price
    raw_html = Column(Text, nullable=True)  # Stored for debugging/retries

    # Relationships
    product = relationship("Product", back_populates="price_history")


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