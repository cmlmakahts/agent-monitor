"""
加密工具：用 keyring 存密钥（Windows 上由 DPAPI 保护），数据用 Fernet 加密落盘。
"""
from __future__ import annotations
import base64
import os
from pathlib import Path

import keyring
from cryptography.fernet import Fernet, InvalidToken


KEYRING_SERVICE = "AgentMonitor"
KEYRING_USER = "config_master_key"


def _ensure_key() -> bytes:
    """从 Windows 凭据管理器取密钥，没有则生成一个存进去"""
    key = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    if key:
        return key.encode("ascii")
    new_key = Fernet.generate_key()
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, new_key.decode("ascii"))
    return new_key


def encrypt(plaintext: bytes) -> bytes:
    return Fernet(_ensure_key()).encrypt(plaintext)


def decrypt(ciphertext: bytes) -> bytes:
    try:
        return Fernet(_ensure_key()).decrypt(ciphertext)
    except InvalidToken:
        # keyring 不可用 / 凭据失效 → 文件作废
        raise


def save_encrypted_blob(path: Path, plaintext: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypt(plaintext))


def load_encrypted_blob(path: Path) -> bytes | None:
    if not path.exists():
        return None
    return decrypt(path.read_bytes())
