"""
Database backup and restore service using Dropbox.

Pattern (same as HouseholdReplacementTracker):
- A long-lived Dropbox OAuth2 *refresh token* + app key/secret (env vars)
  are exchanged for a fresh access token lazily, on every backup/restore
  operation, so the token in use is always valid.
- Backups are consistent SQLite snapshots (online backup API, safe while
  the app is running) written to <db_dir>/backups/pm_backup_YYYY-MM-DD_HHMMSS.sqlite
  and uploaded to Dropbox in overwrite mode.
- Backups older than BACKUP_RETENTION_DAYS are pruned remotely and locally
  after each run.
- RESTORE_LATEST_BACKUP=true triggers a one-shot restore on startup (before
  the app touches the database): newest backup is downloaded, validated in a
  separate connection, a safety copy of the current DB is kept as
  <db_dir>/backups/pm_pre_restore_YYYY-MM-DD_HHMMSS.sqlite, then the DB
  file is replaced. Set it back to false after a successful restore.

The Dropbox API is called with plain `requests` (no SDK dependency), wire
format matching dropbox SDK v11:
- https://api.dropboxapi.com/...      arg as JSON request body
- https://content.dropboxapi.com/...  Dropbox-API-Arg header
  (octet-stream body for uploads)
"""

import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from sqlalchemy.engine import make_url

from database import DATABASE_URL

logger = logging.getLogger(__name__)

API_URL = "https://api.dropboxapi.com"
CONTENT_URL = "https://content.dropboxapi.com"
OAUTH_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"

BACKUP_PREFIX = "pm_backup_"
SAFETY_PREFIX = "pm_pre_restore_"
RESTORE_TEMP_NAME = "_restore_temp.sqlite"
REQUIRED_TABLES = ("users", "products", "price_history")
REQUEST_TIMEOUT = 120


def _masked(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) < 16:
        return value[:2] + "…"
    return f"{value[:8]}…{value[-8:]}"


class DropboxError(RuntimeError):
    """Raised when a Dropbox API call fails."""


class BackupNotSupportedError(RuntimeError):
    """Raised when the database is not a local SQLite file."""


# ── Configuration (env vars) ─────────────────────────────────────────────────

BACKUP_ENABLED = os.getenv("BACKUP_ENABLED", "true").strip().lower() not in ("false", "0", "")
RESTORE_LATEST_BACKUP = os.getenv("RESTORE_LATEST_BACKUP", "false").strip().lower() not in ("false", "0", "")
DROPBOX_REFRESH_TOKEN = os.getenv("BACKUP_DROPBOX_REFRESH_TOKEN", "").strip()
DROPBOX_APP_KEY = os.getenv("BACKUP_DROPBOX_APP_KEY", "").strip()
DROPBOX_APP_SECRET = os.getenv("BACKUP_DROPBOX_APP_SECRET", "").strip()
DROPBOX_FOLDER = os.getenv("BACKUP_DROPBOX_FOLDER", "/Backup").strip()
if not DROPBOX_FOLDER.startswith("/"):
    DROPBOX_FOLDER = "/" + DROPBOX_FOLDER
DROPBOX_FOLDER = DROPBOX_FOLDER.rstrip("/") or "/"
try:
    RETENTION_DAYS = max(1, int(os.getenv("BACKUP_RETENTION_DAYS", "30") or 30))
except ValueError:
    RETENTION_DAYS = 30
BACKUP_SCHEDULE = os.getenv("BACKUP_SCHEDULE", "0 2 * * *").strip()

_ACCESS_TOKEN = None


# ── Database file location ───────────────────────────────────────────────────

def get_db_path() -> Path:
    """Resolve the physical SQLite file path from DATABASE_URL."""
    if not DATABASE_URL.startswith("sqlite"):
        raise BackupNotSupportedError(
            "Backup/restore is only supported for SQLite databases — "
            f"DATABASE_URL is {DATABASE_URL.split(':', 1)[0]}-based"
        )
    database = make_url(DATABASE_URL).database
    if not database:
        raise BackupNotSupportedError(f"Could not resolve a SQLite file path from DATABASE_URL: {DATABASE_URL}")
    return Path(database).expanduser().resolve()


def get_backup_dir() -> Path:
    """Local backup directory: alongside the database file."""
    return get_db_path().parent / "backups"


# ── Dropbox authentication ───────────────────────────────────────────────────

def refresh_access_token():
    """Exchange the refresh token + app key/secret for a fresh access token.

    Returns the access token string, or None on failure (logged).
    """
    try:
        response = requests.post(
            OAUTH_TOKEN_URL,
            auth=(DROPBOX_APP_KEY, DROPBOX_APP_SECRET),
            data={
                "grant_type": "refresh_token",
                "refresh_token": DROPBOX_REFRESH_TOKEN,
            },
            timeout=30,
        )
    except requests.RequestException as e:
        logger.error(f"[Backup] Dropbox token refresh request failed: {e}")
        return None

    if response.status_code != 200:
        logger.error(f"[Backup] Dropbox token refresh failed (HTTP {response.status_code}): {response.text[:300]}")
        return None

    try:
        token = response.json().get("access_token")
    except ValueError:
        logger.error(f"[Backup] Dropbox token refresh returned invalid JSON: {response.text[:300]}")
        return None

    if not token:
        logger.error(f"[Backup] Dropbox token refresh response has no access_token: {response.text[:300]}")
        return None
    return token


def init_dropbox() -> bool:
    """Lazily configure the Dropbox client: token refresh + working verification.

    Verification = best-effort account info + folder access probe (the account
    endpoint is not reliable for every account type). Never raises. Returns
    True when the Dropbox client is usable.
    """
    global _ACCESS_TOKEN
    _ACCESS_TOKEN = None

    if not (BACKUP_ENABLED or RESTORE_LATEST_BACKUP):
        logger.info("[Backup] Backup feature is disabled (BACKUP_ENABLED=false)")
        return False
    if not (DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY and DROPBOX_APP_SECRET):
        logger.info(
            "[Backup] Dropbox not configured "
            f"(refresh token: {_masked(DROPBOX_REFRESH_TOKEN)}, "
            f"app key: {_masked(DROPBOX_APP_KEY)}, app secret: {_masked(DROPBOX_APP_SECRET)})"
        )
        return False

    logger.info(
        f"[Backup] Dropbox config: folder={DROPBOX_FOLDER}, retention={RETENTION_DAYS} days, "
        f"refresh token: {_masked(DROPBOX_REFRESH_TOKEN)}"
    )
    _ACCESS_TOKEN = refresh_access_token()
    if not _ACCESS_TOKEN:
        return False

    try:
        account = _dropbox_post(API_URL, "users/get_current_account", {}).json()
        name = account.get("name", {}).get("display_name", "unknown")
        email = account.get("email", "no email")
        logger.info(f"[Backup] Dropbox connected: {name} ({email})")
    except (DropboxError, requests.RequestException) as e:
        # Account-type quirks (e.g. team accounts with a personal app) can make
        # this endpoint fail while file access still works - probe the folder.
        logger.warning(f"[Backup] Could not fetch Dropbox account info ({e}); verifying file access instead")

    try:
        ensure_dropbox_folder()
        logger.info(f"[Backup] Dropbox backup folder verified: {DROPBOX_FOLDER}")
        return True
    except (DropboxError, requests.RequestException) as e:
        logger.error(f"[Backup] Dropbox verification failed: no file access to {DROPBOX_FOLDER} ({e})")
        _ACCESS_TOKEN = None
        return False


def _ensure_token() -> str:
    global _ACCESS_TOKEN
    if not _ACCESS_TOKEN:
        _ACCESS_TOKEN = refresh_access_token()
    if not _ACCESS_TOKEN:
        raise DropboxError(
            "Dropbox client not available (missing BACKUP_DROPBOX_REFRESH_TOKEN/APP_KEY/APP_SECRET "
            "or token refresh failed)"
        )
    return _ACCESS_TOKEN


def _dropbox_post(base_url: str, endpoint: str, arg: dict, body: bytes = None) -> requests.Response:
    """POST to a Dropbox API endpoint; refreshes the token once on 401/403.

    Wire format mirrors the official dropbox SDK v11: api.dropboxapi.com RPC
    calls carry the arg as a JSON request body, content.dropboxapi.com calls
    use the Dropbox-API-Arg header (octet-stream body for uploads).
    """
    global _ACCESS_TOKEN
    last_error = "unknown error"
    for _ in range(2):
        headers = {"Authorization": f"Bearer {_ensure_token()}"}
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
            headers["Dropbox-API-Arg"] = json.dumps(arg)
            payload = body
        elif base_url == CONTENT_URL:
            headers["Dropbox-API-Arg"] = json.dumps(arg)
            payload = None
        else:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(arg)
        try:
            response = requests.post(f"{base_url}/2/{endpoint}", headers=headers, data=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            raise DropboxError(f"Dropbox {endpoint} request failed: {e}")

        if response.status_code in (401, 403):
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            _ACCESS_TOKEN = None
            continue
        if response.status_code != 200:
            raise DropboxError(f"Dropbox {endpoint} failed (HTTP {response.status_code}): {response.text[:300]}")
        return response
    raise DropboxError(f"Dropbox {endpoint} failed after token refresh: {last_error}")


# ── Dropbox file operations ──────────────────────────────────────────────────

def ensure_dropbox_folder() -> None:
    """Make sure the remote backup folder exists, creating it on first run."""
    try:
        response = _dropbox_post(API_URL, "files/get_metadata", {"path": DROPBOX_FOLDER})
    except DropboxError as e:
        if "path_not_found" in str(e):
            _dropbox_post(API_URL, "files/create_folder_v2", {"path": DROPBOX_FOLDER, ".tag": "auto"})
            logger.info(f"[Backup] Created Dropbox backup folder {DROPBOX_FOLDER}")
            return
        raise
    meta = response.json()
    if meta.get(".tag") != "folder":
        raise DropboxError(f"Dropbox path {DROPBOX_FOLDER} exists but is not a folder")


def upload_to_dropbox(backup_path: Path, filename: str) -> dict:
    """Upload a local backup file to Dropbox (overwrite mode)."""
    data = backup_path.read_bytes()
    arg = {
        "path": f"{DROPBOX_FOLDER}/{filename}",
        "mode": "overwrite",
        "mute": False,
        "strict_conflict": False,
        "autorename": False,
    }
    response = _dropbox_post(CONTENT_URL, "files/upload", arg, body=data)
    try:
        return response.json()
    except ValueError:
        return {"name": filename, "size": len(data)}


def list_dropbox_backups():
    """List pm_backup_*.sqlite files in the remote backup folder (paginated)."""
    entries = []
    cursor = None
    while True:
        arg = {"path": DROPBOX_FOLDER, "recursive": False, "limit": 1000}
        if cursor:
            arg["cursor"] = cursor
        response = _dropbox_post(API_URL, "files/list_folder", arg)
        data = response.json()
        for item in data.get("entries", []):
            if item.get(".tag") != "file":
                continue
            name = item.get("name", "")
            if not (name.startswith(BACKUP_PREFIX) and name.endswith(".sqlite")):
                continue
            entries.append({
                "name": name,
                "path": item.get("path_lower") or f"{DROPBOX_FOLDER}/{name}",
                "path_display": f"{DROPBOX_FOLDER}/{name}",
                "size": item.get("size") or 0,
                "modified": item.get("client_modified") or item.get("server_modified"),
            })
        if not data.get("has_more") or not data.get("cursor"):
            break
        cursor = data["cursor"]
    return entries


def delete_dropbox_backup(dropbox_path: str):
    """Delete one file from the remote backup folder."""
    _dropbox_post(API_URL, "files/delete_v2", {"path": dropbox_path})


def download_backup_from_dropbox(dropbox_path: str, local_path: Path):
    """Download a remote backup to a local file path."""
    response = _dropbox_post(CONTENT_URL, "files/download", {"path": dropbox_path})
    local_path.write_bytes(response.content)


def find_newest_dropbox_backup():
    """Newest pm_backup_*.sqlite in the remote folder (by client_modified)."""
    entries = list_dropbox_backups()
    if not entries:
        return None
    return max(entries, key=lambda e: e["modified"] or "")


# ── Local backups ────────────────────────────────────────────────────────────

def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _snapshot_to(src_path: Path, dst_path: Path):
    """Consistent online copy of a (possibly live) SQLite database file."""
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def create_local_backup() -> dict:
    """Snapshot the live database into <db_dir>/backups/ (safe while running)."""
    db_path = get_db_path()
    if not db_path.exists():
        raise RuntimeError(f"Database file not found: {db_path}")
    backup_dir = get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{BACKUP_PREFIX}{_timestamp()}.sqlite"
    backup_path = backup_dir / filename
    _snapshot_to(db_path, backup_path)
    logger.info(f"[Backup] Local backup created: {backup_path} ({backup_path.stat().st_size} bytes)")
    return {"path": str(backup_path), "filename": filename, "size": backup_path.stat().st_size}


def validate_backup_sqlite(file_path: Path) -> dict:
    """Independently validate a backup file in a fresh SQLite connection."""
    if not file_path.exists() or file_path.stat().st_size == 0:
        return {"valid": False, "reason": "backup file does not exist or is empty"}

    try:
        conn = sqlite3.connect(str(file_path))
    except sqlite3.DatabaseError as e:
        return {"valid": False, "reason": f"failed to open SQLite file: {e}"}

    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            return {"valid": False, "reason": "missing required tables: " + ", ".join(missing)}
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        product_count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        return {"valid": True, "user_count": user_count, "product_count": product_count}
    except sqlite3.DatabaseError as e:
        return {"valid": False, "reason": f"failed to read backup: {e}"}
    finally:
        conn.close()


def _date_from_name(filename: str):
    """Extract the YYYY-MM-DD date embedded in a backup filename."""
    match = re.match(rf"(?:{BACKUP_PREFIX}|{SAFETY_PREFIX})(\d{{4}}-\d{{2}}-\d{{2}})_", filename)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def cleanup_old_backups() -> list:
    """Delete remote backups older than RETENTION_DAYS. Returns deleted filenames."""
    cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    deleted = []
    for entry in list_dropbox_backups():
        file_date = _date_from_name(entry["name"])
        if file_date is None or file_date >= cutoff:
            continue
        try:
            delete_dropbox_backup(entry["path"])
            deleted.append(entry["name"])
        except DropboxError as e:
            logger.error(f"[Backup] Failed to delete old remote backup {entry['name']}: {e}")
    if deleted:
        logger.info(f"[Backup] Deleted {len(deleted)} old remote backup(s): {deleted}")
    return deleted


def cleanup_local_backups() -> list:
    """Delete local backups/safety copies older than RETENTION_DAYS."""
    backup_dir = get_backup_dir()
    if not backup_dir.exists():
        return []
    cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    deleted = []
    for entry in backup_dir.iterdir():
        if not entry.name.endswith(".sqlite"):
            continue
        file_date = _date_from_name(entry.name)
        if file_date is None or file_date >= cutoff:
            continue
        try:
            entry.unlink()
            deleted.append(entry.name)
        except OSError as e:
            logger.error(f"[Backup] Failed to delete old local backup {entry.name}: {e}")
    if deleted:
        logger.info(f"[Backup] Deleted {len(deleted)} old local backup(s): {deleted}")
    return deleted


# ── Orchestration ────────────────────────────────────────────────────────────

def perform_backup():
    """Run one full backup: local snapshot, Dropbox upload, retention cleanup.

    Returns {local: {...}, dropbox: {...} | None}, or None when backups are
    disabled. Raises on failure.
    """
    if not BACKUP_ENABLED:
        logger.info("[Backup] Backup is disabled (BACKUP_ENABLED=false); skipping")
        return None

    initialized = init_dropbox()
    local_backup = create_local_backup()

    dropbox_result = None
    if initialized:
        dropbox_result = upload_to_dropbox(Path(local_backup["path"]), local_backup["filename"])
        logger.info(f"[Backup] Uploaded to Dropbox: {dropbox_result.get('name', local_backup['filename'])}")
        cleanup_old_backups()

    cleanup_local_backups()
    return {"local": local_backup, "dropbox": dropbox_result}


def list_backups() -> dict:
    """Merged list of local and Dropbox backups, newest first."""
    local = []
    backup_dir = get_backup_dir()
    try:
        if backup_dir.exists():
            for entry in backup_dir.iterdir():
                if not (entry.name.startswith(BACKUP_PREFIX) and entry.name.endswith(".sqlite")):
                    continue
                if not entry.is_file():
                    continue
                stat = entry.stat()
                local.append({
                    "name": entry.name,
                    "size": stat.st_size,
                    "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "location": "local",
                })
    except OSError as e:
        logger.error(f"[Backup] Could not list local backups: {e}")

    dropbox = []
    try:
        for entry in list_dropbox_backups():
            dropbox.append({
                "name": entry["name"],
                "size": entry["size"],
                "created": entry["modified"],
                "location": "dropbox",
                "path": entry["path_display"],
            })
    except (DropboxError, requests.RequestException) as e:
        logger.error(f"[Backup] Could not list Dropbox backups: {e}")

    local.sort(key=lambda item: item["created"], reverse=True)
    dropbox.sort(key=lambda item: item["created"] or "", reverse=True)
    return {"local": local, "dropbox": dropbox}


def get_status() -> dict:
    """Backup feature status and configuration (no secrets included)."""
    database = None
    try:
        database = str(get_db_path())
    except BackupNotSupportedError as e:
        database = f"unsupported: {e}"
    return {
        "enabled": BACKUP_ENABLED,
        "dropbox_configured": bool(DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY and DROPBOX_APP_SECRET),
        "dropbox_folder": DROPBOX_FOLDER,
        "retention_days": RETENTION_DAYS,
        "schedule": BACKUP_SCHEDULE or None,
        "restore_latest_backup": RESTORE_LATEST_BACKUP,
        "database": database,
    }


def restore_latest_backup():
    """Restore the newest Dropbox backup over the local database.

    Intended to run on startup, BEFORE the app opens any DB connection.
    Steps: download newest backup → validate in a separate connection →
    safety copy of the current DB (pm_pre_restore_*.sqlite) → replace the
    DB file. Raises RuntimeError on any pre-replacement failure.
    """
    from database import engine  # local import: no circular dependency

    if not RESTORE_LATEST_BACKUP:
        logger.info("[Restore] RESTORE_LATEST_BACKUP not set - skipping startup restore")
        return None

    if not init_dropbox():
        raise RuntimeError(
            "Dropbox client not available for restore - check BACKUP_DROPBOX_REFRESH_TOKEN/"
            "BACKUP_DROPBOX_APP_KEY/BACKUP_DROPBOX_APP_SECRET"
        )

    newest = find_newest_dropbox_backup()
    if not newest:
        raise RuntimeError(f"No backups found in Dropbox folder {DROPBOX_FOLDER} to restore")

    backup_dir = get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    temp_path = backup_dir / RESTORE_TEMP_NAME

    try:
        logger.info(f"[Restore] Downloading newest backup: {newest['name']} ({newest['size']} bytes)")
        download_backup_from_dropbox(newest["path"], temp_path)

        validation = validate_backup_sqlite(temp_path)
        if not validation["valid"]:
            raise RuntimeError(f"Backup validation failed: {validation['reason']}")
        logger.info(
            f"[Restore] Backup validated: {validation['user_count']} users, "
            f"{validation['product_count']} products"
        )

        db_path = get_db_path()
        # Close any pooled connections still holding the live DB file, so the
        # replace below succeeds even on Windows (open file handles block
        # os.replace). Fresh connections will open the new file.
        engine.dispose()
        safety_path = None
        if db_path.exists():
            safety_path = backup_dir / f"{SAFETY_PREFIX}{_timestamp()}.sqlite"
            _snapshot_to(db_path, safety_path)
            logger.info(f"[Restore] Safety copy of current DB saved: {safety_path}")

        temp_path.replace(db_path)
        logger.info(f"[Restore] Database restored from {newest['name']} → {db_path}")

        return {
            "source": newest["name"],
            "source_path": newest["path_display"],
            "size": newest["size"],
            "modified": newest["modified"],
            "user_count": validation["user_count"],
            "product_count": validation["product_count"],
            "safety_backup": str(safety_path) if safety_path else None,
        }
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
