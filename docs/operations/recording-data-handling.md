# Recording — Data Handling & Known Behavior

This document describes how `provide-uterm` persists session recordings, what the
defaults imply for sensitive data, and what operators are expected to configure
before enabling recording in any environment that handles real user traffic.

It is intentionally a documented *known behavior* rather than a list of bugs:
the design centralizes recording in a single store interface so operators can
swap in encryption / retention / redaction backends, but the in-tree defaults
prioritize debuggability over confidentiality.

## TL;DR

- Recording is **off by default** (`recording.enabled_by_default = false`).
- When enabled with the default `LocalFileRecordingStore`, sessions are written
  as **plaintext JSONL** to disk under `recording.directory`
  (default: `.uterm-recordings/`).
- **PTY input/output is recorded.** Default secret-redaction patterns are
  applied before persistence (`recording.redact_sensitive = true`), but this is
  best-effort and should not be treated as a complete DLP solution.
- Local recording file retention can be enforced with `recording.retention_s`
  (`0` means keep indefinitely).
- `recording.control_channel_mode = "exclude"` keeps internal control frames
  out of the recording, but it does **not** scrub the data stream.

## What is recorded

The store interface is `RecordingStore` in
`packages/provide-uterm/src/provide/uterm/recording.py` (ported to Go and C#
with the same lifecycle and query semantics — see
[recording-store-parity.md](./recording-store-parity.md) for diagrams and the
cross-language map). Each session is written as a sequence of JSONL events:

| Event type                | Source         | Contents                      |
| ------------------------- | -------------- | ----------------------------- |
| terminal output (`term`)  | worker → hub   | raw bytes from the PTY/SSH    |
| terminal input (`input`)  | viewer → hub   | raw keystrokes from the user  |
| analysis frames           | hub-derived    | annotation/anomaly markers    |
| metadata (`start`/`end`)  | hub            | session id, principal, times  |

Control-plane frames (hijack state, presence, snapshots) are **omitted by
default** because `control_channel_mode = "exclude"`. Setting it to `"wire"`
preserves them — useful for protocol debugging, never appropriate for storing
real-user sessions.

## Implied data sensitivity

Because PTY input is captured verbatim:

- A user typing `sudo` and a password records the password.
- A user pasting an API key, a `.netrc` entry, an SSH private key, or any
  other secret records the secret.
- A user piping `echo "$SECRET" | …` writes the secret into the output stream.
- A user mistyping a password at the wrong prompt records the partial password.

Treat the recording directory as containing the same trust class as the user's
own shell history — but multiplied across every operator and viewer who shared
the session.

## Operator responsibilities

When recording is enabled in any non-development environment, the operator is
expected to:

1. **Choose a non-default store.** `LocalFileRecordingStore` is intended for
   single-host development. `RecordingStore` is a protocol; a production
   deployment should provide an implementation that:
   - Writes to encrypted-at-rest storage (KMS/HSM-backed object store, encrypted
     volume, etc.).
   - Enforces retention according to organizational policy.
   - Applies access controls aligned with the principal model.
2. **Restrict filesystem access** if the local store is used. The directory
   should be `0700` and owned by the server process user only.
3. **Communicate the recording posture to users.** Operators must inform end
   users that sessions are being recorded — both as a legal/compliance baseline
   and so users self-redact (e.g., paste secrets via files instead of typing).
4. **Set retention.** `recording.retention_s` defaults to `0` (indefinite);
   pick a value compatible with the policies above.

## Configuration reference

The recording configuration lives under `recording.*` in
`packages/provide-uterm-server/src/provide/uterm/server/config_schema.py`:

| Knob                         | Default              | Notes                          |
| ---------------------------- | -------------------- | ------------------------------ |
| `enabled_by_default`         | `false`              | Per-session opt-in if false    |
| `directory`                  | `.uterm-recordings`  | Plain-text JSONL files written here when `store_type = "local"` |
| `max_bytes`                  | `0` (unlimited)      | Per-session size cap           |
| `retention_s`                | `0` (indefinite)     | Local `.jsonl` file TTL for sweep task |
| `control_channel_mode`       | `"exclude"`          | `"wire"` includes control frames |
| `redact_sensitive`           | `true`               | Apply default secret redaction patterns before persistence |
| `store_type`                 | `"local"`            | `local` / `memory` / `null` / `webhook` |
| `webhook_url`                | unset                | Required when `store_type = "webhook"` |

`session_retention_s` lives at the top level of `UtermServerConfig` and applies
to stopped session definitions in the in-memory registry; recording file TTL is
controlled separately by `recording.retention_s`.

## Future work

The `RecordingStore` protocol is the right hook point. Forward-looking work
that would change the defaults rather than the contract:

- An optional input redactor (regex denylist applied to `input` frames before
  persistence) configured under `recording.input_redact_patterns`.
- A retention floor when `enabled_by_default = true` (e.g., refuse to start
  the server with both flags set unless `session_retention_s > 0`).
- A first-party encrypted store implementation.

These are tracked as design follow-ups, not regressions: today's behavior
is intentional and operators are expected to bridge the gap with the steps
above.
