from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag


@pytest.fixture(autouse=True)
def fresh_master_key(monkeypatch: pytest.MonkeyPatch):
    """Provide a fresh QM_MASTER_KEY for every test and reset the module-level cache."""
    from app.services import crypto

    key = crypto.generate_master_key()
    monkeypatch.setenv("QM_MASTER_KEY", key)
    # Re-import settings so the value picks up the new env (pydantic-settings caches).
    from app import config

    config.get_settings.cache_clear()  # type: ignore[attr-defined]
    config.settings = config.get_settings()
    crypto._KEY_BY_VERSION.clear()
    yield
    crypto._KEY_BY_VERSION.clear()


def test_roundtrip_bytes() -> None:
    from app.services.crypto import decrypt, encrypt

    plaintext = os.urandom(64)
    ct, nonce, ver = encrypt(plaintext)
    assert ver == 1
    assert decrypt(ct, nonce) == plaintext


def test_roundtrip_str() -> None:
    from app.services.crypto import decrypt, encrypt

    ct, nonce, _ = encrypt("hello world")
    assert decrypt(ct, nonce) == b"hello world"


def test_nonce_is_random() -> None:
    from app.services.crypto import encrypt

    ct1, n1, _ = encrypt(b"same")
    ct2, n2, _ = encrypt(b"same")
    assert n1 != n2
    assert ct1 != ct2


def test_tamper_ciphertext_rejected() -> None:
    from app.services.crypto import decrypt, encrypt

    ct, nonce, _ = encrypt(b"secret")
    bad = bytearray(ct)
    bad[0] ^= 0x01
    with pytest.raises(InvalidTag):
        decrypt(bytes(bad), nonce)


def test_tamper_nonce_rejected() -> None:
    from app.services.crypto import decrypt, encrypt

    ct, _, _ = encrypt(b"secret")
    bad_nonce = os.urandom(12)
    with pytest.raises(InvalidTag):
        decrypt(ct, bad_nonce)


def test_aad_mismatch_rejected() -> None:
    from app.services.crypto import decrypt, encrypt

    ct, nonce, _ = encrypt(b"secret", aad=b"workspace-1")
    with pytest.raises(InvalidTag):
        decrypt(ct, nonce, aad=b"workspace-2")


def test_unset_master_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import config
    from app.services import crypto

    monkeypatch.delenv("QM_MASTER_KEY", raising=False)
    config.get_settings.cache_clear()  # type: ignore[attr-defined]
    # Bypass backend/.env so "unset" means truly unset, not "fell back to .env".
    config.settings = config.Settings(_env_file=None)  # type: ignore[call-arg]
    crypto._KEY_BY_VERSION.clear()
    with pytest.raises(RuntimeError, match="QM_MASTER_KEY"):
        crypto.encrypt(b"x")


def test_short_master_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    from app import config
    from app.services import crypto

    # 24 bytes encoded → 32 base64 chars (decodes cleanly, but wrong length).
    short = base64.b64encode(os.urandom(24)).decode()
    monkeypatch.setenv("QM_MASTER_KEY", short)
    config.get_settings.cache_clear()  # type: ignore[attr-defined]
    config.settings = config.get_settings()
    crypto._KEY_BY_VERSION.clear()
    with pytest.raises(RuntimeError, match="32 bytes"):
        crypto.encrypt(b"x")
