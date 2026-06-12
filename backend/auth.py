"""Access management: PIN setup/verify and signed session tokens."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from . import config, database as db

_PBKDF_ROUNDS = 200_000


def _secret() -> bytes:
    s = db.get_setting("auth_secret")
    if not s:
        s = secrets.token_hex(32)
        db.set_setting("auth_secret", s)
    return s.encode()


def is_configured() -> bool:
    return bool(db.get_setting("auth_pin_hash"))


def setup_pin(pin: str) -> None:
    if not pin or len(pin) < 4:
        raise ValueError("PIN must be at least 4 characters.")
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), _PBKDF_ROUNDS).hex()
    db.set_setting("auth_salt", salt)
    db.set_setting("auth_pin_hash", h)


def verify_pin(pin: str) -> bool:
    salt = db.get_setting("auth_salt")
    stored = db.get_setting("auth_pin_hash")
    if not salt or not stored:
        return False
    h = hashlib.pbkdf2_hmac("sha256", (pin or "").encode(), salt.encode(), _PBKDF_ROUNDS).hex()
    return hmac.compare_digest(h, stored)


def change_pin(old_pin: str, new_pin: str) -> None:
    if not verify_pin(old_pin):
        raise PermissionError("Current PIN is incorrect.")
    setup_pin(new_pin)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_token() -> str:
    payload = {"iat": int(time.time()), "exp": int(time.time()) + config.TOKEN_TTL,
               "nonce": secrets.token_hex(4)}
    body = _b64(json.dumps(payload).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str) -> bool:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return False
        payload = json.loads(_unb64(body))
        return payload.get("exp", 0) >= time.time()
    except Exception:
        return False
