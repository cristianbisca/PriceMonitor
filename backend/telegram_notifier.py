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

# Track whether we've already logged the configuration status
_config_logged = False


def _log_configuration_status():
    """Log detailed Telegram configuration status (only once)."""
    global _config_logged
    if _config_logged:
        return

    logger.info("=" * 50)
    logger.info("Telegram Notification Configuration:")
    
    # Check bot token
    if TELEGRAM_BOT_TOKEN:
        # Mask the token for security, show only prefix and suffix
        masked_token = f"{TELEGRAM_BOT_TOKEN[:8]}...{TELEGRAM_BOT_TOKEN[-4:]}"
        logger.info(f"  Bot Token: SET ({masked_token})")
        
        # Validate token format (Telegram tokens are typically: <numeric>:<alphanumeric>)
        if ":" in TELEGRAM_BOT_TOKEN and len(TELEGRAM_BOT_TOKEN) > 20:
            logger.info("  Bot Token Format: VALID (standard format detected)")
        else:
            logger.warning("  Bot Token Format: SUSPICIOUS (may not be a valid Telegram bot token)")
    else:
        logger.warning("  Bot Token: NOT SET (TELEGRAM_BOT_TOKEN environment variable is empty)")

    # Check chat ID
    if TELEGRAM_CHAT_ID:
        chat_ids = [cid.strip() for cid in TELEGRAM_CHAT_ID.split(",") if cid.strip()]
        logger.info(f"  Chat ID(s): SET ({len(chat_ids)} chat(s) configured: {chat_ids})")
        
        # Validate chat ID format (Telegram chat IDs are typically numeric, may start with -)
        for chat_id in chat_ids:
            if chat_id.lstrip("-").isdigit():
                logger.info(f"    Chat ID '{chat_id}': VALID format (numeric)")
            else:
                logger.warning(f"    Chat ID '{chat_id}': UNUSUAL format (expected numeric ID)")
    else:
        logger.warning("  Chat ID: NOT SET (TELEGRAM_CHAT_ID environment variable is empty)")

    # Overall status
    if is_configured():
        logger.info("Telegram Notifications: ENABLED")
    else:
        logger.warning("Telegram Notifications: DISABLED (missing configuration)")
    
    logger.info("=" * 50)
    _config_logged = True


def verify_telegram_connection() -> dict:
    """Verify Telegram bot token by calling getMe API.
    
    Returns a dict with verification results.
    """
    result = {
        "configured": is_configured(),
        "token_valid": False,
        "bot_info": None,
        "error": None,
    }

    if not TELEGRAM_BOT_TOKEN:
        result["error"] = "TELEGRAM_BOT_TOKEN not set"
        logger.error(result["error"])
        return result

    url = f"{TELEGRAM_API_BASE}/getMe"
    try:
        logger.info(f"Verifying Telegram bot token...")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("ok"):
            bot_info = data.get("result", {})
            result["token_valid"] = True
            result["bot_info"] = {
                "id": bot_info.get("id"),
                "first_name": bot_info.get("first_name"),
                "username": bot_info.get("username"),
            }
            logger.info(
                f"Telegram bot verified successfully: "
                f"{result['bot_info']['first_name']} (@{result['bot_info'].get('username', 'no username')})"
            )
        else:
            error_desc = data.get("description", "Unknown error")
            result["error"] = f"API returned ok=false: {error_desc}"
            logger.error(f"Telegram bot token INVALID: {error_desc}")
            
    except requests.RequestException as e:
        result["error"] = str(e)
        logger.error(f"Failed to verify Telegram bot token: {e}")

    return result


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
        logger.debug(f"Sending Telegram message to chat_id={chat_id}")
        response = requests.post(url, json=payload, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                msg_id = data.get("result", {}).get("message_id")
                logger.info(f"Telegram notification sent successfully to {chat_id} (message_id={msg_id})")
                return True
            else:
                error_desc = data.get("description", "Unknown error")
                logger.error(f"Telegram API error sending to {chat_id}: {error_desc}")
                # Specific checks for common issues
                if "chat not found" in error_desc.lower():
                    logger.error(
                        f"Chat ID '{chat_id}' not found. "
                        f"Make sure the bot has been added to the chat/group."
                    )
                elif "not found" in error_desc.lower():
                    logger.error(
                        f"Telegram entity not found. Verify your TELEGRAM_CHAT_ID is correct: '{chat_id}'"
                    )
                return False
        else:
            response.raise_for_status()
            return True
            
    except requests.HTTPError as e:
        status_code = e.response.status_code if hasattr(e, 'response') else 'unknown'
        error_body = e.response.text if hasattr(e, 'response') else ''
        logger.error(f"Telegram HTTP error {status_code} sending to {chat_id}: {error_body}")
        
        # Provide specific guidance for common HTTP errors
        if status_code == 401:
            logger.error(
                "Bot token is invalid or expired. "
                "Check your TELEGRAM_BOT_TOKEN environment variable."
            )
        elif status_code == 400:
            if "chat not found" in error_body.lower():
                logger.error(
                    f"Chat ID '{chat_id}' is invalid or the bot is not a member. "
                    f"Verify TELEGRAM_CHAT_ID and add the bot to the chat."
                )
        return False
        
    except requests.Timeout:
        logger.error(f"Telegram API request timed out for chat {chat_id}")
        return False
        
    except requests.RequestException as e:
        logger.error(f"Failed to send Telegram notification to {chat_id}: {e}")
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