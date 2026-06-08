# Specification: Exponential-Backoff Reconnection Strategy

## Overview
When a websocket connection drops, the frontend client attempts to reconnect. Without a properly jittered exponential backoff, a massive regional drop could result in all clients attempting to reconnect simultaneously, creating a thundering herd that overwhelms the backend.

## Requirements
- Replace the current static retry interval with a formula: `delay = min(base_delay * (2 ^ attempt) + jitter, max_delay)`.
- Ensure reconnect logic resets its attempt counter upon a successful stable connection.
- Visually indicate the reconnect countdown to the user in the UI.

## Scope
- `packages/provide-uterm-app/src/hooks/useTerminal.ts` (or equivalent client connection logic).
