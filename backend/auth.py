"""
Token-based Authentication system.
Configurable via APP_USERNAME and APP_PASSWORD environment variables.
Authentication is disabled if either variable is not set.

Uses base64-encoded JSON tokens (similar to LocalNetworkDropper approach)
for session-based authentication with a themed login UI.
"""

import os
import hmac
import base64
import json
import logging
import time
from typing import Callable, Optional

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Token validity: 7 days in seconds
TOKEN_TTL_SECONDS = 604800


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
    result = (
        hmac.compare_digest(
            provided_username.encode(), expected_username.encode()
        )
        and hmac.compare_digest(
            provided_password.encode(), expected_password.encode()
        )
    )
    return result


def generate_token(username: str, password: str) -> str:
    """Generate a base64-encoded JSON token with timestamp."""
    token_data = {
        "user": username,
        "pass": password,
        "ts": time.time()
    }
    return base64.b64encode(json.dumps(token_data).encode()).decode()


def validate_token(token: str) -> bool:
    """Validate a token by decoding and checking credentials and expiry."""
    if not token:
        return False
    try:
        decoded = json.loads(base64.b64decode(token).decode())
        expected_username, expected_password = get_credentials()
        user_valid = hmac.compare_digest(decoded.get("user", ""), expected_username)
        pass_valid = hmac.compare_digest(decoded.get("pass", ""), expected_password)
        timestamp = decoded.get("ts", 0)
        not_expired = (time.time() - timestamp) < TOKEN_TTL_SECONDS
        return user_valid and pass_valid and not_expired
    except Exception:
        return False


def extract_token_from_request(request: Request) -> Optional[str]:
    """Extract auth token from request headers."""
    # Check custom token header (primary method for API calls)
    token = request.headers.get("X-PM-Token")
    if token:
        return token

    # Check Authorization Bearer header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1]

    return None


def authenticate_request(request: Request) -> bool:
    """Check if a request is authenticated via token."""
    if not is_auth_enabled():
        return True

    token = extract_token_from_request(request)
    if token:
        return validate_token(token)

    return False


def _unauthorized_response() -> JSONResponse:
    """Return a 401 response."""
    return JSONResponse(
        status_code=401,
        content={"detail": "Authentication required"},
    )


# Paths that never require authentication
PUBLIC_PATHS = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
}

# Paths that serve static assets for the login page
STATIC_PREFIXES = (
    "/static/",
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Token-based Authentication middleware.
    
    Allows access to:
    - Public API endpoints (/api/health, /api/auth/status, /api/auth/login)
    - Static files (needed for login page rendering)
    - Root path (serves the frontend which handles auth UI)
    
    Requires valid token for all other API endpoints.
    Token is passed via X-PM-Token header or Bearer Authorization header.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ):
        # Skip auth if not enabled
        if not is_auth_enabled():
            return await call_next(request)

        path = request.url.path

        # Allow public endpoints without auth
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # Allow static files (needed for login page to render)
        for prefix in STATIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Allow root and SPA routes (frontend handles auth UI)
        if path == "/" or not path.startswith("/api/"):
            return await call_next(request)

        # Require token authentication for API endpoints
        token = extract_token_from_request(request)
        if not validate_token(token or ""):
            return _unauthorized_response()

        response = await call_next(request)
        return response


def require_auth(request: Request):
    """Dependency to check authentication for API endpoints (fallback)."""
    if not is_auth_enabled():
        return True

    token = extract_token_from_request(request)
    if not validate_token(token or ""):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )
    return True