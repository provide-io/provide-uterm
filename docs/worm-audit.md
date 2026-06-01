# WORM Audit Chain — Operator Guide

The server can emit a **tamper-evident, append-only audit log** (a WORM —
Write-Once-Read-Many — chain) alongside the always-present structured audit log
stream. This guide explains what the chain guarantees, how to enable it, how to
verify it, and — most importantly — the operational requirement that makes it
genuinely tamper-*proof* rather than merely tamper-*evident*.

## What the chain guarantees

Every audited action (`audit_event(...)`) appends one JSONL record to the chain
file. Each record carries:

- a **monotonic sequence number** (`seq`, strictly +1 per record),
- a wall-clock and a monotonic timestamp,
- the action plus principal / session / source-IP / detail, and
- a **`prev_hash`** linking it to the previous record's `record_hash`
  (sha256 over the record's canonical payload).

Because every record's hash covers its predecessor's hash, the records form an
unbroken cryptographic chain. **Any insertion, deletion, reordering, or content
modification breaks the chain** and is detected by the verifier:

```bash
uterm audit verify /var/log/uterm/audit.log
```

A clean chain reports `ok=True` with the head `seq`/hash; a broken one reports
`ok=False`, the `first_bad_seq`, and a human reason (`broken hash link`,
`non-contiguous sequence`, `record hash mismatch — content altered`,
`head mismatch — log truncated or rolled back`, ...).

This is the core property: **tamper-evidence**. You cannot prevent a writer with
file access from rewriting the log, but you *can* always detect that they did.

## The file sink

The chain writes to a single append-only JSONL file:

- created `0600` if absent, and **tightened to `0600`** on every append even if
  it pre-existed with looser permissions (an audit log must never be
  world-readable),
- opened `O_APPEND` so each record lands atomically at end-of-file, and
- **`fsync`-durable** — each record reaches disk before the audited action is
  acknowledged. The append path is fully synchronous; it never awaits, so it can
  never deadlock the request path. An append failure (disk full, permissions) is
  swallowed and logged (`audit_chain_append_failed`) — auditing never crashes the
  action it records.

## The control-plane head — cross-restart anti-rollback

The verifier alone cannot detect **end-truncation** (an attacker who deletes the
last N records leaves a *shorter but still internally-valid* chain). To catch
that across restarts, the server persists the chain **head** (`seq`, hash) into
the durable control plane:

- A periodic background task checkpoints `chain.seq` / `chain.last_hash` into the
  control plane (monotonic — the head can never move backwards).
- On clean shutdown the final head is flushed.
- **On startup** the server verifies the on-disk log against the persisted head.
  If the file's head is *behind* the persisted head — i.e. records were truncated
  or rolled back below the last checkpoint — verification fails with a
  `head mismatch` reason.

When startup detects a mismatch (or an internally-broken file) it emits:

- a **`CRITICAL` log** line: `audit_chain_integrity_alarm reason=... first_bad_seq=...`, and
- an **`audit.chain_integrity_alarm` audit event** (so your SIEM / monitoring fires).

The server **boots anyway** — refusing to boot would let an attacker DoS the
service simply by corrupting the log — and then **resumes the chain from the
file's actual head** so the forward chain stays valid going forward.

> **Operators MUST alert on `audit.chain_integrity_alarm` / the CRITICAL log.**
> It means the on-disk history no longer matches what the server last wrote:
> tamper, rollback, or end-truncation. Treat it as a security incident.

A brand-new deployment (no persisted head **and** no file yet) is the legitimate
genesis case and does **not** raise a false alarm.

## Enabling it

```toml
[audit]
chain_enabled = true
chain_file = "/var/log/uterm/audit.log"
```

`chain_enabled = true` without `chain_file` is rejected at config-load time
(a chain with nowhere to write is a misconfiguration, not a silent no-op). The
chain is **opt-in and default-disabled**; when disabled, the security-posture
self-report surfaces a compliance warning
(`audit log is not tamper-evident (audit.chain_enabled=false)`).

## The immutable-sink requirement (key long-term guidance)

**Tamper-*evident* is not tamper-*proof*.** A local-disk attacker with write
access to `chain_file` can rewrite the entire history (re-chaining every record
from a forged genesis); the chain only guarantees that doing so is *detectable*
by anyone holding an independent copy of the head — it does not *prevent* it.

For a genuine WORM guarantee, **ship the chain file to an immutable / append-only
sink** so history physically cannot be rewritten in place:

- **S3 with Object Lock** (compliance mode) or another write-once object store,
- an **append-only syslog / SIEM** pipeline that ingests each line, or
- a **write-once volume** (WORM-backed mount).

Put `chain_file` on (or continuously forward it to) such a sink. The local file
is the fast fsync-durable sink; the immutable copy is what makes the audit trail
unforgeable. The control-plane head + startup verification then detect any
divergence between the two.

## Anchoring & multi-instance (deferred)

The chain supports periodic **`anchor()` checkpoint records** — a record that
snapshots the head so an external notary can countersign it. Full
cross-instance / external-notary anchoring (countersigning anchor records across
an HA fleet) is **deferred to the HA / multi-instance design**; today's wiring
provides the single-instance chain, the immutable-sink path, and the
control-plane anti-rollback head. The `anchor()` records exist as checkpoints
ready for that future countersigning.
