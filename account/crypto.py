# -*- coding: utf-8 -*-
"""敏感信息加解密：避免账号 Cookie、S3 SecretKey 等明文落库。

密钥来源（优先级）：环境变量 COOKIE_ENC_KEY > data/.cookie_key（首次自动生成）。
"""
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


def encrypt_text(plain: str) -> str:
    """通用加密（Cookie / S3 SecretKey 等敏感串共用）。"""
    if not plain:
        return ""
    return Fernet(_load_or_create_key()).encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_text(cipher: str) -> str:
    """通用解密；key 变更或数据损坏时返回空串，由上层按「未配置」处理。"""
    if not cipher:
        return ""
    try:
        return Fernet(_load_or_create_key()).decrypt(cipher.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


def encrypt_cookie(plain: str) -> str:
    return encrypt_text(plain)


def decrypt_cookie(cipher: str) -> str:
    return decrypt_text(cipher)
