# provide-terminal-cloudflare

Cloudflare Workers package for running the `provide-terminal` hosted control plane.

## Commands

- `uterm-cf build`
- `uterm-cf dev`
- `uterm-cf deploy --env production`

## Runtime model

- One Durable Object per `worker_id` session.
- Durable Object SQLite stores hijack leases, snapshots, and event stream sequence.
- Browser and worker websocket traffic is arbitrated by the DO.
- Upstream runtime connector uses websocket backend for v1.

## WebSocket endpoints

- `/ws/worker/{worker_id}/term` - runtime worker protocol (JSON frames)
- `/ws/browser/{worker_id}/term` - browser/operator protocol (JSON frames)
- `/ws/raw/{worker_id}/term` - raw stream mode for `uterm listen` telnet/SSH gateways
