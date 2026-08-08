"""
HTTP Basic Authentication middleware.
Configurable via APP_USERNAME and APP_PASSWORD environment variables.
Authentication is disabled if either variable is not set.
"""

import os
import hashlib
import logging
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

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
    # Use constant-time comparison to prevent timing attacks
    return (
        hashlib.compare_digest(provided_username.encode(), expected_username.encode())
        and hashlib.compare_digest(provided_password.encode(), expected_password.encode())
    )


def parse_basic_auth(authorization: str) -> tuple:
    """Parse Authorization header for Basic auth. Returns (username, password) or (None, None)."""
    try:
        if not authorization or not authorization.startswith("Basic "):
            return None, None

        import base64
        encoded = authorization.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, _, password = decoded.partition(":")
        return username, password
    except Exception:
        return None, None


class AuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Authentication middleware."""

    async def dispatch(self, request: Request, call_next):
        # Skip auth if not enabled
        if not is_auth_enabled():
            return await call_next(request)

        # Allow health check endpoint without auth (for Docker healthchecks)
        if request.url.path == "/api/health":
            return await call_next(request)

        # Get Authorization header
        authorization = request.headers.get("Authorization", "")
        username, password = parse_basic_auth(authorization)

        # For API endpoints, require authentication
        if request.url.path.startswith("/api/"):
            if not verify_credentials(username or "", password or ""):
                response = HTMLResponse(
                    status_code=401,
                    content="<h1>401 Unauthorized</h1><p>Authentication required.</p>",
                    headers={"WWW-Authenticate": 'Basic realm="Price Monitor", charset="UTF-8"'},
                )
                return response

        # For frontend pages, send 401 with WWW-Authenticate header to trigger browser login prompt
        if not verify_credentials(username or "", password or ""):
            # Check if it's an HTML request (not API)
            accept = request.headers.get("Accept", "")
            if "text/html" in accept or not request.url.path.startswith("/api/"):
                response = HTMLResponse(
                    status_code=401,
                    content="<h1>401 Unauthorized</h1><p>Authentication required.</p>",
                    headers={"WWW-Authenticate": 'Basic realm="Price Monitor", charset="UTF-8"'},
                )
                return response

        return await call_next(request)


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