# Hijack Protocol Matrix

This matrix defines the backend capability contract consumed by `hijack.js`.

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| `hello.hijack_control` | `ws` | `rest` |
| `hello.hijack_step_supported` | `true` | `true` |
| WS frame `hijack_request` | supported | rejected (`use_rest_hijack_api`) |
| WS frame `hijack_release` | supported | rejected (`use_rest_hijack_api`) |
| WS frame `hijack_step` | supported | rejected (`use_rest_hijack_api`) |
| REST `/hijack/acquire` | supported | supported |
| REST `/hijack/{id}/heartbeat` | supported | supported |
| REST `/hijack/{id}/release` | supported | supported |
| REST `/hijack/{id}/step` | supported | supported |
| REST `/hijack/{id}/send` | supported | supported |
| REST `/hijack/{id}/snapshot` | supported | **not implemented** |
| REST `/hijack/{id}/events` | supported | supported |

## Client behavior contract

- The client must key behavior on `hello.hijack_control` (or `hello.capabilities.hijack_control`).
- The client must not assume backend type by URL or deployment.
- Unsupported WS control paths must degrade to REST when `hijack_control=rest`.
