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

## Components

| Component | Purpose |
|-----------|---------|
| `hijack.ts` | HijackWidget — xterm.js terminal with hijack controls |
| `inspect-view.ts` | HTTP inspection UI with intercept toggle, action bar, modify editor |
| `deckmux/` | DeckMux overlay — avatar bar, edge indicators, control transfer |
| `app-header.ts` | Navigation header with session status |

## Build

```bash
npm ci                  # install dependencies
npm run build:frontend  # output to src/provide/terminal/frontend/
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
