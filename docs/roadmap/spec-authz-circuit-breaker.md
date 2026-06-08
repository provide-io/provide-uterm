# Specification: Authz LRU Cache & Circuit Breaker

## Overview
Authentication and Authorization (such as fetching external JWKS or validating session tokens) currently rely on synchronous upstream network requests. During an upstream outage or an "auth storm" (thundering herd of reconnecting clients), this can exhaust limits or cascade failures.

## Requirements
- Implement an in-memory LRU cache for short-lived (e.g., 60-second) caching of successfully validated tokens.
- Add a circuit-breaker mechanism: if the upstream identity provider throws consecutive 5xx errors, gracefully fall back or explicitly reject connections without waiting for timeouts.

## Scope
- Impacts the JWT validation routines in both FastAPI (`auth/jwt.py`) and Cloudflare Workers middleware.
