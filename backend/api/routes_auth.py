"""
routes_auth.py — Simple token-based auth for protected jTAK pages.

Credentials stored in jtak.yaml:
  admin:
    username: admin
    password: LetMeIn

Secret stored in /run/jtak/auth_secret.bin (tmpfs):
  - Survives jtak-api service restarts → session stays valid
  - Cleared on hub reboot → re-login required after reboot
  - Browser window close → sessionStorage cleared → re-login required
"""

import base64
import hashlib
import hmac
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

from utils.config import get

router = APIRouter()

_SECRET_PATH = Path("/run/jtak/auth_secret.bin")
_TOKEN_TTL   = 8 * 3600  # 8 hours


def _load_secret() -> bytes:
    """/run is tmpfs — persists across service restarts, cleared on reboot."""
    try:
        if _SECRET_PATH.exists():
            return _SECRET_PATH.read_bytes()
    except Exception:
        pass
    secret = secrets.token_bytes(32)
    try:
        _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_PATH.write_bytes(secret)
    except Exception:
        pass  # fall back to in-memory only
    return secret


_SECRET = _load_secret()


# ── Token helpers (HMAC-SHA256, base64url encoded) ────────────────────────────

def _make_token(username: str) -> str:
    exp = int(time.time()) + _TOKEN_TTL
    payload = f"{username}:{exp}"
    sig = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        # Format: username:exp:sig  (sig is hex, no colons)
        parts = raw.split(":")
        sig = parts[-1]
        username_exp = ":".join(parts[:-1])
        exp = int(parts[-2])
        if exp < time.time():
            return False
        expected = hmac.new(_SECRET, username_exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def require_auth(authorization: str = Header(None)):
    """FastAPI dependency — raises 401 if token missing/invalid."""
    tok = ""
    if authorization:
        tok = authorization[7:] if authorization.startswith("Bearer ") else authorization
    if not verify_token(tok):
        raise HTTPException(status_code=401, detail="Authentication required")


# ── Endpoints ─────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(req: LoginRequest):
    expected_user = get("admin.username", "admin")
    expected_pass = get("admin.password", "LetMeIn")
    if req.username != expected_user or req.password != expected_pass:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"token": _make_token(req.username), "expires_in": _TOKEN_TTL}


@router.get("/auth/verify")
async def verify(authorization: str = Header(None)):
    tok = ""
    if authorization:
        tok = authorization[7:] if authorization.startswith("Bearer ") else authorization
    if not verify_token(tok):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"ok": True}
