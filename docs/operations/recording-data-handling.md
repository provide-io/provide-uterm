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
| `screen_interval_s`          | `0` (off)            | Also record the screen of a session nobody is reading (a PAM capture) once its output settles, at most every N s; 0, or >= 0.25. Each is a whole rendered screen (`screen`, `raw`, `raw_bytes_b64`, about 18 KB at 80x24 in truecolor) and counts toward `max_bytes` |

### Capture sessions across a restart (`[session_persistence]`)

PAM capture sessions (`pam.mode = "capture"`) can be kept on disk too. A
table of its own rather than `[sessions]`, which is the list of static session
definitions:

| Knob                         | Default              | Notes                          |
| ---------------------------- | -------------------- | ------------------------------ |
| `state_dir`                  | none                 | Where the files go. Required when either feature below is on, and absolute; created `0700` at startup if missing (its parent must exist), and the server does not start if it is not a `0700` directory its user owns. Name one per server: two servers sharing it would adopt each other's sessions. Put it inside the unit's writable state, for example `/var/lib/uterm/capture-sessions` |
| `persist_capture`            | `false`              | Keep each PAM capture session (an id `pam-<user>-capture-<pid>-<start ticks>`) in `<state_dir>/live/<id>.json`: its definition, written on create, update and mode change, and its last rendered screen, written once output settles (at most once a second, only when it changed, with no recording or past the quota too) and once more as it stops. At startup each is re-created under its id, owner, visibility and recording instance while its socket passes PAM's confinement and its login's pid still reports the start ticks its id names; any other ends |
| `previous_keep_days`         | `0` (off)            | 0 to 3650. Keep a session that ends (PAM close, `DELETE`, or found gone at startup) as a read-only previous session in `.../previous/<id>.json`, read through `GET /api/previous-sessions[/{id}]` by its owner, an admin, or an operator unless it was private (never a share viewer, nor anyone for a public session alone); one that ended longer ago than its days is not kept; pruned past its days at startup and hourly, and past 20 per owner (by the PAM user in the id when it has no owner); an admin can reassign its owner or visibility with `PATCH /api/previous-sessions/{id}`, and the new owner's 20 then apply (see below) |
| `previous_scrollback_lines`  | `1000`               | 0 to 10000. The rows that scrolled off the top of the screen a previous session keeps, as plain text |

These files hold the screen **unredacted**: redaction is for text a reader is
shown, and a redacted screen painted back is a different screen. A previous
session's screen and scrollback are redacted when they are read, by the policy
the live screen is redacted by, and withheld (`503`) when that policy cannot
answer. Who can read a previous session is not fixed when it ends: an admin
can change its `owner` and `visibility` with `PATCH
/api/previous-sessions/{id}` (never a share viewer, nor an admin scoped to one
session), which widens or narrows the readers by the rule above and rewrites
the file with the same permissions. A record moved to an owner who already has
20 newer ones is deleted by the move, and the request answers `404`. Each
successful change is written to the audit log as `previous_session.update`,
with the new values as its detail. The files are as private as the terminal
state the server holds in memory: `0600` files in `0700` directories, written
to a synced temporary file renamed into place. One is read back only if it is
a regular file this user owns, no larger than 16 MiB, of a format this build
reads, and of the shape and JSON types the server writes, a definition key
another build of the same format added or dropped aside
(`docs/protocol-matrix.md`, "Capture sessions across a restart"); any other
file, or a directory that is not `0700` and this user's, is skipped with a
warning.

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
