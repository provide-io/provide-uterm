<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Egress guard parity

A webhook destination is a URL the **server** fetches, supplied by whoever can
mutate a session. That makes it attacker-influenced input driving a request from
a more privileged position than the caller holds — the classic SSRF shape. This
document is the contract all four ports implement, so the guard cannot be strong
in one language and decorative in another.

Normative for: Python (`provide-uterm-server`), Go (`provide-uterm-go`), C#
(`provide-uterm-csharp`), TypeScript (`provide-uterm-ts`).

The reference implementation is Python
(`packages/provide-uterm-server/src/provide/uterm/server/webhooks.py`,
`.../egress.py`, `.../_net.py`). Where this document and the reference disagree,
the reference is wrong and should be fixed to match — do not silently diverge.

## 1. What is always refused

Refused regardless of any configuration key. There is deliberately **no** knob
that re-opens any of these for webhook delivery:

- cloud metadata addresses: `169.254.169.254`, `100.100.100.200`, `fd00:ec2::254`
- the hostname `metadata.google.internal`
- private ranges, link-local, multicast, unspecified, and IANA-reserved space

The canonical CIDR union is `blockedPrivateV4` / `blockedPrivateV6` in
`packages/provide-uterm-go/server/server_egress.go`. Go is the reference **for
the CIDR lists specifically**, because Python obtains this set from
`ipaddress.is_private` / `is_reserved` / `is_link_local` / `is_multicast` /
`is_unspecified` and the other three languages have no stdlib equivalent to lean
on. Those lists were derived to be the exact union of CPython's classifiers; port
them literally rather than re-deriving them, and cite them when you do.

### `100.64.0.0/10` is blocked, and CPython does not do it for you

RFC 6598 carrier-grade NAT space is **in** the refusal set. Current CPython does
*not* classify it as private —

```
>>> ipaddress.ip_address("100.64.0.1").is_private
False
```

— so deriving the set from `is_private`, as Python does and as the Go lists were
built to mirror, silently omits it. That is a gap in the derivation, not a
deliberate allowance: CGNAT space carries real infrastructure on carrier and
container networks, and it is exactly the sort of address an SSRF pivot wants.

Every port blocks it explicitly. A port that inherits the set from a stdlib
classifier must add this range on top rather than trusting the classifier.

### The rule this case generalises to

**Wherever the policy is deliberately stricter than the classifier it borrows,
the shared corpus must carry a row for it.**

That row is what makes borrowing safe. A port that leans on a stdlib
classifier — CPython's `ipaddress`, .NET's `IPAddress`, Go's `net` — and forgets
the addition fails that row immediately. Without it, four hand-written checks
quietly become three and nothing notices.

Two things went wrong here that the rule would have caught:

- `packages/provide-uterm-go/server/testdata/egress_golden.json` recorded
  `100.64.0.1` as `blocked_private: false`. The corpus had encoded the hole as
  expected behaviour, and the Go suite faithfully enforced it.
- C# blocked the range on the connector path and not the webhook path, so one
  port permitted a destination the other three refused — and its guard had a
  correct-sounding comment explaining why it belonged there, written when the
  premise was still true.

Corpora are only a guard where they are regenerated and diffed. `.ci/check_goldens.sh`
does that for every `gen_*_golden.py` in the tree. A corpus with no generator
cannot be re-derived and therefore cannot be checked; that script counts them so
the number is visible rather than discovered.

### Embedded IPv4

An IPv6 address can carry an IPv4 one, so `64:ff9b::169.254.169.254` reaches the
v4 metadata service on a NAT64 cluster. Every port must decode the mapped
(`::ffff:0:0/96`), 6to4 (`2002::/16`), NAT64 (`64:ff9b::/96`) and deprecated
IPv4-compatible forms before classifying, per
`decodeEmbeddedIPv4` (`server_egress.go`).

Python is the exception and does **not** decode explicitly: CPython's classifiers
already reject every such form carrying a private or metadata IPv4, pinned by the
`test_embedded_ipv4_*` regression tests. That is an implementation note, not a
licence for other ports to skip the decode — they have no such backstop.

## 2. Loopback: the one conditional case

Loopback (`127.0.0.0/8`, `::1`, the hostname `localhost` and any `*.localhost`)
is refused **unless** the effective permission below allows it.

Loopback is singled out because binding to `127.0.0.1` is itself an access
control: services listen there precisely so the network cannot reach them, and
skip authentication on that basis. Loopback SSRF converts "unreachable" into
"reachable". A service on a private range at least chose a routable interface.

## 3. The effective permission

```
effective_allow_loopback =
    config.webhooks.allow_loopback_destinations
    OR is_loopback_host(config.server.host)
```

Computed once where the server is built from config, not at each call site.

The bind term exists because the default bind is `127.0.0.1`
(`packages/provide-uterm/src/provide/uterm/defaults.py`). Without it, the default
configuration listens only on loopback **and refuses loopback webhook
destinations** — a guard that protects nothing (no remote caller can reach the
listener) at the cost of breaking every single-box deployment. Deriving
permissiveness from the bind address is the established idiom in this codebase:
see `_is_loopback_host` in
`packages/provide-uterm-server/src/provide/uterm/server/app/auth.py`, used to
gate `require_jwt_in_production`, `auth.mode="header"`, and
`security.mode="dev"`.

`webhooks.allow_loopback_destinations` keeps its schema — `bool`, default
`false` — and now reads as "*also* allow loopback on a routable bind". **Do not
change the schema.** It is drift-checked across all four ports via
`packages/provide-uterm-ts/testdata/configschema_golden.json`; changing its kind
churns every port for no gain.

Library-level constructors default to refusing loopback (`false`), matching the
reference's `allow_loopback_destinations: bool = False`. An embedder who has not
considered egress gets the closed posture.

## 4. Delivery-time tunnel check

At **delivery** time, a loopback destination is refused for any session that
currently holds an active tunnel share — even when §3 permits loopback.

Tunnel sharing exposes a loopback-bound server through a relay, so "bound to
loopback" stops implying "only local callers exist". Shares are issued at
runtime, which is why this cannot be folded into the load-time default: at config
load, the fact is not yet true or false.

The route is `POST /api/tunnels`, and the tunnel id **is** a session id, so
"does this session hold a share" is a keyed lookup on the token store. (An
earlier draft of this document said `POST /api/sessions/{id}/tunnels`; no such
route exists in any port.) `DELETE .../tokens` removes the share and
`POST .../tokens/rotate` replaces it with a fresh expiry, so the token record
*is* the share.

An expired share must not keep the guard closed: read the expiry off the record
rather than assuming a sweep has run, since a record outlives its expiry until
the next sweep. `expires_at == now` is **not** live.

Emit `webhook_delivery_blocked_tunnel_total` on this refusal — a dedicated
counter, **not** the generic `webhook_delivery_blocked_total`. The generic one
feeds the 3-strike auto-unregister, and a tunnel share can be revoked at any
time; counting a tunnel refusal there would let a temporary share permanently
kill an otherwise valid webhook.

The registration path must still **accept** a loopback destination while a share
is live. That is what makes this a delivery-time rule rather than a
registration-time one, and it is worth an explicit test.

### Order of evaluation

Destination safety is checked **first**, the share **second**. The two orders
differ only when the configuration refuses loopback *and* a share is live *and*
the destination is loopback-only. Such a destination can never deliver under the
current configuration, so it belongs on the generic counter that eventually
retires the webhook; the share guard exists for destinations that would
otherwise be fine. Ports must not reverse this — an implementation that reports
that case as a share refusal keeps a permanently-dead webhook alive forever.

## 5. DNS

A destination naming a DNS host is resolved, and **every** returned address is
checked. Resolution failure or an empty answer is a refusal, not a pass. This is
what stops DNS-rebinding SSRF, where a name resolves to metadata or private
space. Resolution runs under a hard timeout so a hostile resolver cannot hang
delivery.

## 6. Required test cases

Every port proves all of these, behaviourally, driven through the path an
operator actually uses (config → the production factory → registration or
delivery) rather than by calling the validator directly:

| case | expected |
|---|---|
| loopback bind, key unset, loopback destination | accepted |
| routable bind (`0.0.0.0`), key unset, loopback destination | refused |
| routable bind, key `true`, loopback destination | accepted |
| any bind, `169.254.169.254` | refused, **with and without** the key |
| any bind, `100.100.100.200` and `fd00:ec2::254` | refused |
| any bind, a `10.x` / `192.168.x` / `172.16.x` address | refused with and without the key |
| any bind, `metadata.google.internal` | refused |
| any bind, `64:ff9b::169.254.169.254` | refused (embedded-IPv4 decode) |
| any bind, hostname resolving to a private address | refused |
| any bind, hostname that fails to resolve | refused |
| loopback bind, session tunnel-shared, loopback destination | refused at delivery, counter incremented |
| loopback bind, share expired or never created | delivery proceeds |
| a public destination | accepted (the guard is not stuck closed) |

The last row matters as much as the others: a guard that refuses everything
passes every negative test.
