# -*- coding: utf-8 -*-
"""Cookie 加解密：避免账号 Cookie 明文落库。"""
from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet

_KEY_FILE = Path(__file__).resolve().parents[1] / "data" / ".cookie_key"


def _load_or_create_key() -> bytes:
    import os

    env_key = os.getenv("COOKIE_ENC_KEY")
    if env_key:
        return env_key.encode("utf-8")

    if _KEY_FILE.exists():
        return _KEY_FILE.read_text(encoding="utf-8").strip().encode("utf-8")

    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    _KEY_FILE.write_text(key.decode("utf-8"), encoding="utf-8")
    return key


def encrypt_cookie(plain: str) -> str:
    if not plain:
        return ""
    return Fernet(_load_or_create_key()).encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_cookie(cipher: str) -> str:
    if not cipher:
        return ""
    try:
        return Fernet(_load_or_create_key()).decrypt(cipher.encode("utf-8")).decode("utf-8")
    except Exception:
        # key 变更或数据损坏时返回空，让上层按"未登录"处理，不要把密文当 Cookie 用
        return ""
