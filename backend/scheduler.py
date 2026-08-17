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
from models import Product, PriceEntry, User
from alternate_links import scheduled_alternate_discovery
from price_checker import check_product_price, run_all_price_checks
from telegram_notifier import (
    is_configured,
    send_price_drop_notification,
    send_new_minimum_notification,
    send_first_price_notification,
)

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=os.getenv("TZ", "Europe/Bucharest"))


def _get_user_chat_ids_for_product(product_id: int) -> List[str]:
    """Get Telegram chat IDs for users who have this product and notifications enabled.

    Falls back to global TELEGRAM_CHAT_ID env var if no users have per-user settings configured.
    """
    db = SessionLocal()
    try:
        # Get the product to find its owner
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            return []

        # Get the product owner's Telegram settings
        user = db.query(User).filter(User.id == product.user_id).first()
        if user and user.telegram_notifications_enabled and user.telegram_chat_id:
            return [user.telegram_chat_id]

        # Fallback: use global TELEGRAM_CHAT_ID from env for backward compatibility
        chat_id_env = os.getenv("TELEGRAM_CHAT_ID", "")
        if chat_id_env:
            return [cid.strip() for cid in chat_id_env.split(",") if cid.strip()]

        return []
    finally:
        db.close()


def _send_notifications_for_product(product_id: int):
    """Send Telegram notifications for a product just after a check run.

    Everything is based on the product's **current price**: the minimum over all
    sources checked in the latest check cycle (entries from one run share a
    ``check_cycle`` timestamp).

    - **First price**: no check cycles recorded before this one
    - **New minimum**: current price < minimum over all previously recorded entries
      (all-time minimum, i.e. the min of the per-check minimums)
    - **Price drop**: current price < the previous check cycle's current price
      (and it is not a new all-time minimum)
    """
    if not is_configured():
        logger.info("Telegram not configured, skipping notification")
        return

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            return

        chat_ids = _get_user_chat_ids_for_product(product_id)
        if not chat_ids:
            logger.info(f"No Telegram chat IDs configured for product {product_id} (user {product.user_id})")
            return

        all_entries = (
            db.query(PriceEntry)
            .filter(PriceEntry.product_id == product_id)
            .order_by(PriceEntry.checked_at.asc())
            .all()
        )
        if not all_entries:
            return

        cycles = [e.check_cycle for e in all_entries if e.check_cycle is not None]
        if not cycles:
            # Legacy rows without cycle stamps: nothing reliable to compare against.
            return
        last_cycle = max(cycles)

        # Current price = cheapest across all sources of the latest check cycle
        current_price = min(e.price for e in all_entries if e.check_cycle == last_cycle)
        prev_entries = [e for e in all_entries if e.check_cycle != last_cycle]

        if not prev_entries:
            # First price check - send initial notification
            for chat_id in chat_ids:
                send_first_price_notification(
                    product_name=product.name,
                    product_url=product.url,
                    price=current_price,
                    currency=product.currency,
                    chat_id=chat_id,
                )
            return

        prev_min = min(e.price for e in prev_entries)
        if current_price < prev_min:
            # New all-time minimum
            for chat_id in chat_ids:
                send_new_minimum_notification(
                    product_name=product.name,
                    product_url=product.url,
                    old_min_price=prev_min,
                    new_price=current_price,
                    currency=product.currency,
                    chat_id=chat_id,
                )
            return

        # Price drop: compare with the previous check cycle's current price
        prev_cycles = [e.check_cycle for e in prev_entries if e.check_cycle is not None]
        if not prev_cycles:
            return
        prev_cycle = max(prev_cycles)
        prev_current = min(e.price for e in prev_entries if e.check_cycle == prev_cycle)
        if current_price < prev_current:
            for chat_id in chat_ids:
                send_price_drop_notification(
                    product_name=product.name,
                    product_url=product.url,
                    old_price=prev_current,
                    new_price=current_price,
                    currency=product.currency,
                    chat_id=chat_id,
                )

    except Exception as e:
        logger.error(f"Error sending notifications: {e}")
    finally:
        db.close()


def scheduled_price_check():
    """Scheduled job that checks all enabled products for ALL users and sends notifications.
    
    This is a global scheduler - it checks every user's products regardless of who triggered it.
    """
    logger.info("=== Scheduled price check started (checking all users' products) ===")

    db = SessionLocal()
    try:
        # Query ALL enabled products across all users
        products = db.query(Product).filter(Product.enabled == True).all()
        logger.info(f"Running price checks for {len(products)} enabled products (all users)")
    finally:
        db.close()

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

    for result in results:
        if result["success"]:
            # Trigger notification for this product (based on the cycle-min current price)
            _send_notifications_for_product(result["product_id"])

    logger.info(f"=== Price check completed. Results: {results} ===")


def _parse_check_times(raw: str):
    """Yield valid (time_str, hour, minute) tuples from a comma-separated HH:MM list."""
    for time_str in raw.split(","):
        time_str = time_str.strip()
        if ":" not in time_str:
            continue

        hour, minute = time_str.split(":")
        try:
            hour_int = int(hour)
            minute_int = int(minute)
            if not (0 <= hour_int <= 23 and 0 <= minute_int <= 59):
                raise ValueError("Invalid time")
            yield time_str, hour_int, minute_int
        except ValueError as e:
            logger.error(f"Invalid check time '{time_str}': {e}")


def init_scheduler():
    """Initialize the scheduler with configured times."""
    # Parse check times from environment variables
    # Default: 9:00 AM and 2:00 PM
    check_times = os.getenv("PRICE_CHECK_TIMES", "09:00,14:00")

    for time_str, hour_int, minute_int in _parse_check_times(check_times):
        scheduler.add_job(
            scheduled_price_check,
            trigger=CronTrigger(hour=hour_int, minute=minute_int),
            id=f"price_check_{hour_int}_{minute_int}",
            name=f"Price check at {time_str}",
            replace_existing=True,
        )
        logger.info(f"Scheduled price check at {time_str}")

    # Alternate-link discovery schedule (empty = feature disabled)
    alternate_link_times = os.getenv("ALTERNATE_LINK_TIMES", "").strip()
    if alternate_link_times:
        for time_str, hour_int, minute_int in _parse_check_times(alternate_link_times):
            scheduler.add_job(
                scheduled_alternate_discovery,
                trigger=CronTrigger(hour=hour_int, minute=minute_int),
                id=f"alternate_links_{hour_int}_{minute_int}",
                name=f"Alternate link discovery at {time_str}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info(f"Scheduled alternate link discovery at {time_str}")
    else:
        logger.info("Alternate link discovery disabled (ALTERNATE_LINK_TIMES not set)")

    # Start the scheduler
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def shutdown_scheduler():
    """Shutdown the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")