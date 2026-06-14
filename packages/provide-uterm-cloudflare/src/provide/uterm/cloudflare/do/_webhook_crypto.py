#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""At-rest encryption for webhook HMAC signing secrets (CF Durable Object).

The webhook ``secret`` is an HMAC signing key: it must be recoverable in
plaintext to sign each delivery, so it cannot be one-way hashed like the tunnel
bearer tokens. Cloudflare already encrypts Durable Object storage at rest; this
layer adds app-level **AES-256-GCM** on top, keyed by the ``WEBHOOK_SECRET_KEY``
Worker secret binding, so a raw SQLite/KV dump never exposes the signing keys.

Envelope format::

    enc:v1:<base64(iv)>:<base64(ciphertext)>

Compatibility:

* No ``WEBHOOK_SECRET_KEY`` configured → the secret is stored/returned as-is
  (single-tenant / no-key deployments keep working; signing still functions).
* Legacy plaintext rows (no ``enc:v1:`` prefix) are returned unchanged on read.
* An encrypted row with no key configured returns ``None`` on read (skip the
  signature rather than emit a wrong one).

The AES-GCM primitives use the Web Crypto API (``crypto.subtle``) and are
``# pragma: no cover`` — they run only inside the CF Pyodide runtime and are
exercised by the ``real_cf`` integration tests, matching ``auth/jwt.py``. The
*wiring* around them (key lookup, envelope encode/decode, fallbacks) is pure
Python and unit-tested.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

_ENVELOPE_PREFIX = "enc:v1:"

# Web Crypto handles — None outside the CF Worker (Pyodide) runtime.
_js_crypto: Any = None
_to_js: Any = None
_js_object: Any = None
try:  # pragma: no cover - CF runtime only
    import js as _js_mod  # type: ignore[import-not-found]  # ty:ignore[unresolved-import]

    _js_crypto = _js_mod.crypto
    _js_object = _js_mod.Object
    from pyodide.ffi import to_js  # type: ignore[import-not-found]  # ty:ignore[unresolved-import]

    _to_js = to_js
except ImportError:
    pass


def webhook_key_b64(env: Any) -> str | None:
    """Return the base64 AES-256 key from the ``WEBHOOK_SECRET_KEY`` binding, or None."""
    raw = getattr(env, "WEBHOOK_SECRET_KEY", None)
    key = str(raw).strip() if raw else ""
    return key or None


def is_encrypted(stored: str) -> bool:
    """True when *stored* carries the encryption envelope (vs legacy plaintext)."""
    return stored.startswith(_ENVELOPE_PREFIX)


async def encrypt_secret(env: Any, plaintext: str) -> str:
    """Return the encrypted envelope for *plaintext*, or it unchanged when no
    key is configured (so single-tenant deployments still sign deliveries)."""
    key_b64 = webhook_key_b64(env)
    if not key_b64:
        return plaintext
    iv, ciphertext = await _aesgcm_encrypt(key_b64, plaintext.encode())
    return f"{_ENVELOPE_PREFIX}{base64.b64encode(iv).decode()}:{base64.b64encode(ciphertext).decode()}"


async def decrypt_secret(env: Any, stored: str) -> str | None:
    """Return the plaintext signing secret for a stored value.

    Legacy plaintext is returned unchanged; an envelope with no key configured
    (or a malformed envelope) returns ``None``.
    """
    if not is_encrypted(stored):
        return stored
    key_b64 = webhook_key_b64(env)
    if not key_b64:
        logger.warning("encrypted webhook secret present but WEBHOOK_SECRET_KEY unset — skipping signature")
        return None
    parts = stored.split(":", 3)
    if len(parts) != 4:
        logger.warning("malformed encrypted webhook secret envelope — skipping signature")
        return None
    try:
        iv = base64.b64decode(parts[2])
        ciphertext = base64.b64decode(parts[3])
        plaintext = await _aesgcm_decrypt(key_b64, iv, ciphertext)
        return plaintext.decode()
    except Exception as exc:
        # Corrupted ciphertext / wrong key — skip the signature rather than
        # crash delivery or emit a garbage one.
        logger.warning("webhook secret decryption failed: %s", exc)
        return None


async def _import_key(key_b64: str, usages: list[str]) -> Any:  # pragma: no cover - CF Web Crypto only
    raw = base64.b64decode(key_b64)
    return await _js_crypto.subtle.importKey(
        "raw",
        _to_js(raw),
        _to_js({"name": "AES-GCM"}, dict_converter=_js_object.fromEntries),
        False,
        _to_js(usages),
    )


async def _aesgcm_encrypt(
    key_b64: str, plaintext: bytes
) -> tuple[bytes, bytes]:  # pragma: no cover - CF Web Crypto only
    key = await _import_key(key_b64, ["encrypt"])
    iv = bytes(_js_crypto.getRandomValues(_to_js(bytearray(12))).to_py())
    algo = _to_js({"name": "AES-GCM", "iv": _to_js(iv)}, dict_converter=_js_object.fromEntries)
    ciphertext_buf = await _js_crypto.subtle.encrypt(algo, key, _to_js(plaintext))
    return iv, bytes(ciphertext_buf.to_py())


async def _aesgcm_decrypt(key_b64: str, iv: bytes, ciphertext: bytes) -> bytes:  # pragma: no cover - CF Web Crypto only
    key = await _import_key(key_b64, ["decrypt"])
    algo = _to_js({"name": "AES-GCM", "iv": _to_js(iv)}, dict_converter=_js_object.fromEntries)
    plaintext_buf = await _js_crypto.subtle.decrypt(algo, key, _to_js(ciphertext))
    return bytes(plaintext_buf.to_py())
