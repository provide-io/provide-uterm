# provide-uterm-frontend

Browser UI for the [provide-uterm](../../README.md) platform. Vanilla TypeScript — no React, no framework.

## Views

| View | Route | Purpose |
|------|-------|---------|
| Dashboard | `/app/` | Session list, status overview |
| Session | `/app/session/{id}` | Read-only terminal viewer |
| Operator | `/app/operator/{id}` | Full operator console with sidebar |
| Inspect | `/app/inspect/{id}` | HTTP traffic inspection with intercept |
| Connect | `/app/connect` | Quick-connect to new sessions |
| Replay | `/app/replay/{id}` | Session replay viewer |
| VNC console | `/_terminal/vnc.html` | First-party RFB client (noVNC) via human VNC relay |

## Components

| Component | Purpose |
|-----------|---------|
| `hijack.ts` | HijackWidget — xterm.js terminal with hijack controls |
| `terminal.ts` | Terminal rendering and page wiring |
| `vnc-page.ts` / `vnc-url.ts` | VNC console page + binary WS URL helpers |
| `app/widgets/hijack-widget-host.ts` | Hosts the hijack widget and wires control-transfer events |
| `app/deckmux/` | DeckMux overlay — avatar bar, edge indicators, control transfer |

## Embedding the terminal elements

`public.ts` (the package entry) exports `<uterm-session>` (`UtermSessionElement`,
terminal plus hijack chrome) and `<uterm-terminal>` (`TerminalElement`,
terminal only). Register them with `registerUtermElements()`, set `config`, and
call `connect()` / `disconnect()`.

Both elements reconnect by themselves, with backoff, when their socket closes.
They report the stream through public DOM events, so a page never needs their
private fields:

| Event | `detail` | Cancelable | When |
|-------|----------|------------|------|
| `uterm:connection` | `{ connected: boolean }` | no | The socket opened (`true`), or an open socket went away (`false`): a close, `disconnect()`, or a `connect()` that replaced it. Changes only. |
| `uterm:close` | `{ code: number, reason: string, wasClean: boolean, willReconnect: boolean }` | yes | The server closed a socket, or it dropped (`code` 1006), whether it had opened or not. Fires before the reconnect is scheduled; `preventDefault()` cancels that reconnect. Not fired for `disconnect()`. |

Both bubble and are `composed`, and both fire for every socket the element
opens: the first `connect()`, its own reconnects, and manual reconnects. The
element's `connected` getter (read-only) gives the current state.

A `uterm:close` listener may call `disconnect()` or `connect()`, or remove the
element; the element then schedules no reconnect of its own for that close.
Removing a connected element disconnects it, and its `uterm:connection`
`{connected: false}` is dispatched after it has left the document, so only a
listener on the element itself hears that one.

Close codes in `FINAL_CLOSE_CODES` end the stream with no reconnect, listener or
not (`willReconnect` is `false`). Today that is only `4410` (`VIEWER_REVOKED_CLOSE_CODE`:
the server revoked this viewer's access, so a reconnect could only be refused).
A page may still call `connect()` again itself.

```ts
import { UTERM_CLOSE_EVENT, UTERM_CONNECTION_EVENT, VIEWER_REVOKED_CLOSE_CODE } from "provide-uterm-frontend";

element.addEventListener(UTERM_CONNECTION_EVENT, (e) => showLive(e.detail.connected));
element.addEventListener(UTERM_CLOSE_EVENT, (e) => {
  if (e.detail.code === VIEWER_REVOKED_CLOSE_CODE) showRevoked();
  else if (sessionIsOver()) e.preventDefault(); // do not reconnect
});
```

`UtermConnectionEventDetail` and `UtermCloseEventDetail` are exported as types,
and both events are declared on `HTMLElementEventMap`, so `addEventListener`
types `e.detail` without a cast. `<uterm-session>` also dispatches
`uterm:geometry` (`detail: {cols, rows}`) when the session's size changes.

## Build

```bash
npm ci                  # install dependencies
npm run build:frontend  # output to src/provide/uterm/frontend/
```

Output is served by the FastAPI server or CF Worker as static assets.

## Tests

472 vitest tests.

```bash
cd packages/provide-uterm-frontend
npx vitest run
```

## License

AGPL-3.0-or-later. Copyright (c) 2025-2026 provide.io llc.
