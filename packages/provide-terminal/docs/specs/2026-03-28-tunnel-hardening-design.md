# Tunnel Token Hardening Design

## Context

The tunnel sharing system has 19 security gaps identified by audit. The three original P1 auth bugs (tokens not enforced, per-session tokens ignored, share page auth not propagating) were fixed, but the fixes lack lifecycle management. This spec hardens the token system to enterprise grade.

## Token Lifecycle

- **TTL**: Default 3600s (1 hour), configurable server-wide via `TunnelConfig.token_ttl_s`, overridable per-tunnel via `POST /api/tunnels` payload `ttl_s` field
- **Cleanup**: Tokens removed on session deletion. Periodic sweep every 60s removes expired tokens (FastAPI). KV entries checked for expiry on load (CF)
- **Revocation**: `DELETE /api/tunnels/{id}/tokens` immediately invalidates all tokens
- **Rotation**: `POST /api/tunnels/{id}/tokens/rotate` generates new tokens, returns new URLs

## Token Transport (Configurable)

`TunnelConfig.token_transport`: `"query"` | `"cookie"` | `"both"` (default: `"both"`)

- **query**: Token in URL query param on every request (current behavior)
- **cookie**: HttpOnly cookie set on first page load, no token in bootstrap JSON
- **both**: Cookie set AND token in query param (backward compatible)

Cookie attributes: `HttpOnly; Secure; SameSite=Lax; Max-Age={ttl}`. CF stays query-only.

## Security Fixes

- **Timing attack**: CF `resolve_share_context()` uses `secrets.compare_digest()` instead of `==`
- **Enumeration**: Share routes return 404 for both "not found" and "invalid token"
- **CSRF**: Optional Origin header check on `/tunnel/{id}` WS upgrade
- **IP binding**: Optional, off by default. Stores `issued_ip` at creation, validates on access
- **Token extraction**: `parse_qs()` instead of `str.split("token=")`

## Audit Logging

All token operations logged at INFO with structured fields:
`tunnel_token_{action} session_id={} token_type={} source_ip={} valid={} reason={}`

Actions: `created`, `validated`, `expired`, `revoked`, `rotated`

## Shared Types

`tunnel/types.py`: `TunnelTokenState` and `TunnelCreateResponse` TypedDicts used by both FastAPI and CF.

## Config

```python
class TunnelConfig(BaseModel):
    token_ttl_s: int = 3600
    token_transport: Literal["query", "cookie", "both"] = "both"
    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    ip_binding: bool = False
```

Added to `ServerConfig.tunnel`. CF equivalent from env vars.

## Verification

1. Token expiry enforced on both backends
2. Revocation immediately rejects access
3. Rotation invalidates old tokens, new tokens work
4. Cookie mode sets HttpOnly cookie, subsequent requests work without query param
5. IP binding rejects requests from different IPs
6. Timing-safe comparison on all token checks
7. No 403 responses that reveal session existence
8. Audit logs emitted for all token operations
