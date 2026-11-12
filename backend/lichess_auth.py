"""
Lichess OAuth2 PKCE flow and Board API scope verification.
"""
import base64
import hashlib
import os
import secrets
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

from backend.db import get_setting, set_setting
from backend.lichess_client import ACCOUNT_TIMEOUT, LichessClient, redact

LICHESS_BASE_URL = "https://lichess.org"
OAUTH_CLIENT_ID = "repertoire-studio-local"
REQUIRED_PLAY_SCOPES = ("board:play", "challenge:write")
PKCE_TTL_SECONDS = 600

_PKCE_LOCK = threading.Lock()
_PENDING_PKCE: Dict[str, Dict[str, Any]] = {}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _prune_expired_pkce(now: float) -> None:
    expired = [k for k, v in _PENDING_PKCE.items() if now - v["created_at"] > PKCE_TTL_SECONDS]
    for k in expired:
        _PENDING_PKCE.pop(k, None)


def start_oauth_pkce(redirect_uri: str) -> Dict[str, str]:
    """Generate an OAuth2 PKCE challenge and Lichess authorization URL."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(24)

    now = time.time()
    with _PKCE_LOCK:
        _prune_expired_pkce(now)
        _PENDING_PKCE[state] = {
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "created_at": now,
        }

    params = {
        "response_type": "code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": " ".join(REQUIRED_PLAY_SCOPES),
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    }
    auth_url = f"{LICHESS_BASE_URL}/oauth?{urllib.parse.urlencode(params)}"
    return {"auth_url": auth_url, "state": state}


def complete_oauth_pkce(code: str, state: str) -> Tuple[bool, str, Optional[str]]:
    """Exchange Lichess OAuth authorization code for an access token."""
    if not code or not state:
        return False, "Missing authorization code or state.", None

    with _PKCE_LOCK:
        _prune_expired_pkce(time.time())
        session = _PENDING_PKCE.pop(state, None)

    if not session:
        return False, "Authorization session expired or invalid. Try connecting again.", None

    try:
        resp = requests.post(
            f"{LICHESS_BASE_URL}/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": session["code_verifier"],
                "redirect_uri": session["redirect_uri"],
                "client_id": OAUTH_CLIENT_ID,
            },
            headers={"User-Agent": "ChessImprovementApp/1.0"},
            timeout=ACCOUNT_TIMEOUT,
        )
    except Exception as exc:
        return False, f"Network error talking to Lichess: {redact(exc)}", None

    if resp.status_code != 200:
        return False, f"Lichess token exchange failed (HTTP {resp.status_code}).", None

    try:
        data = resp.json()
    except Exception:
        return False, "Invalid JSON from Lichess token endpoint.", None

    access_token = (data.get("access_token") or "").strip()
    if not access_token:
        return False, "No access token returned by Lichess.", None

    set_setting("lichess_token", access_token)
    client = LichessClient(token=access_token)
    profile = client.get_account_profile()
    username = (profile or {}).get("username") or get_setting("lichess_username") or ""
    if username:
        set_setting("lichess_username", username)
    return True, "Connected to Lichess.", username


def inspect_token_capabilities(token: Optional[str] = None) -> Dict[str, Any]:
    """Query `/api/account` and inspect `X-OAuth-Scopes` for `board:play`."""
    client = LichessClient(token=token)
    if not client.token:
        return {
            "connected": False,
            "token_present": False,
            "username": get_setting("lichess_username") or "",
            "scopes": [],
            "can_play": False,
            "can_challenge": False,
            "perfs": {},
        }

    try:
        resp = requests.get(
            f"{client.base_url}/api/account",
            headers=client.headers,
            timeout=ACCOUNT_TIMEOUT,
        )
    except Exception:
        return {
            "connected": False,
            "token_present": True,
            "username": get_setting("lichess_username") or "",
            "scopes": [],
            "can_play": False,
            "can_challenge": False,
            "perfs": {},
        }

    if resp.status_code != 200:
        return {
            "connected": False,
            "token_present": True,
            "username": get_setting("lichess_username") or "",
            "scopes": [],
            "can_play": False,
            "can_challenge": False,
            "perfs": {},
        }

    raw_scopes = resp.headers.get("X-OAuth-Scopes") or resp.headers.get("x-oauth-scopes") or ""
    scopes: List[str] = [s.strip() for s in raw_scopes.replace(" ", ",").split(",") if s.strip()]
    data = resp.json() if resp.content else {}
    username = data.get("username") or get_setting("lichess_username") or ""
    if username:
        set_setting("lichess_username", username)

    return {
        "connected": True,
        "token_present": True,
        "username": username,
        "scopes": scopes,
        "can_play": "board:play" in scopes,
        "can_challenge": "challenge:write" in scopes or "board:play" in scopes,
        "perfs": data.get("perfs") or {},
    }
