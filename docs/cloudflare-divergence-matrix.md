# Cloudflare Divergence Matrix

Where the **Cloudflare Worker** backend deliberately behaves differently from the
**FastAPI** backend, and the executable test that pins each difference.

This is a *divergence* table, not a capability table. Surfaces that behave
identically on both backends are out of scope — see
[`docs/protocol-matrix.md`](protocol-matrix.md) for the full capability contract
and [`spec/session_lifecycle_security_scenarios.json`](../spec/session_lifecycle_security_scenarios.json)
for the machine-readable per-backend scenario claims.

Every row is backed by a test in
`packages/provide-uterm-cloudflare/tests/`. Run them with:

```bash
uv run --frozen --package provide-uterm-cloudflare --extra dev \
  pytest -q packages/provide-uterm-cloudflare/tests/test_edge_divergence_matrix.py
```

## The root cause of most rows

A Cloudflare Worker is **always internet-facing** (there is no loopback bind, so
no "local dev is safe" escape hatch) and a Durable Object is **per-session and
evictable** (no process uptime, no cross-session registry, no long-lived
in-memory state). Nearly every row below follows from one of those two facts.

## Matrix

| # | Divergence | FastAPI behavior | Cloudflare behavior | Why (intentional reason) | Code evidence | Regression test |
|---|---|---|---|---|---|---|
| 1 | **Auth modes** | `jwt`, `dev_token` (loopback-gated stub IdP), `header` (proxy-trust, loopback or `trusted_proxy_ips`), `api_key`, `webhook` | `jwt` **only**; any other `AUTH_MODE` raises `ValueError` at config load, so the Worker fails to boot | Every non-`jwt` mode is safe only because it can be pinned to a loopback bind or a trusted proxy hop. The Worker has neither, so those modes would be an unauthenticated admin bypass on the public internet. | CF: `packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/config.py:212-219`<br>FastAPI: `packages/provide-uterm-server/src/provide/uterm/server/app/auth.py:143-197` | `test_edge_divergence_matrix.py::test_edge_rejects_every_auth_mode_except_jwt`<br>`::test_edge_accepts_jwt_auth_mode_case_insensitively`<br>`::test_edge_defaults_to_jwt_when_auth_mode_is_unset`<br>(also `test_security_hardening.py::test_config_rejects_dev_mode_in_all_environments`) |
| 2 | **Worker bearer-token entropy floor** | 32-char + placeholder floor applied only on a *production-like* config (`require_jwt_in_production`, or a non-loopback bind) | Same floor applied **unconditionally** whenever `WORKER_BEARER_TOKEN` is set, in every `ENVIRONMENT` | The floor guards an edge auth boundary that is reachable from the internet the moment it is deployed. There is no `ENVIRONMENT` in which a weak Worker token is contained. | CF: `packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/config.py:44-67`<br>FastAPI: `packages/provide-uterm-server/src/provide/uterm/server/app/auth.py:96-99` | `test_edge_divergence_matrix.py::test_edge_bearer_token_floor_applies_in_every_environment`<br>`::test_edge_bearer_token_placeholder_rejected_in_every_environment`<br>(also `test_security_hardening.py::TestWorkerBearerTokenFloor`) |
| 3 | **`hello.hijack_control`** — *no divergence; documented here because `protocol-matrix.md` claims one* | `"ws"`; WS `hijack_request` / `hijack_release` / `hijack_step` served | `"ws"`; the same three WS frames served, with the same admin/owner fencing | The Worker reached WS-hijack parity; the "Hijack control" table in `docs/protocol-matrix.md` still advertises `hijack_control=rest` and a `use_rest_hijack_api` refusal. **That refusal code exists nowhere in the tree.** The row is kept (with tests) so nobody "fixes" the drift by regressing the Worker to match the stale doc. | CF hello emitters: `do/session_runtime/fetch.py:390`, `do/session_runtime/lifecycle.py:145`, `api/ws_routes.py:406`<br>CF WS handler: `api/ws_routes.py:158-159,193`<br>FastAPI: `server/bridge/routes/websockets_impl.py:339` | `test_edge_divergence_matrix.py::test_edge_hello_advertises_ws_hijack_control`<br>`::test_edge_never_emits_a_rest_hijack_refusal_code`<br>(WS hijack served end-to-end: `test_owned_input_fencing_hijack_tunnel.py::test_browser_hijack_control_is_public_and_owner_fenced`) |
| 4 | **`hello.mcp_supported` / `hello.vnc_supported`** | Both default to `true` | Both keys **absent** from every `hello` the Worker emits | The Worker hosts no MCP server and no RFB relay. Omitting the key (rather than sending `false`) keeps the edge hello minimal; clients must read "absent" as "unsupported", which is what the frontend already does. Note the "Hijack control" table in `docs/protocol-matrix.md` records these as `false` on Cloudflare — the values are never sent at all, so a client that reads them as present-and-false is reading a doc, not the wire. | CF: `do/session_runtime/fetch.py:383-402`, `do/session_runtime/lifecycle.py:137-156`, `api/ws_routes.py:396-417`<br>FastAPI: `server/bridge/frames.py:279-280` | `test_edge_divergence_matrix.py::test_edge_hello_omits_mcp_and_vnc_capability_flags` |
| 5 | **VNC / RFB browser relay** | `WS /worker/{id}/hijack/{hijack_id}/gui/vnc` — authz + RFB frame filter, optional upstream dial | No such route; the path 404s. No RFB implementation ships in the Worker at all | The relay proxies a long-lived binary stream to an operator-supplied upstream host. That is a Worker-outbound socket to an arbitrary address with no egress control — deliberately not offered at the edge. | CF: no route in `entry/registry.py:23-30`; DO dispatch falls through to 404 at `api/http_routes/_dispatch.py:129`<br>FastAPI: `server/bridge/routes/ws_gui_vnc.py:153` | `test_edge_divergence_matrix.py::test_edge_has_no_vnc_rfb_relay_route`<br>`::test_edge_ships_no_rfb_implementation` |
| 6 | **`/api/lifecycle/browser-quota`** | Per-principal browser quota is enforced inside the hub on WS connect, with setup rollback | Route returns **501** `{"error": "per_principal_browser_quota_unsupported", "supported": false}` | Quota is *per principal across sessions*. A Durable Object is per session and cannot see another session's connection count, so the edge cannot enforce it — and silently accepting would be a false security guarantee. | CF: `entry/handlers.py:172-177`<br>FastAPI: `server/bridge/hub/connection.py:387,416` | `test_worker_route_defs.py::test_lifecycle_unsupported_capability_routes_return_501[/api/lifecycle/browser-quota-per_principal_browser_quota_unsupported]`<br>`test_edge_divergence_matrix.py::test_edge_lifecycle_refusals_match_the_shared_contract` |
| 7 | **`/api/lifecycle/governance`** | Configured signed governance (policy / authz / audit webhooks) gates input, failing closed when the policy service is unavailable | Route returns **501** `{"error": "unsupported_governance", "supported": false}` | Governance requires a signed, synchronous outbound webhook on the input path. That is not wired at the edge, and a fail-*open* stand-in would defeat the control's purpose. | CF: `entry/handlers.py:172-177`<br>FastAPI: `server/config_schema.py:438` (`GovernanceConfig`) | `test_worker_route_defs.py::test_lifecycle_unsupported_capability_routes_return_501[/api/lifecycle/governance-unsupported_governance]`<br>`test_edge_divergence_matrix.py::test_edge_lifecycle_refusals_match_the_shared_contract` |
| 8 | **`/api/lifecycle/capabilities`** | No such route (the FastAPI backend supports both capabilities, so it has nothing to refuse) | Returns 200 with `browser_quota.supported: false` and `governance.supported: false`, each naming its `refusal_route` | Clients must be able to *discover* rows 6 and 7 before attempting them, rather than probing for a 501. The document is the edge's machine-readable half of this table. | CF: `entry/handlers.py:157-171` | `test_worker_route_defs.py::test_lifecycle_capabilities_publish_explicit_edge_refusals` |
| 9 | **`/api/health` payload** | `status`, `ok`, `ready`, `service`, `version`, `uptime_s`, `active_sessions`, `control_plane_backend`; 503 while starting | Worker entry: `{ok, service, environment}`. Durable Object: `{ok, service}`. Always 200 | A Worker isolate has no process start time (no `uptime_s`), no cross-session registry (no `active_sessions`), and no control-plane backend choice. There is also no "starting" state to report — the isolate either serves the request or does not exist. | CF: `entry/handlers.py:151-152`, `api/http_routes/_dispatch.py:92-93`<br>FastAPI: `server/routes/health.py:61-98` | `test_edge_divergence_matrix.py::test_edge_worker_health_payload_is_minimal`<br>`::test_edge_durable_object_health_payload_is_minimal`<br>(also `test_http_routes_coverage.py::test_health_endpoint`) |
| 10 | **Hibernation + resume-token recovery** | No equivalent. The hub process holds sessions in memory for its lifetime; `hello.resume_supported` reflects whether a `resume_store` was configured | Cloudflare may evict a Durable Object with WebSockets still open. On wake, `browser_sockets` / `worker_ws` / the hijack session are empty and are rebuilt from DO SQLite plus per-socket `serializeAttachment`. Resume is always on | This is a runtime property of Durable Objects, not a design choice — but the *recovery contract* is. Losing a hijack lease or a socket's role to an eviction the client never observed would be a silent authorization change, so both are made durable. | CF: `do/session_runtime/lifecycle.py:101,127,216`, `state/store.py:519`<br>UX contract: [`docs/operations/cf-hibernate-resume-ux.md`](operations/cf-hibernate-resume-ux.md)<br>FastAPI: `server/bridge/routes/websockets_impl.py:345` | `test_hibernate_wake_contract.py::test_hibernate_wake_restores_lease_and_broadcasts_via_get_websockets`<br>`::test_hibernate_wake_rebuilds_browser_owner_and_worker_generation`<br>`::test_socket_role_not_identity_after_hibernate` |
| 11 | **Tunnel transport (multiplexed WS)** | `WS /tunnel/{worker_id}` registers a *tunnel* worker (`is_tunnel_worker=True`) speaking the binary multiplexed channel framing | `/tunnel/{worker_id}` is routed to the Durable Object and classified as an **ordinary worker terminal socket**. No mux codec ships at the edge, so the tunnel-transport fragmentation scenario is `unserved` (`no_tunnel_websocket_route`) | The tunnel mux carries TCP-forward and HTTP-inspect channels alongside terminal bytes. Reproducing that demux inside a Durable Object has not been done; declaring it `unserved` is truthful, whereas accepting the upgrade and dropping non-terminal channels would silently lose data. | CF: `entry/registry.py:27`, `do/session_runtime/fetch.py:251,286`<br>FastAPI: `server/tunnel/fastapi_routes.py:54-57`<br>Contract: `spec/session_lifecycle_security_scenarios.json` (`fragmented_tunnel_websocket_message`) | `test_edge_divergence_matrix.py::test_edge_tunnel_path_upgrades_as_a_plain_worker_socket`<br>`::test_edge_tunnel_fragmentation_is_declared_unserved_in_the_shared_contract` |

Paths in the *Code evidence* column are relative to
`packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/` for CF rows and
`packages/provide-uterm-server/src/provide/uterm/` for FastAPI rows, unless a
full path is given. Test ids are relative to
`packages/provide-uterm-cloudflare/tests/`.

## How to change this

**Any change to a shared protocol surface that touches edge-runtime behavior
must update this table *and* the corresponding edge test, in the same commit.**

Concretely:

1. **Adding a divergence** — add a row here with all six columns filled in, then
   add an executable test to
   `packages/provide-uterm-cloudflare/tests/test_edge_divergence_matrix.py`
   asserting the Cloudflare side of it. A row with an empty *Regression test*
   cell is not an accepted divergence; it is an undocumented bug.
2. **Removing a divergence** (the edge reached parity) — delete the row and the
   test in the same commit. If the divergence is still claimed elsewhere
   (`docs/protocol-matrix.md`, `spec/*.json`), fix that too, or keep a
   parity-pinning row like row 3 so the stale claim cannot be "resolved"
   by regressing the code.
3. **Changing a divergence** — the *Why* column is the load-bearing one. If the
   reason no longer holds, the divergence is a bug, not a design choice.

Rows must state a **reason**, not a status. "Not implemented yet" is a backlog
item; it belongs in
[`docs/roadmap/uterm-risk-ranked-action-plan.md`](roadmap/uterm-risk-ranked-action-plan.md),
not here.

Tests for these rows must stay deterministic and offline: no `REAL_CF=1`, no
live Worker, no `pywrangler`. The Durable Object runtime is drivable directly
with in-memory SQLite plus WebSocket stubs — see the helpers at the top of
`test_edge_divergence_matrix.py`.
