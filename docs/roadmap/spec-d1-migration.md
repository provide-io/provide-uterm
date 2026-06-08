# Specification: Cloudflare KV to D1 State Migration

## Overview
The Cloudflare Worker edge currently relies on Workers KV for session metadata and configuration persistence. KV is eventually consistent (up to 60s propagation), which creates race conditions during rapid state changes. Migrating to D1 (Cloudflare's serverless SQLite) provides stronger consistency guarantees and allows for relational querying (e.g., "list all active sessions for this user").

## Requirements
- Map the existing JSON lease/session schemas to relational D1 tables.
- Update `packages/provide-uterm-cloudflare/state/store.py` to support D1 via the `@cloudflare/sqlite` API bindings.
- Build a dual-write or cutover migration path for existing active sessions.

## Scope
- Cloudflare state management and persistent store bindings only. Does not affect DO ephemeral state.
