"""
Multi-user Authentication system.
Users are stored in the database with SHA-256 hashed passwords.
Uses signed base64-encoded JSON tokens for session-based authentication:
base64url(JSON payload) + "." + HMAC-SHA256 hex signature, so tokens cannot
be forged or modified without the signing secret.
"""

import hashlib
import base64
import hmac
import json
import logging
import os
import secrets
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

# HMAC-SHA256 signing secret for tokens. Resolution order:
#   1. PM_TOKEN_SECRET env var (set it to keep tokens valid across replicas/volumes)
#   2. A random 256-bit secret persisted at <db_dir>/.pm_token_secret (created on
#      first use; survives container restarts via the data volume)
#   3. A per-process random secret (last resort — all tokens die on restart)
_TOKEN_SECRET: Optional[bytes] = None


def _get_token_secret() -> bytes:
    global _TOKEN_SECRET
    if _TOKEN_SECRET is not None:
        return _TOKEN_SECRET

    env_secret = os.getenv("PM_TOKEN_SECRET", "").strip()
    if env_secret:
        _TOKEN_SECRET = env_secret.encode("utf-8")
        return _TOKEN_SECRET

    try:
        from backup import get_db_path

        secret_file = get_db_path().parent / ".pm_token_secret"
        if secret_file.is_file():
            value = secret_file.read_text().strip()
            if value:
                _TOKEN_SECRET = value.encode("utf-8")
                return _TOKEN_SECRET
        value = secrets.token_hex(32)
        secret_file.write_text(value)
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        logger.info(f"Generated a new token-signing secret at {secret_file}")
        _TOKEN_SECRET = value.encode("utf-8")
        return _TOKEN_SECRET
    except Exception as e:
        logger.warning(
            f"PM_TOKEN_SECRET is not set and the secret file could not be used ({e}) - "
            "using a per-process secret; all tokens will be invalidated on restart"
        )
        _TOKEN_SECRET = secrets.token_bytes(32)
        return _TOKEN_SECRET


def hash_password(password: str) -> str:
    """Hash a password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()


def generate_token(user_id: int, username: str) -> str:
    """Generate a signed token: base64url(JSON payload) + "." + HMAC-SHA256 hex signature."""
    payload = json.dumps(
        {"user_id": user_id, "username": username, "ts": time.time()},
        separators=(",", ":"),
    )
    signature = hmac.new(_get_token_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + signature


def validate_token(token: str) -> Optional[dict]:
    """Validate a signed token: signature, then expiry, then user existence.

    Returns user info dict or None.
    """
    if not token:
        return None
    try:
        token_b64, signature = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(token_b64.encode())
        expected = hmac.new(_get_token_secret(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None

        decoded = json.loads(payload.decode())
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
    "/api/config/timezone",
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