from __future__ import annotations

import base64
import os
from typing import Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import config

_KEY_BY_VERSION: dict[int, bytes] = {}

_PLACEHOLDER = "REPLACE_WITH_BASE64_32_BYTE_KEY"


def _load_keys() -> None:
    if 1 in _KEY_BY_VERSION:
        return
    raw = config.settings.QM_MASTER_KEY
    if not raw or raw == _PLACEHOLDER:
        raise RuntimeError("QM_MASTER_KEY env var is not set")
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        raise RuntimeError(f"QM_MASTER_KEY must be valid base64: {e}") from e
    if len(key) != 32:
        raise RuntimeError(
            f"QM_MASTER_KEY must decode to 32 bytes (got {len(key)})"
        )
    _KEY_BY_VERSION[1] = key


def encrypt(
    plaintext: bytes | str, *, aad: bytes | None = None
) -> tuple[bytes, bytes, int]:
    _load_keys()
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    nonce = os.urandom(12)
    ct = AESGCM(_KEY_BY_VERSION[1]).encrypt(nonce, plaintext, aad)
    return ct, nonce, 1


def decrypt(
    ciphertext: bytes,
    nonce: bytes,
    *,
    key_version: int = 1,
    aad: bytes | None = None,
) -> bytes:
    _load_keys()
    if key_version not in _KEY_BY_VERSION:
        raise RuntimeError(f"Unknown key_version {key_version}")
    return AESGCM(_KEY_BY_VERSION[key_version]).decrypt(nonce, ciphertext, aad)


def decrypt_with_aads(
    ciphertext: bytes,
    nonce: bytes,
    *,
    key_version: int = 1,
    aads: Iterable[bytes | None],
) -> bytes:
    """Decrypt against a list of candidate AADs.

    Returns the plaintext from the first AAD whose authentication tag
    verifies. Raises :class:`cryptography.exceptions.InvalidTag` from
    the LAST failed attempt if none verify.

    Use case: Phase 1's migration moved ``WorkspaceCredentials`` from
    workspace_id-keyed to connection_id-keyed without re-encrypting
    the stored payloads. Existing rows authenticate with
    ``aad=workspace_id`` while newly-created rows use
    ``aad=connection_id``. Callers pass both candidates; this helper
    transparently handles either.
    """
    _load_keys()
    if key_version not in _KEY_BY_VERSION:
        raise RuntimeError(f"Unknown key_version {key_version}")
    gcm = AESGCM(_KEY_BY_VERSION[key_version])
    last_error: InvalidTag | None = None
    for aad in aads:
        try:
            return gcm.decrypt(nonce, ciphertext, aad)
        except InvalidTag as e:
            last_error = e
            continue
    assert last_error is not None  # iterable must contain at least one
    raise last_error


def generate_master_key() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")
