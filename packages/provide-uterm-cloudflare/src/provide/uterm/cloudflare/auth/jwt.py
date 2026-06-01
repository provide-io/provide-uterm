from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# Module-level JWKS cache: url → (fetched_at, jwks_dict).
# Avoids a network round-trip on every authenticated request within the same
# V8 isolate lifetime.  The cache is per-isolate (not shared across requests
# in different isolates), so the TTL only matters within a single long-lived isolate.
_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS_CACHE_TTL_S: float = 60.0
# After a refresh failure, keep serving the stale cached JWKS for this long
# before attempting another network fetch — so a transient or flapping JWKS
# endpoint cannot take down all authentication, and a known-bad endpoint is not
# re-hit on every request.
_JWKS_NEGATIVE_TTL_S: float = 5.0
# url → monotonic deadline before which a failed refresh will not be retried.
_JWKS_RETRY_AFTER: dict[str, float] = {}

# Detect CF Workers (Pyodide) runtime once at import time.
_IN_CF_RUNTIME = False
_js_crypto: Any = None
_to_js: Any = None
_js_object: Any = None
try:
    import js as _js_mod  # type: ignore[import-not-found]  # pragma: no cover

    _js_crypto = _js_mod.crypto  # pragma: no cover
    _js_object = _js_mod.Object  # pragma: no cover
    from pyodide.ffi import to_js  # type: ignore[import-not-found]  # pragma: no cover

    _to_js = to_js  # pragma: no cover

    _IN_CF_RUNTIME = True  # pragma: no cover
except ImportError:
    pass

if TYPE_CHECKING:
    from provide.uterm.cloudflare.config import JwtConfig
else:
    JwtConfig = Any


class JwtValidationError(ValueError):
    pass


@dataclass(slots=True)
class Principal:
    subject_id: str
    roles: tuple[str, ...]


def _b64url_decode(data: str) -> bytes:
    """Decode base64url without padding."""
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data)


def _parse_jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """Split a JWT into (header, payload, signature, signing_input)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise JwtValidationError("malformed JWT: expected 3 parts")
    header = json.loads(_b64url_decode(parts[0]))
    payload = json.loads(_b64url_decode(parts[1]))
    signature = _b64url_decode(parts[2])
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    return header, payload, signature, signing_input


async def _request_jwks(url: str) -> dict[str, Any]:
    """Perform the JWKS network fetch (no caching).

    In Cloudflare Workers, uses the native ``js.fetch`` (async, non-blocking).
    Outside CF (tests / local dev), falls back to ``urllib`` as a last resort.
    """
    try:
        from js import (
            fetch as _js_fetch,  # CF Workers native async fetch  # pragma: no cover
        )

        response = await _js_fetch(url)  # pragma: no cover
        result: dict[str, Any] = (await response.json()).to_py()  # pragma: no cover
        return result  # pragma: no cover
    except ImportError:
        import urllib.request

        # Reject non-http(s) schemes before opening the URL. The JWKS URL
        # comes from server config and should always be http/https; this
        # gate makes the assumption explicit and turns a bandit B310 false
        # positive into a real preflight check.
        if not (url.startswith(("http://", "https://"))):
            raise ValueError(f"JWKS URL must be http(s), got: {url!r}") from None
        _req = urllib.request.Request(url, headers={"User-Agent": "provide-uterm/1.0"})  # noqa: S310
        with urllib.request.urlopen(_req, timeout=5) as resp:  # noqa: S310  # nosec B310  # scheme validated above
            data: dict[str, Any] = json.loads(resp.read())
            return data


async def _fetch_jwks(url: str) -> dict[str, Any]:
    """Fetch JWKS with a per-isolate TTL cache and stale-on-error fallback.

    On a cache miss or TTL expiry we attempt a refresh. If that refresh fails
    but a previously-fetched copy is still held, the stale copy is served and
    further refresh attempts are suppressed for ``_JWKS_NEGATIVE_TTL_S`` — so a
    transient or flapping JWKS endpoint cannot take down all authentication. A
    first-ever fetch with no cached fallback still propagates the error.
    """
    now = time.monotonic()
    cached = _JWKS_CACHE.get(url)
    if cached is not None:
        fetched_at, jwks_dict = cached
        if now - fetched_at < _JWKS_CACHE_TTL_S:
            return jwks_dict
        # TTL expired: while a recent refresh is still backed off, keep serving
        # the stale copy rather than re-hitting a known-bad endpoint.
        if now < _JWKS_RETRY_AFTER.get(url, 0.0):
            return jwks_dict

    try:
        result = await _request_jwks(url)
    except Exception:
        if cached is not None:
            # Transient refresh failure, but a usable copy is cached: serve it
            # and back off before the next attempt.
            _JWKS_RETRY_AFTER[url] = now + _JWKS_NEGATIVE_TTL_S
            return cached[1]
        raise

    _JWKS_CACHE[url] = (time.monotonic(), result)
    _JWKS_RETRY_AFTER.pop(url, None)
    return result


def _find_jwk(jwks_data: dict[str, Any], kid: str | None, alg: str | None) -> dict[str, Any]:
    """Find a matching JWK from JWKS data by kid or algorithm."""
    keys = jwks_data.get("keys", [])
    if not keys:
        raise JwtValidationError("JWKS contains no keys")
    for key in keys:
        if kid is not None:
            if key.get("kid") == kid:
                return dict(key)
        else:
            key_alg = key.get("alg")
            if alg is None or key_alg is None or key_alg == alg:
                return dict(key)
    raise JwtValidationError("no matching key found in JWKS")


async def _verify_web_crypto(
    token: str, config: JwtConfig
) -> dict[str, Any]:  # pragma: no cover — requires Pyodide runtime
    """Verify and decode a JWT using the Web Crypto API (CF Workers / Pyodide).

    Returns the validated claims dict.
    """
    header, payload, signature, signing_input = _parse_jwt_parts(token)

    alg = header.get("alg")
    if alg not in config.algorithms:
        raise JwtValidationError(f"unsupported algorithm: {alg}")

    if alg != "RS256":
        raise JwtValidationError(f"Web Crypto path only supports RS256, got {alg}")

    if not config.jwks_url:
        raise JwtValidationError("Web Crypto verification requires jwks_url")

    jwks_data = await _fetch_jwks(config.jwks_url)
    jwk_dict = _find_jwk(jwks_data, header.get("kid"), alg)

    algo_obj = _to_js(
        {"name": "RSASSA-PKCS1-v1_5", "hash": "SHA-256"}, dict_converter=_js_object.fromEntries
    )  # pragma: no cover
    crypto_key = await _js_crypto.subtle.importKey(  # pragma: no cover
        "jwk",
        _to_js(jwk_dict, dict_converter=_js_object.fromEntries),
        algo_obj,
        False,
        _to_js(["verify"]),
    )

    valid = await _js_crypto.subtle.verify(  # pragma: no cover
        "RSASSA-PKCS1-v1_5",
        crypto_key,
        _to_js(signature),
        _to_js(signing_input),
    )
    if not valid:  # pragma: no cover
        raise JwtValidationError("signature verification failed")

    _validate_claims(payload, config)
    return payload


def _validate_claims(payload: dict[str, Any], config: JwtConfig) -> None:
    """Validate standard JWT claims (exp, nbf, iss, aud)."""
    now = time.time()
    leeway = max(0, int(config.clock_skew_seconds))
    _check_exp(payload, now, leeway)
    _check_nbf(payload, now, leeway)
    _check_issuer(payload, config)
    _check_audience(payload, config)


def _check_exp(payload: dict[str, Any], now: float, leeway: int) -> None:
    exp = payload.get("exp")
    if exp is None:
        raise JwtValidationError("missing exp claim")
    if now > exp + leeway:
        raise JwtValidationError("token has expired")


def _check_nbf(payload: dict[str, Any], now: float, leeway: int) -> None:
    nbf = payload.get("nbf")
    if nbf is not None and now < nbf - leeway:
        raise JwtValidationError("token not yet valid")


def _check_issuer(payload: dict[str, Any], config: JwtConfig) -> None:
    if config.issuer and payload.get("iss") != config.issuer:
        raise JwtValidationError("invalid issuer")


def _check_audience(payload: dict[str, Any], config: JwtConfig) -> None:
    if not config.audience:
        return
    aud = payload.get("aud")
    expected = config.audience
    if isinstance(aud, list):
        if expected not in aud:
            raise JwtValidationError("invalid audience")
    elif aud != expected:
        raise JwtValidationError("invalid audience")


async def _resolve_signing_key(token: str, config: JwtConfig) -> Any:
    """Return the signing key to use for *token* based on *config*.

    Supports both static PEM keys and JWKS URLs (key rotation / RS256 / ES256).
    Uses async HTTP for JWKS so the V8 isolate is never blocked.
    """
    import jwt as _jwt

    if config.jwks_url:
        jwks_data = await _fetch_jwks(config.jwks_url)
        try:
            jwks = _jwt.PyJWKSet.from_dict(jwks_data)
        except Exception as exc:
            raise JwtValidationError(f"no matching key found in JWKS: {exc}") from exc
        headers = _jwt.get_unverified_header(token)
        kid = headers.get("kid")
        alg = headers.get("alg")
        for key in jwks.keys:
            if kid is not None:
                if key.key_id == kid:
                    return key.key
            else:
                key_alg = getattr(key, "algorithm_name", None)
                if alg is None or key_alg is None or key_alg == alg:
                    return key.key
        raise JwtValidationError("no matching key found in JWKS")
    if config.public_key_pem:
        return config.public_key_pem
    raise JwtValidationError("jwt_public_key_pem or jwt_jwks_url must be configured in jwt mode")


async def _verify_pyjwt(token: str, config: JwtConfig) -> dict[str, Any]:
    """Verify and decode a JWT using PyJWT (tests / local dev)."""
    import jwt
    from jwt import InvalidTokenError

    key = await _resolve_signing_key(token, config)

    options = {
        "verify_aud": bool(config.audience),
        "verify_iss": bool(config.issuer),
        "require": ["sub", "exp"],
    }
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=list(config.algorithms),
            issuer=config.issuer,
            audience=config.audience,
            options=options,  # type: ignore[arg-type]  # PyJWT Options stub
            leeway=max(0, int(config.clock_skew_seconds)),
        )
    except InvalidTokenError as exc:
        raise JwtValidationError(str(exc)) from exc

    return claims


async def decode_jwt(token: str, config: JwtConfig) -> Principal:
    # No dev/none bypass: every principal must come from a cryptographically
    # verified token. The worker is always internet-facing.
    if not config.public_key_pem and not config.jwks_url:
        raise JwtValidationError("missing jwt public key")

    try:
        if _IN_CF_RUNTIME:
            claims = await _verify_web_crypto(token, config)  # pragma: no cover
        else:
            claims = await _verify_pyjwt(token, config)
    except JwtValidationError:
        raise
    except Exception as exc:
        raise JwtValidationError(f"failed to verify token: {exc}") from exc

    sub = str(claims.get("sub") or "")
    common_name = str(claims.get("common_name") or "")
    # CF Access service-token JWTs carry a ``common_name`` claim and no human
    # ``email`` claim. The presence of ``email`` (or any human identity) means a
    # user token, never a service token — so it can never be elevated as one.
    has_email = bool(claims.get("email"))
    is_service_token = bool(common_name) and not has_email

    if not sub:
        # Fall back to common_name as the subject for service tokens.
        sub = common_name
    if not sub:
        raise JwtValidationError("missing sub")

    # Service tokens get admin ONLY when explicitly opted in via config; an empty
    # ``sub`` or a bare ``common_name`` is too weak a signal to auto-grant admin.
    if is_service_token and getattr(config, "jwt_service_token_admin", False):
        roles: tuple[str, ...] = ("admin",)
    else:
        roles = _extract_roles(claims, config)
    return Principal(subject_id=sub, roles=roles)


def _parse_roles_claim(raw: object) -> tuple[str, ...]:
    """Parse a roles claim value (string, list, or other) into a tuple."""
    if isinstance(raw, str):
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if isinstance(raw, list):
        return tuple(str(role) for role in raw)
    return ()


def _apply_role_map(roles: tuple[str, ...], config: JwtConfig) -> tuple[str, ...]:
    """Apply group→role mapping and default role fallback."""
    role_map: dict[str, str] = getattr(config, "jwt_role_map", {}) or {}
    if role_map:
        roles = tuple(role_map.get(r, r) for r in roles)
    if not roles:
        roles = (getattr(config, "jwt_default_role", "viewer") or "viewer",)
    return roles


def _extract_roles(claims: dict[str, Any], config: JwtConfig) -> tuple[str, ...]:
    """Extract roles from JWT claims with fallback to scopes."""
    roles = _parse_roles_claim(claims.get(getattr(config, "jwt_roles_claim", "roles"), []))
    if not roles:
        raw_scope = claims.get(getattr(config, "jwt_scopes_claim", "scope"), "")
        if isinstance(raw_scope, str) and raw_scope:
            roles = tuple(part.strip() for part in raw_scope.split() if part.strip())
    return _apply_role_map(roles, config)


def resolve_role(principal: Principal) -> str:
    role_set = set(principal.roles)
    if "admin" in role_set:
        return "admin"
    if "operator" in role_set:
        return "operator"
    return "viewer"


def extract_bearer_or_cookie(request: object) -> str | None:
    """Extract a JWT from the Authorization: Bearer header or CF_Authorization cookie.

    Used by both the Default worker (entry.py) and the SessionRuntime DO
    (session_runtime.py).  Browser WebSockets cannot send custom headers, so
    the CF_Authorization cookie is the only auth mechanism available for WS
    upgrade requests protected by Cloudflare Access.
    """
    try:
        auth_header = str(request.headers.get("Authorization") or "")  # type: ignore[attr-defined]
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            if token:
                return token
    except Exception:
        pass
    try:
        cookie_header = str(request.headers.get("Cookie") or "")  # type: ignore[attr-defined]
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name.strip() == "CF_Authorization" and value.strip():
                return value.strip()
    except Exception:
        pass
    return None
