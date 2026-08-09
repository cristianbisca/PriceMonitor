"""
Multi-user Authentication system.
Users are stored in the database with SHA-256 hashed passwords.
Uses base64-encoded JSON tokens for session-based authentication.
"""

import hashlib
import base64
import json
import logging
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from database import SessionLocal
from models import User

logger = logging.getLogger(__name__)

# Token validity: 7 days in seconds
TOKEN_TTL_SECONDS = 604800


def hash_password(password: str) -> str:
    """Hash a password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()


def generate_token(user_id: int, username: str) -> str:
    """Generate a base64-encoded JSON token with timestamp and user info."""
    token_data = {
        "user_id": user_id,
        "username": username,
        "ts": time.time()
    }
    return base64.b64encode(json.dumps(token_data).encode()).decode()


def validate_token(token: str) -> Optional[dict]:
    """Validate a token by decoding and checking expiry. Returns user info dict or None."""
    if not token:
        return None
    try:
        decoded = json.loads(base64.b64decode(token).decode())
        user_id = decoded.get("user_id")
        username = decoded.get("username")
        timestamp = decoded.get("ts", 0)
        not_expired = (time.time() - timestamp) < TOKEN_TTL_SECONDS

        if not user_id or not username or not not_expired:
            return None

        # Verify user still exists in database
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id, User.username == username).first()
            if not user:
                return None
        finally:
            db.close()

        return {"user_id": user_id, "username": username}
    except Exception:
        return None


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


def get_current_user(request: Request) -> Optional[dict]:
    """Get the current user from the request token. Returns user info dict or None."""
    token = extract_token_from_request(request)
    if token:
        return validate_token(token)
    return None


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
    "/api/auth/logout",
    "/api/auth/register",
}

# Paths that serve static assets for the login page
STATIC_PREFIXES = (
    "/static/",
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Token-based Authentication middleware.
    
    Allows access to:
    - Public API endpoints (/api/health, /api/auth/status, /api/auth/login, /api/auth/register)
    - Static files (needed for login page rendering)
    - Root path (serves the frontend which handles auth UI)
    
    Requires valid token for all other API endpoints.
    Token is passed via X-PM-Token header or Bearer Authorization header.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ):
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
        user_info = get_current_user(request)
        if not user_info:
            return _unauthorized_response()

        # Store user info on request state for downstream use
        request.state.user = user_info

        response = await call_next(request)
        return response


def require_auth(request: Request) -> dict:
    """Dependency to check authentication for API endpoints and return user info."""
    user_info = get_current_user(request)
    if not user_info:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )
    return user_info