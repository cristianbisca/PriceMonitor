"""
Scheduler service for periodic price checks.
Uses APScheduler to run price checks at configured times.
"""

import logging
import os
from typing import List

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from database import SessionLocal
from models import Product, PriceEntry
from price_checker import check_product_price, run_all_price_checks
from telegram_notifier import (
    is_configured,
    send_price_drop_notification,
    send_new_minimum_notification,
    send_first_price_notification,
)

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=os.getenv("TZ", "Europe/Bucharest"))


def _get_telegram_chat_ids() -> List[str]:
    """Get list of Telegram chat IDs from environment or settings."""
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if chat_id:
        # Support multiple chat IDs separated by commas
        return [cid.strip() for cid in chat_id.split(",") if cid.strip()]
    return []


def _send_notifications_for_entry(product_id: int, entry: PriceEntry):
    """Send Telegram notifications based on price change type."""
    if not is_configured():
        logger.info("Telegram not configured, skipping notification")
        return

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            return

        chat_ids = _get_telegram_chat_ids()
        if not chat_ids:
            return

        # Get all price entries for this product to determine notification type
        all_entries = (
            db.query(PriceEntry)
            .filter(PriceEntry.product_id == product_id)
            .order_by(PriceEntry.checked_at.asc())
            .all()
        )

        if len(all_entries) == 1:
            # First price check - send initial notification
            for chat_id in chat_ids:
                send_first_price_notification(
                    product_name=product.name,
                    product_url=product.url,
                    price=entry.price,
                    currency=product.currency,
                    chat_id=chat_id,
                )
        elif entry.is_minimum:
            # New minimum price
            prev_min = min(e.price for e in all_entries if e.id != entry.id)
            for chat_id in chat_ids:
                send_new_minimum_notification(
                    product_name=product.name,
                    product_url=product.url,
                    old_min_price=prev_min,
                    new_price=entry.price,
                    currency=product.currency,
                    chat_id=chat_id,
                )
        else:
            # Check if price dropped compared to previous check
            prev_entries = [e for e in all_entries if e.id != entry.id]
            if prev_entries:
                last_price = max(
                    (e.price for e in prev_entries),
                    key=lambda p: next(
                        (e.checked_at for e in prev_entries if e.price == p), None
                    ),
                )
                # Get the actual last entry by date
                last_entry = max(prev_entries, key=lambda e: e.checked_at)
                if entry.price < last_entry.price:
                    for chat_id in chat_ids:
                        send_price_drop_notification(
                            product_name=product.name,
                            product_url=product.url,
                            old_price=last_entry.price,
                            new_price=entry.price,
                            currency=product.currency,
                            chat_id=chat_id,
                        )

    except Exception as e:
        logger.error(f"Error sending notifications: {e}")
    finally:
        db.close()


def scheduled_price_check():
    """Scheduled job that checks all product prices and sends notifications."""
    logger.info("=== Scheduled price check started ===")
    results = run_all_price_checks()

    for result in results:
        if result["success"]:
            # Trigger notification for this product
            db = SessionLocal()
            try:
                entry = (
                    db.query(PriceEntry)
                    .filter(PriceEntry.product_id == result["product_id"])
                    .order_by(PriceEntry.checked_at.desc())
                    .first()
                )
                if entry:
                    _send_notifications_for_entry(result["product_id"], entry)
            finally:
                db.close()

    logger.info(f"=== Price check completed. Results: {results} ===")


def init_scheduler():
    """Initialize the scheduler with configured times."""
    # Parse check times from environment variables
    # Default: 9:00 AM and 2:00 PM
    check_times = os.getenv("PRICE_CHECK_TIMES", "09:00,14:00").split(",")

    for time_str in check_times:
        time_str = time_str.strip()
        if ":" not in time_str:
            continue

        hour, minute = time_str.split(":")
        try:
            hour_int = int(hour)
            minute_int = int(minute)
            if not (0 <= hour_int <= 23 and 0 <= minute_int <= 59):
                raise ValueError("Invalid time")

            trigger = CronTrigger(hour=hour_int, minute=minute_int)
            scheduler.add_job(
                scheduled_price_check,
                trigger=trigger,
                id=f"price_check_{hour_int}_{minute_int}",
                name=f"Price check at {time_str}",
                replace_existing=True,
            )
            logger.info(f"Scheduled price check at {time_str}")

        except ValueError as e:
            logger.error(f"Invalid check time '{time_str}': {e}")

    # Start the scheduler
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def shutdown_scheduler():
    """Shutdown the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")