# Specification: Tunnels Endpoint Rate Limiting

## Overview
The `/api/tunnels` endpoints are responsible for provisioning new external connections. This is a computationally and network-heavy operation. Without explicit rate limits, an attacker could volumetrically exhaust infrastructure resources (DoS).

## Requirements
- Implement a token-bucket or fixed-window rate limiter on tunnel provisioning routes.
- Return HTTP 429 (Too Many Requests) when thresholds are exceeded.
- Provide configurable limit tiers based on user roles or IP address reputation.

## Scope
- FastAPI endpoints for tunnels. Cloudflare Worker routing rules.
