"""轻量安全工具：pbkdf2 口令哈希 + 随机会话 token（零三方依赖）。"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

_ALGO = "sha256"
_ITERS = 120_000


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode(), bytes.fromhex(salt), _ITERS)
    return f"pbkdf2_{_ALGO}${_ITERS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt, digest = stored.split("$")
        if scheme != f"pbkdf2_{_ALGO}":
            return False
        dk = hashlib.pbkdf2_hmac(_ALGO, password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(42)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
