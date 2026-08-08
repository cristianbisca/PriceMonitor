"""
Telegram notification service for price alerts.
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def is_configured() -> bool:
    """Check if Telegram notifications are configured."""
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_message(chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to a specific Telegram chat."""
    if not is_configured():
        logger.warning("Telegram not configured, skipping notification")
        return False

    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        logger.info(f"Telegram notification sent to {chat_id}")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send Telegram notification: {e}")
        return False


def send_price_drop_notification(
    product_name: str,
    product_url: str,
    old_price: float,
    new_price: float,
    currency: str,
    chat_id: Optional[str] = None,
) -> bool:
    """Send notification when price drops (but not a new minimum)."""
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not target_chat:
        return False

    drop_amount = old_price - new_price
    drop_percent = (drop_amount / old_price) * 100

    message = (
        f"🔻 <b>Price Drop Alert</b>\n\n"
        f"📦 <b>{product_name}</b>\n"
        f"💰 Price dropped from <b>{old_price:,.2f} {currency}</b> "
        f"to <b>{new_price:,.2f} {currency}</b>\n"
        f"📉 Savings: <b>{drop_amount:,.2f} {currency}</b> ({drop_percent:.1f}%)\n\n"
        f"<a href='{product_url}'>View Product</a>"
    )

    return send_message(target_chat, message)


def send_new_minimum_notification(
    product_name: str,
    product_url: str,
    old_min_price: float,
    new_price: float,
    currency: str,
    chat_id: Optional[str] = None,
) -> bool:
    """Send notification when a new minimum price is found."""
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not target_chat:
        return False

    drop_amount = old_min_price - new_price
    drop_percent = (drop_amount / old_min_price) * 100

    message = (
        f"🎉 <b>NEW MINIMUM PRICE!</b>\n\n"
        f"📦 <b>{product_name}</b>\n"
        f"💰 New lowest price: <b>{new_price:,.2f} {currency}</b>\n"
        f"📊 Previous minimum: <b>{old_min_price:,.2f} {currency}</b>\n"
        f"🔥 Drop from minimum: <b>{drop_amount:,.2f} {currency}</b> ({drop_percent:.1f}%)\n\n"
        f"<a href='{product_url}'>View Product</a>"
    )

    return send_message(target_chat, message)


def send_first_price_notification(
    product_name: str,
    product_url: str,
    price: float,
    currency: str,
    chat_id: Optional[str] = None,
) -> bool:
    """Send notification for the first price check of a new product."""
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not target_chat:
        return False

    message = (
        f"✅ <b>First Price Recorded</b>\n\n"
        f"📦 <b>{product_name}</b>\n"
        f"💰 Current price: <b>{price:,.2f} {currency}</b>\n\n"
        f"<a href='{product_url}'>View Product</a>"
    )

    return send_message(target_chat, message)


def send_startup_notification() -> bool:
    """Send a notification when the bot starts."""
    if not is_configured():
        return False

    message = (
        f"🤖 <b>Price Monitor Bot Started</b>\n\n"
        f"The price monitoring service has been started successfully.\n"
        f"I'll notify you when prices drop!"
    )

    return send_message(TELEGRAM_CHAT_ID, message)


def test_notification(chat_id: Optional[str] = None) -> bool:
    """Send a test notification."""
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not target_chat:
        return False

    message = (
        f"🧪 <b>Test Notification</b>\n\n"
        f"This is a test from Price Monitor Bot.\n"
        f"If you see this, notifications are working correctly!"
    )

    return send_message(target_chat, message)