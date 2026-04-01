"""
auth_manager.py — Firebase Authentication for ANEMI Room Charge Importer
=========================================================================
Handles email/password sign-in via Firebase Auth REST API.
Stores session credentials locally for auto-login.
No Firebase SDK required — uses plain HTTP requests.

Stamhad Software © 2026
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from urllib import request, error, parse

# ── Firebase project config ──────────────────────────────────────────────────
# Loaded from config/firebase.json (git-ignored) so keys stay out of the repo.
# Falls back to environment variables FIREBASE_API_KEY / FIREBASE_PROJECT_ID.

def _app_root() -> Path:
    """Return the app root — works both for normal Python and PyInstaller bundles."""
    import sys
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _load_firebase_config() -> tuple[str, str]:
    """Return (api_key, project_id) from config file or env vars."""
    # Check bundled / local config
    cfg_path = _app_root() / "config" / "firebase.json"
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            return data["api_key"], data["project_id"]
        except Exception:
            pass
    # Also check next to the script (for development)
    cfg_path2 = Path(__file__).resolve().parent / "config" / "firebase.json"
    if cfg_path2.exists():
        try:
            data = json.loads(cfg_path2.read_text(encoding="utf-8"))
            return data["api_key"], data["project_id"]
        except Exception:
            pass
    # Fallback to environment variables
    api_key = os.environ.get("FIREBASE_API_KEY", "")
    project_id = os.environ.get("FIREBASE_PROJECT_ID", "")
    if api_key and project_id:
        return api_key, project_id
    raise RuntimeError(
        "Firebase config not found.  Create config/firebase.json with "
        '{"api_key": "...", "project_id": "..."} or set FIREBASE_API_KEY '
        "and FIREBASE_PROJECT_ID environment variables."
    )

FIREBASE_API_KEY, FIREBASE_PROJECT_ID = _load_firebase_config()

_AUTH_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
    f"?key={FIREBASE_API_KEY}"
)
_FIRESTORE_URL = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
    f"/databases/(default)/documents"
)

# ── Session file location ────────────────────────────────────────────────────

def _session_dir() -> Path:
    """Platform-aware config directory for storing session data."""
    if platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "AnemiRoomCharge"
    d.mkdir(parents=True, exist_ok=True)
    return d


_SESSION_FILE = None  # lazily initialised


def _get_session_file() -> Path:
    global _SESSION_FILE
    if _SESSION_FILE is None:
        _SESSION_FILE = _session_dir() / ".session.json"
    return _SESSION_FILE


# ═════════════════════════════════════════════════════════════════════════════
#  FIREBASE AUTH — sign in with email + password (REST API)
# ═════════════════════════════════════════════════════════════════════════════

def authenticate(email: str, password: str) -> tuple[bool, str, dict | None]:
    """
    Authenticate with Firebase Auth REST API.

    Returns:
        (True,  "OK",         account_dict)   on success
        (False, error_message, None)           on failure
    """
    payload = json.dumps({
        "email": email,
        "password": password,
        "returnSecureToken": True,
    }).encode("utf-8")

    req = request.Request(
        _AUTH_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except error.HTTPError as e:
        body = e.read().decode()
        try:
            err_data = json.loads(body)
            code = err_data.get("error", {}).get("message", "UNKNOWN_ERROR")
        except Exception:
            code = "UNKNOWN_ERROR"

        msg_map = {
            "EMAIL_NOT_FOUND": "Email not found.",
            "INVALID_PASSWORD": "Incorrect password.",
            "INVALID_EMAIL": "Invalid email address.",
            "USER_DISABLED": "Account disabled.",
            "INVALID_LOGIN_CREDENTIALS": "Invalid email or password.",
            "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many attempts. Try later.",
        }
        return False, msg_map.get(code, f"Login failed: {code}"), None
    except Exception as exc:
        return False, f"Network error: {exc}", None

    uid = data.get("localId", "")
    id_token = data.get("idToken", "")
    display_name = data.get("displayName", "")

    # Try to fetch user profile from Firestore
    profile = _fetch_user_profile(uid, id_token)

    account = {
        "uid": uid,
        "email": data.get("email", email),
        "id_token": id_token,
        "refresh_token": data.get("refreshToken", ""),
        "display_name": display_name or profile.get("display_name", ""),
        "role": profile.get("role", "staff"),
        "enabled": profile.get("enabled", True),
    }

    # Check if account is disabled in Firestore profile
    if not account["enabled"]:
        return False, "Account suspended.", account

    return True, "OK", account


# ═════════════════════════════════════════════════════════════════════════════
#  FIRESTORE — fetch user profile
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_user_profile(uid: str, id_token: str) -> dict:
    """
    Fetch /users/{uid} document from Firestore.
    Returns a plain dict of the profile fields, or {} on failure.
    """
    if not uid or not id_token:
        return {}

    url = f"{_FIRESTORE_URL}/users/{uid}"
    req = request.Request(
        url,
        headers={"Authorization": f"Bearer {id_token}"},
        method="GET",
    )

    try:
        with request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        # Profile doc might not exist yet — that's OK
        return {}

    return _parse_firestore_fields(data.get("fields", {}))


def _parse_firestore_fields(fields: dict) -> dict:
    """Convert Firestore REST response fields to a plain dict."""
    result = {}
    for key, val_obj in fields.items():
        if "stringValue" in val_obj:
            result[key] = val_obj["stringValue"]
        elif "booleanValue" in val_obj:
            result[key] = val_obj["booleanValue"]
        elif "integerValue" in val_obj:
            result[key] = int(val_obj["integerValue"])
        elif "doubleValue" in val_obj:
            result[key] = val_obj["doubleValue"]
        else:
            result[key] = str(val_obj)
    return result


def create_user_profile(uid: str, id_token: str, profile_data: dict) -> bool:
    """
    Create or update /users/{uid} document in Firestore.
    profile_data example: {"display_name": "George", "role": "admin", "enabled": True}
    """
    fields = {}
    for key, val in profile_data.items():
        if isinstance(val, bool):
            fields[key] = {"booleanValue": val}
        elif isinstance(val, int):
            fields[key] = {"integerValue": str(val)}
        elif isinstance(val, float):
            fields[key] = {"doubleValue": val}
        else:
            fields[key] = {"stringValue": str(val)}

    url = f"{_FIRESTORE_URL}/users/{uid}"
    payload = json.dumps({"fields": fields}).encode("utf-8")

    req = request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {id_token}",
        },
        method="PATCH",
    )

    try:
        with request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  SESSION PERSISTENCE  (save / load / clear)
# ═════════════════════════════════════════════════════════════════════════════

def save_session(email: str, password: str, display_name: str = "") -> None:
    """Save login credentials locally for auto-login on next launch."""
    data = {
        "email": email,
        "password": password,
        "display_name": display_name,
    }
    try:
        path = _get_session_file()
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_session() -> dict | None:
    """
    Load saved session. Returns dict with email/password or None.
    """
    try:
        path = _get_session_file()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("email") and data.get("password"):
                return data
    except Exception:
        pass
    return None


def clear_session() -> None:
    """Delete saved session file."""
    try:
        path = _get_session_file()
        if path.exists():
            path.unlink()
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  FIRESTORE — Order Storage
# ═════════════════════════════════════════════════════════════════════════════

def _to_firestore_value(val):
    """Convert a Python value to a Firestore REST API value object."""
    if isinstance(val, bool):
        return {"booleanValue": val}
    elif isinstance(val, int):
        return {"integerValue": str(val)}
    elif isinstance(val, float):
        return {"doubleValue": val}
    elif isinstance(val, str):
        return {"stringValue": val}
    elif isinstance(val, list):
        return {"arrayValue": {"values": [_to_firestore_value(v) for v in val]}}
    elif isinstance(val, dict):
        return {"mapValue": {"fields": {k: _to_firestore_value(v) for k, v in val.items()}}}
    else:
        return {"stringValue": str(val)}


def upload_order(id_token: str, order_data: dict) -> tuple[bool, str]:
    """
    Upload a single order to Firestore at /orders/{date_folder}_{check_id}.

    order_data should contain:
        date_folder, check_id, check_number, tab_name, server, table,
        paid_date, subtotal, tax_total, tip, total, charge_type,
        items: [{"name", "qty", "net_price", "tax"}, ...]

    Returns (True, doc_id) on success, (False, error_msg) on failure.
    """
    doc_id = f"{order_data['date_folder']}_{order_data['check_id']}"
    url = f"{_FIRESTORE_URL}/orders/{doc_id}"

    fields = {}
    for key, val in order_data.items():
        fields[key] = _to_firestore_value(val)

    payload = json.dumps({"fields": fields}).encode("utf-8")

    req = request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {id_token}",
        },
        method="PATCH",
    )

    try:
        with request.urlopen(req, timeout=15):
            return True, doc_id
    except error.HTTPError as e:
        body = e.read().decode()
        return False, f"HTTP {e.code}: {body[:200]}"
    except Exception as exc:
        return False, str(exc)


def upload_orders_batch(id_token: str, orders: list[dict],
                        log_fn=None) -> tuple[int, int]:
    """
    Upload multiple orders to Firestore. Skips already-existing docs
    by using PATCH (upsert) — so re-uploads are safe / idempotent.

    Returns (success_count, error_count).
    """
    ok = 0
    errs = 0
    for order in orders:
        success, msg = upload_order(id_token, order)
        if success:
            ok += 1
            if log_fn:
                log_fn(f"[Firebase] {order.get('charge_type', '?')} "
                       f"Check #{order.get('check_number', '?')} → uploaded  ✓",
                       "ok")
        else:
            errs += 1
            if log_fn:
                log_fn(f"[Firebase] Check #{order.get('check_number', '?')} "
                       f"— failed: {msg}", "err")
    return ok, errs


def check_order_exists(id_token: str, date_folder: str,
                       check_id: str) -> bool:
    """Check if an order doc already exists in Firestore."""
    doc_id = f"{date_folder}_{check_id}"
    url = f"{_FIRESTORE_URL}/orders/{doc_id}"
    req = request.Request(
        url,
        headers={"Authorization": f"Bearer {id_token}"},
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def _parse_firestore_value(val_obj) -> object:
    """Convert a single Firestore value object back to Python."""
    if "stringValue" in val_obj:
        return val_obj["stringValue"]
    elif "booleanValue" in val_obj:
        return val_obj["booleanValue"]
    elif "integerValue" in val_obj:
        return int(val_obj["integerValue"])
    elif "doubleValue" in val_obj:
        return float(val_obj["doubleValue"])
    elif "arrayValue" in val_obj:
        return [_parse_firestore_value(v)
                for v in val_obj.get("arrayValue", {}).get("values", [])]
    elif "mapValue" in val_obj:
        fields = val_obj.get("mapValue", {}).get("fields", {})
        return {k: _parse_firestore_value(v) for k, v in fields.items()}
    return str(val_obj)


# ═════════════════════════════════════════════════════════════════════════════
#  FIREBASE — SSH Key Management (per-account cloud storage)
# ═════════════════════════════════════════════════════════════════════════════

import base64

# Firebase Realtime Database base URL
_RTDB_URL = f"https://{FIREBASE_PROJECT_ID}-default-rtdb.firebaseio.com"


def save_ssh_key_to_firebase(id_token: str, uid: str,
                              key_file_path: str) -> tuple[bool, str]:
    """
    Read an SSH private key file, base64-encode it, and store it in
    Firebase Realtime Database at /restaurant_data/{uid}/settings/toast_ssh_key.

    Returns (True, "OK") on success or (False, error_message) on failure.
    """
    try:
        raw = Path(key_file_path).read_bytes()
    except Exception as exc:
        return False, f"Could not read key file: {exc}"

    encoded = base64.b64encode(raw).decode("ascii")
    url = (f"{_RTDB_URL}/restaurant_data/{uid}/settings/toast_ssh_key.json"
           f"?auth={id_token}")

    payload = json.dumps(encoded).encode("utf-8")
    req = request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with request.urlopen(req, timeout=15):
            return True, "OK"
    except error.HTTPError as e:
        body = e.read().decode()
        return False, f"HTTP {e.code}: {body[:200]}"
    except Exception as exc:
        return False, str(exc)


def load_ssh_key_from_firebase(id_token: str, uid: str,
                                dest_path: str) -> tuple[bool, str]:
    """
    Download the SSH key from Firebase, decode it, write it to *dest_path*,
    and set permissions to 600.

    Returns (True, dest_path) on success or (False, error_message) on failure.
    If no key is stored in Firebase, returns (False, "NO_KEY").
    """
    url = (f"{_RTDB_URL}/restaurant_data/{uid}/settings/toast_ssh_key.json"
           f"?auth={id_token}")
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        return False, str(exc)

    if data is None or data == "null":
        return False, "NO_KEY"

    try:
        raw = base64.b64decode(data)
    except Exception as exc:
        return False, f"Decode error: {exc}"

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_bytes(raw)
        # Set permissions to 600 (required by SSH/Paramiko)
        if platform.system() != "Windows":
            dest.chmod(0o600)
        return True, str(dest)
    except Exception as exc:
        return False, f"Write error: {exc}"


def check_ssh_key_in_firebase(id_token: str, uid: str) -> bool:
    """Return True if an SSH key exists in Firebase for this account."""
    url = (f"{_RTDB_URL}/restaurant_data/{uid}/settings/toast_ssh_key.json"
           f"?auth={id_token}&shallow=true")
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data is not None and data != "null"
    except Exception:
        return False


def fetch_orders(id_token: str, date_folders: list[str] | None = None) -> list[dict]:
    """
    Fetch orders from Firestore.  If date_folders is given, only returns
    orders whose date_folder field is in that list.

    Uses the Firestore REST list endpoint to get all /orders docs,
    with pagination to handle collections larger than 500 docs.
    Filters client-side (avoids needing composite indexes).
    Returns a list of plain dicts.
    """
    all_docs = []
    page_token = None

    while True:
        url = f"{_FIRESTORE_URL}/orders?pageSize=500"
        if page_token:
            url += f"&pageToken={page_token}"

        req = request.Request(
            url,
            headers={"Authorization": f"Bearer {id_token}"},
            method="GET",
        )

        try:
            with request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            break

        all_docs.extend(data.get("documents", []))

        # Check for next page
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    orders = []
    for doc in all_docs:
        fields = doc.get("fields", {})
        order = {k: _parse_firestore_value(v) for k, v in fields.items()}
        orders.append(order)

    # Filter client-side if specific dates requested
    if date_folders:
        date_set = set(date_folders)
        orders = [o for o in orders if o.get("date_folder") in date_set]

    return orders


# ═══════════════════════════════════════════════════════════════════════════════
#  FIRESTORE — Sales Summary Storage
# ═══════════════════════════════════════════════════════════════════════════════

def upload_sales_summary(id_token: str, date_folder: str,
                         summary_data: dict) -> tuple[bool, str]:
    """
    Upload a daily sales summary to Firestore at /sales/{date_folder}.

    summary_data should contain:
        date_folder, dayparts: [{name, orders, net_sales, tax, discount, gross_sales}],
        void_amount, void_count, total_orders, total_guests
    """
    doc_id = date_folder
    url = f"{_FIRESTORE_URL}/sales/{doc_id}"

    fields = {}
    for key, val in summary_data.items():
        fields[key] = _to_firestore_value(val)

    payload = json.dumps({"fields": fields}).encode("utf-8")

    req = request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {id_token}",
        },
        method="PATCH",
    )

    try:
        with request.urlopen(req, timeout=15):
            return True, doc_id
    except error.HTTPError as e:
        body = e.read().decode()
        return False, f"HTTP {e.code}: {body[:200]}"
    except Exception as exc:
        return False, str(exc)


def upload_sales_batch(id_token: str, summaries: list[dict],
                       log_fn=None) -> tuple[int, int]:
    """
    Upload multiple daily sales summaries to Firestore.
    Returns (success_count, error_count).
    """
    ok = 0
    errs = 0
    for s in summaries:
        success, msg = upload_sales_summary(id_token, s["date_folder"], s)
        if success:
            ok += 1
            if log_fn:
                log_fn(f"[Firebase] Sales {s['date_folder']} → uploaded  ✓", "ok")
        else:
            errs += 1
            if log_fn:
                log_fn(f"[Firebase] Sales {s['date_folder']} — failed: {msg}", "err")
    return ok, errs


def fetch_sales(id_token: str,
                date_folders: list[str] | None = None) -> list[dict]:
    """
    Fetch sales summaries from Firestore /sales collection.
    If date_folders is given, filters to those dates only.
    Returns a list of plain dicts.
    """
    url = f"{_FIRESTORE_URL}/sales?pageSize=500"

    req = request.Request(
        url,
        headers={"Authorization": f"Bearer {id_token}"},
        method="GET",
    )

    try:
        with request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []

    summaries = []
    for doc in data.get("documents", []):
        fields = doc.get("fields", {})
        summary = {k: _parse_firestore_value(v) for k, v in fields.items()}
        summaries.append(summary)

    if date_folders:
        date_set = set(date_folders)
        summaries = [s for s in summaries
                     if s.get("date_folder") in date_set]

    return summaries
