"""
HTTP Basic Authentication middleware.
Configurable via APP_USERNAME and APP_PASSWORD environment variables.
Authentication is disabled if either variable is not set.
"""

import os
import hashlib
import base64
import logging
from typing import Callable

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


def get_credentials():
    """Get username and password from environment variables."""
    username = os.environ.get("APP_USERNAME", "")
    password = os.environ.get("APP_PASSWORD", "")
    return username, password


def is_auth_enabled():
    """Check if authentication is enabled (both credentials must be set)."""
    username, password = get_credentials()
    return bool(username and password)


def verify_credentials(provided_username: str, provided_password: str) -> bool:
    """Verify provided credentials against environment variables."""
    expected_username, expected_password = get_credentials()
    logger.info(f"Auth check: username='{provided_username}' vs '{expected_username}', password length={len(provided_password)} vs {len(expected_password)}, auth_enabled={is_auth_enabled()}")
    result = (
        hashlib.compare_digest(
            provided_username.encode(), expected_username.encode()
        )
        and hashlib.compare_digest(
            provided_password.encode(), expected_password.encode()
        )
    )
    logger.info(f"Auth result: {result}")
    return result


def parse_basic_auth(authorization: str) -> tuple:
    """Parse Authorization header for Basic auth. Returns (username, password) or (None, None)."""
    try:
        if not authorization or not authorization.startswith("Basic "):
            return None, None
        encoded = authorization.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, _, password = decoded.partition(":")
        return username, password
    except Exception:
        return None, None


def _unauthorized_response() -> JSONResponse:
    """Return a 401 response with WWW-Authenticate header."""
    return JSONResponse(
        status_code=401,
        content={"detail": "Authentication required"},
        headers={"WWW-Authenticate": 'Basic realm="Price Monitor", charset="UTF-8"'},
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Authentication middleware."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ):
        # Skip auth if not enabled
        if not is_auth_enabled():
            return await call_next(request)

        # Allow health check endpoint without auth (for Docker healthchecks)
        if request.url.path == "/api/health":
            return await call_next(request)

        # Get Authorization header
        authorization = request.headers.get("Authorization", "")
        username, password = parse_basic_auth(authorization)

        # Verify credentials
        if not verify_credentials(username or "", password or ""):
            return _unauthorized_response()

        response = await call_next(request)
        return response


def require_auth(request: Request):
    """Dependency to check authentication for API endpoints (fallback)."""
    if not is_auth_enabled():
        return True

    authorization = request.headers.get("Authorization", "")
    username, password = parse_basic_auth(authorization)
    if not verify_credentials(username or "", password or ""):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Price Monitor"'},
        )
    return True