from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.core.config import get_settings


def session_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="fbr-dss-session")


def sign_session(payload: dict[str, Any]) -> str:
    return session_serializer().dumps(payload)


def load_session(token: str, max_age: int = 60 * 60 * 12) -> dict[str, Any] | None:
    try:
        data = session_serializer().loads(token, max_age=max_age)
        return data if isinstance(data, dict) else None
    except BadSignature:
        return None


def decrypt_hrms_token(cipher_b64: str, passphrase: str) -> str:
    """Rijndael-compatible AES-CBC decrypt used by HRMS launch tokens.

    Derives a 32-byte key via PBKDF2-SHA1 (1000 iterations) with the first 16
    bytes of the ciphertext as salt/IV pattern matching the legacy FBR client.
    """
    raw = base64.b64decode(cipher_b64)
    if len(raw) < 32:
        raise ValueError("ciphertext too short")
    salt = raw[:16]
    iv = raw[16:32]
    ciphertext = raw[32:]
    key = hashlib.pbkdf2_hmac("sha1", passphrase.encode("utf-8"), salt, 1000, dklen=32)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    data = unpadder.update(padded) + unpadder.finalize()
    return data.decode("utf-8", errors="replace")
