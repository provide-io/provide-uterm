# Specification: Graphical Target Hardening

## Overview

A graphical target points the server at a remote console (`memory`, `rfb`,
`litevirt`). Today `graphical.Definition` describes *where* to connect —
protocol, endpoint, dimensions, secret refs — but carries no policy about
*whether* that connection is allowed, how far it may be trusted, or how much it
may allocate. Once a target is created, the server will dial whatever endpoint
it names and accept whatever the far side sends.

That leaves three gaps:

- **Egress.** A target endpoint is operator-supplied and reaches the network
  from inside the server. Nothing constrains it to an expected range, so a
  target is an SSRF primitive against anything the server can route to.
- **Resource bounds.** RFB and litevirt framebuffer geometry, rectangle counts
  and clipboard payloads arrive from the *remote* side. Nothing caps them, so a
  hostile or faulty console can drive allocation.
- **Transport trust.** `ca_secret_ref` / `client_cert_secret_ref` /
  `client_key_secret_ref` exist on the model, but there is no field saying
  whether TLS is required, and no expected server name to verify against, so the
  refs have no policy to apply.

An abandoned branch (`fix/architecture-security-remediation`, archived as
`archive/graphical-target-hardening`) modelled and validated all of this but
**never enforced any of it** — no dial path in `vnc/`, `transports/` or
`graphical/` ever read the fields. This spec recovers that design and states the
enforcement work the branch did not do.

## Requirements

### Transport trust

| Field | Values / default | Rule |
|---|---|---|
| `tls_mode` | `disabled` \| `tls` \| `mtls`, default `tls` | Rejects the combination it cannot honour |
| `expected_server_name` | optional identity | Verified against the presented certificate |

Validation rules from the archived branch, worth keeping:

- `tls_mode = disabled` must reject `ca_secret_ref` or `expected_server_name` —
  material that implies verification cannot sit on a target that does none.
- Client cert/key refs are only meaningful when `tls_mode = mtls`.
- `mtls` requires **both** the client cert and key, never one.

### Authorization and reachability

| Field | Default | Meaning |
|---|---|---|
| `minimum_role` | `viewer` (of `viewer` \| `operator` \| `admin`) | Authorization floor to attach to this target |
| `allowed_vm_patterns` | `["*"]`, must be non-empty | Which VM names the target may address (litevirt) |
| `allowed_cidrs` | `[]` | Endpoint must resolve inside one of these ranges |

`allowed_cidrs` is the anti-SSRF control and the most valuable item here. Note
the empty default means "unconstrained" — a deployment-visible decision worth
revisiting, since a safe default would be a deny-list posture instead.

### Resource bounds

Defaults recovered from the archived branch:

| Field | Default |
|---|---|
| `max_framebuffer_width` / `max_framebuffer_height` | `8192` |
| `max_rectangles` | `4096` |
| `max_clipboard_bytes` | `1 MiB` |
| `max_pixel_allocation_bytes` | `256 MiB` |
| `max_grpc_message_bytes` | `16 MiB` |

`max_pixel_allocation_bytes` is the one that actually bounds memory:
width × height × bytes-per-pixel is attacker-influenced, and the individual
dimension caps do not bound their product.

### Timeouts

`connect` / `handshake` (10s), `read` / `write` (30s), `shutdown` (5s). All must
be positive. Per-phase rather than a single deadline, so a stalled handshake is
distinguishable from a slow session.

### Audit

`audit_labels` — operator-supplied key/value pairs emitted with target-scoped
audit records, so console access can be attributed to a deployment's own
taxonomy.

## Scope

The archived branch stopped at the model. The work that remains is the half that
matters:

1. Add the fields to `graphical.Definition` (Go) and the canonical C#
   `GraphicalTargets.cs`, with the validation rules above.
2. Extend `cp_graphical_targets` (schema v0004) — mirrored verbatim between the
   Python and Go sources, per the existing byte-parity requirement in
   `packages/provide-uterm-go/controlplane/sqlite/schema.go`.
3. **Enforce at the dial path** — `vnc/`, `transports/`, and the litevirt client:
   - resolve the endpoint and check it against `allowed_cidrs` *before*
     connecting, re-checking after resolution to close the DNS-rebinding window;
   - apply `tls_mode` / `expected_server_name` when building the TLS config;
   - reject a framebuffer whose geometry or pixel allocation exceeds the caps,
     at the point the far side declares it;
   - apply the per-phase timeouts.
4. Gate attach on `minimum_role` in the graphical REST handlers alongside the
   existing capability + tenant-scope check.

## Non-goals

Persisting these fields is not the deliverable. The archived branch proved that a
schema which stores security intent without an enforcement point reads as
protection while providing none — the fields were populated, validated and
written to SQLite, and every connection ignored them. Any increment here should
land a field **and** its check together, or not at all.

## References

- `archive/graphical-target-hardening` — the archived branch (local-only tag;
  `serverconfig/graphical.go` holds the original field definitions and
  validation).
- `23d2f1ea` — canonical graphical-target registry + multi-tenancy.
- `f7338bed` — control-plane persistence for runtime targets.
