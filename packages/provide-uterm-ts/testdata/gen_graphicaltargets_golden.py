#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for graphical targets.

A graphical target says where a remote console is and how to reach it, so
three things decide whether one tenant can see another's screen:

* **What a definition may say.** An identifier is one safe name, a protocol is
  one this system speaks, a size is a size, and a secret reference names an
  environment variable or a file — never something arbitrary that a loader
  might follow.
* **Where the endpoint actually points.** Two grammars, one per protocol, both
  accepting a `dns:///` prefix and both insisting on a real port. An endpoint
  read wrongly is a console somewhere nobody asked for.
* **Who may see it.** A scope is derived from the authenticated principal and
  never from client input: exactly one of the system scope or a single
  tenant, and a tenant scope permits only its own.
* **What the registry does with all three.** Every read and write is gated by
  a scope, seeded targets are immutable, and an identifier is claimed once.

And what leaves the server: a definition crossing the REST boundary has every
secret stripped, so a target's password is never in a listing.

This is the one subsystem where C# is canonical — the Python rules say so —
so what is recorded here is Python's reading of them.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_graphicaltargets_golden.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from provide.uterm.server import graphical_targets as gt

OUT = Path(__file__).resolve().parent / "graphicaltargets_golden.json"

# A fixed instant, so the corpus is the same on every run.
WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

RFB_ENDPOINTS: list[tuple[str, str | None]] = [
    ("a host and port", "vm.example:5900"),
    ("a host and port with a scheme", "rfb://vm.example:5900"),
    ("a scheme in capitals", "RFB://vm.example:5900"),
    ("a dns prefix", "dns:///vm.example:5900"),
    ("a dns prefix and a scheme", "dns:///rfb://vm.example:5900"),
    ("a dns prefix in capitals", "DNS:///vm.example:5900"),
    ("spaces around it", "  vm.example:5900  "),
    ("a tab and a newline around it", "\t\nvm.example:5900\n\t"),
    ("an address", "192.0.2.10:5900"),
    ("an address in brackets", "[2001:db8::1]:5900"),
    ("an address with a zone", "[FE80::1%ETH0]:5900"),
    ("a zone and nothing before it", "%ETH0:5900"),
    ("brackets with nothing in them", "[]:5900"),
    ("credentials and no user", "@vm.example:5900"),
    ("a bracket nobody closed", "[2001:db8::1:5900"),
    ("a tab in the middle of the host", "vm.exa\tmple:5900"),
    ("the lowest port", "vm.example:1"),
    ("the highest port", "vm.example:65535"),
    ("port zero", "vm.example:0"),
    ("a port past the end", "vm.example:65536"),
    ("a negative port", "vm.example:-1"),
    ("a port that is not a number", "vm.example:abc"),
    ("no port at all", "vm.example"),
    ("no host at all", ":5900"),
    ("nothing", ""),
    ("only spaces", "   "),
    ("nothing given", None),
    ("a path after it", "vm.example:5900/path"),
    ("a query after it", "vm.example:5900?x=1"),
    ("a fragment after it", "vm.example:5900#x"),
    ("credentials in front", "user:pw@vm.example:5900"),
    ("a host in capitals", "VM.Example:5900"),
    ("another colon after the port", "vm.example:5900:1"),
    ("an address in brackets with no port", "[2001:db8::1]"),
    ("a scheme and nothing else", "rfb://"),
    ("a space inside the port", "vm.example: 5900"),
    # Digits, but not the ones a port is written in: the reference asks for
    # ASCII, and a runtime whose `isdigit` is looser would read a port here.
    ("a port in another script", "vm.example:\u0665\u0669\u0660\u0660"),
    # Brackets, which CPython's `urlsplit` reads with rules of its own: they
    # come in order, nothing may sit either side of them, and what is between
    # them is an address rather than a name. A runtime that only matched the
    # brackets up would take `[vmhost]` for a host and connect somewhere the
    # reference refuses to.
    ("a name in brackets", "[vmhost]:5900"),
    ("brackets the wrong way round", "]a[:1"),
    ("something before the bracket", "a[::1]:5900"),
    ("something after the bracket", "[::1]x:5900"),
    ("four octets in brackets", "[192.0.2.10]:5900"),
    ("an IPvFuture address", "[v7.example]:5900"),
    ("an IPvFuture address with no version", "[v.example]:5900"),
    ("brackets in the credentials", "[::1]@vm.example:5900"),
    ("brackets in the credentials and no port", "[::1]@vm.example"),
    ("a closing bracket in the credentials", "]@[::1"),
    ("a zone with nothing in it", "[fe80::1%]:5900"),
    ("a second zone", "[fe80::1%a%b]:5900"),
    ("an address ending in four octets", "[::ffff:192.0.2.10]:5900"),
    ("an octet past the end", "[::ffff:192.0.2.256]:5900"),
    ("an octet with a leading zero", "[::ffff:192.0.2.010]:5900"),
    ("three octets where there are four", "[::ffff:1.2.3]:5900"),
    ("a name where the octets go", "[::ffff:host.example]:5900"),
    ("eight groups written out", "[1:2:3:4:5:6:7:8]:5900"),
    ("nine groups", "[1:2:3:4:5:6:7:8:9]:5900"),
    ("eleven groups", "[1:2:3:4:5:6:7:8:9:a:b]:5900"),
    ("eight groups and a run of zeroes", "[1:2:3:4:5:6:7:8::]:5900"),
    ("a run of zeroes standing in for nothing", "[1:2:3:4::5:6:7:8]:5900"),
    ("two runs of zeroes", "[1::2::3]:5900"),
    ("a leading colon before a run", "[:1::2]:5900"),
    ("a trailing colon after a run", "[1::2:]:5900"),
    ("a leading colon and no run at all", "[:1:2:3:4:5:6:7]:5900"),
    ("a trailing colon and no run at all", "[1:2:3:4:5:6:7:]:5900"),
    ("a run of zeroes at the end", "[1:2:3:4:5:6:7::]:5900"),
    ("three groups where there are eight", "[1:2:3]:5900"),
    ("two groups", "[a:b]:5900"),
    ("a group of five digits", "[12345::1]:5900"),
    ("a group that is not hex", "[::z]:5900"),
    ("nothing but a run of zeroes", "[::]:5900"),
    ("an address longer than an address goes", "[" + "2001:db8:" * 5 + "1]:5900"),
]

LITEVIRT_ENDPOINTS: list[tuple[str, str | None]] = [
    ("a host and port", "vm.example:9000"),
    ("a dns prefix", "dns:///vm.example:9000"),
    ("a dns prefix in capitals", "DNS:///vm.example:9000"),
    ("spaces around it", "  vm.example:9000  "),
    ("an address in brackets", "[2001:db8::1]:9000"),
    ("a bracket nobody closed", "[2001:db8::1:9000"),
    ("a scheme it does not take", "rfb://vm.example:9000"),
    ("no port at all", "vm.example"),
    ("port zero", "vm.example:0"),
    ("a port past the end", "vm.example:65536"),
    ("nothing", ""),
    ("only spaces", "   "),
    ("nothing given", None),
    ("a host in capitals", "VM.Example:9000"),
    ("credentials in front", "user:pw@vm.example:9000"),
    ("a path after it", "vm.example:9000/x"),
    ("an address in brackets with no port", "[2001:db8::1]"),
    ("a negative port", "vm.example:-1"),
    ("a port that is not a number", "vm.example:abc"),
    # The same bracket reading, reported under this protocol's own message.
    ("a name in brackets", "[vmhost]:9000"),
    ("brackets the wrong way round", "]a[:9000"),
    ("an IPvFuture address", "[v7.example]:9000"),
]

DEFINITIONS: list[tuple[str, dict[str, Any]]] = [
    ("an ordinary target", {"target_id": "vm1", "endpoint": "vm.example:5900"}),
    ("a target with a tenant", {"target_id": "vm1", "tenant_id": "acme", "endpoint": "vm.example:5900"}),
    ("an identifier with a dot", {"target_id": "vm.1", "endpoint": "vm.example:5900"}),
    ("an identifier starting with a dash", {"target_id": "-vm", "endpoint": "vm.example:5900"}),
    ("an identifier starting with a dot", {"target_id": ".vm", "endpoint": "vm.example:5900"}),
    ("an identifier with a slash", {"target_id": "a/b", "endpoint": "vm.example:5900"}),
    ("an identifier with a space", {"target_id": "a b", "endpoint": "vm.example:5900"}),
    ("an empty identifier", {"target_id": "", "endpoint": "vm.example:5900"}),
    ("an identifier of one character", {"target_id": "a", "endpoint": "vm.example:5900"}),
    ("an identifier of the longest length", {"target_id": "a" * 128, "endpoint": "vm.example:5900"}),
    ("an identifier one too long", {"target_id": "a" * 129, "endpoint": "vm.example:5900"}),
    # A trailing newline: the reference anchors with `$`, which in Python
    # matches before one. Recorded rather than tightened, so the port accepts
    # exactly what the reference accepts.
    ("an identifier with a newline after it", {"target_id": "vm1\n", "endpoint": "vm.example:5900"}),
    ("an identifier with a newline inside it", {"target_id": "vm\n1", "endpoint": "vm.example:5900"}),
    ("a protocol in capitals", {"target_id": "vm1", "protocol": "RFB", "endpoint": "vm.example:5900"}),
    ("a protocol with spaces", {"target_id": "vm1", "protocol": "  rfb  ", "endpoint": "vm.example:5900"}),
    ("a protocol nobody speaks", {"target_id": "vm1", "protocol": "vnc2", "endpoint": "vm.example:5900"}),
    ("the memory protocol", {"target_id": "vm1", "protocol": "memory"}),
    ("litevirt", {"target_id": "vm1", "protocol": "litevirt", "endpoint": "vm.example:9000"}),
    ("a size of one", {"target_id": "vm1", "endpoint": "vm.example:5900", "width": 1, "height": 1}),
    ("the largest size", {"target_id": "vm1", "endpoint": "vm.example:5900", "width": 8192, "height": 8192}),
    ("no width at all", {"target_id": "vm1", "endpoint": "vm.example:5900", "width": 0}),
    ("a width past the end", {"target_id": "vm1", "endpoint": "vm.example:5900", "width": 8193}),
    ("a negative height", {"target_id": "vm1", "endpoint": "vm.example:5900", "height": -1}),
    ("no height at all", {"target_id": "vm1", "endpoint": "vm.example:5900", "height": 0}),
    ("a negative width", {"target_id": "vm1", "endpoint": "vm.example:5900", "width": -1}),
    ("a tenant with a slash", {"target_id": "vm1", "tenant_id": "a/b", "endpoint": "vm.example:5900"}),
    ("a tenant of only spaces", {"target_id": "vm1", "tenant_id": "   ", "endpoint": "vm.example:5900"}),
    ("a tenant with a newline after it", {"target_id": "vm1", "tenant_id": "acme\n", "endpoint": "vm.example:5900"}),
    (
        "a secret from the environment",
        {"target_id": "vm1", "endpoint": "vm.example:5900", "ca_secret_ref": "env:CA_BUNDLE"},
    ),
    ("a secret from a file", {"target_id": "vm1", "endpoint": "vm.example:5900", "ca_secret_ref": "file:/etc/ca.pem"}),
    (
        "a secret from a relative file",
        {"target_id": "vm1", "endpoint": "vm.example:5900", "ca_secret_ref": "file:etc/ca.pem"},
    ),
    (
        "a secret from somewhere else",
        {"target_id": "vm1", "endpoint": "vm.example:5900", "ca_secret_ref": "http://x/ca"},
    ),
    ("a secret named oddly", {"target_id": "vm1", "endpoint": "vm.example:5900", "ca_secret_ref": "env:1BAD"}),
    ("a client key reference", {"target_id": "vm1", "endpoint": "vm.example:5900", "client_key_secret_ref": "bad"}),
    ("a certificate reference", {"target_id": "vm1", "endpoint": "vm.example:5900", "client_cert_secret_ref": "bad"}),
    (
        "a secret reference with a newline after it",
        {"target_id": "vm1", "endpoint": "vm.example:5900", "ca_secret_ref": "env:CA\n"},
    ),
    (
        "a secret reference with a newline inside it",
        {"target_id": "vm1", "endpoint": "vm.example:5900", "ca_secret_ref": "env:CA\nmore"},
    ),
    (
        "a secret reference with a null byte in it",
        {"target_id": "vm1", "endpoint": "vm.example:5900", "ca_secret_ref": "file:/etc/\x00ca.pem"},
    ),
    (
        "a secret reference that is only a file scheme",
        {"target_id": "vm1", "endpoint": "vm.example:5900", "ca_secret_ref": "file:/"},
    ),
    (
        "an endpoint written every long way",
        {"target_id": "vm1", "endpoint": "  dns:///RFB://VM.Example:5900/ignored  "},
    ),
    ("memory with an endpoint nobody could reach", {"target_id": "vm1", "protocol": "memory", "endpoint": "nonsense"}),
    ("memory with no endpoint at all", {"target_id": "vm1", "protocol": "memory", "endpoint": None}),
    ("litevirt with no endpoint at all", {"target_id": "vm1", "protocol": "litevirt", "endpoint": None}),
    ("rfb with no endpoint at all", {"target_id": "vm1", "endpoint": None}),
    (
        "everything filled in",
        {
            "target_id": "vm1",
            "tenant_id": "acme",
            "display_name": "The one in the corner",
            "protocol": "rfb",
            "endpoint": "vm.example:5900",
            "secret": "s3cret",  # pragma: allowlist secret
            "width": 1024,
            "height": 768,
            "is_system": True,
            "is_static": True,
            "ca_secret_ref": "env:CA",
            "client_cert_secret_ref": "file:/etc/cert.pem",
            "client_key_secret_ref": "env:KEY",
            "created_by": "ada",
            "updated_by": "grace",
            "updated_at": "2026-02-03T04:05:06+00:00",
            "config": {"vm_name": "guest-1", "depth": 24},
        },
    ),
]

# A registry scenario is a list of steps, each an operation under a named
# scope. Recorded in order, because what an operation does depends on what
# the ones before it left behind.
SCENARIOS: list[tuple[str, list[dict[str, Any]]]] = [
    (
        "creating and reading back",
        [
            {"op": "create", "scope": "system", "fields": {"target_id": "vm1", "endpoint": "vm.example:5900"}},
            {"op": "get", "scope": "system", "target_id": "vm1"},
            {"op": "list", "scope": "system"},
        ],
    ),
    (
        "creating the same identifier twice",
        [
            {"op": "create", "scope": "system", "fields": {"target_id": "vm1", "endpoint": "vm.example:5900"}},
            {"op": "create", "scope": "system", "fields": {"target_id": "vm1", "endpoint": "other.example:5900"}},
        ],
    ),
    (
        "creating something that does not validate",
        [
            {"op": "create", "scope": "system", "fields": {"target_id": "vm1", "endpoint": "vm.example"}},
            {"op": "get", "scope": "system", "target_id": "vm1"},
        ],
    ),
    (
        "a tenant creating its own target",
        [
            {
                "op": "create",
                "scope": "acme",
                "fields": {"target_id": "vm1", "tenant_id": "acme", "endpoint": "vm.example:5900"},
            },
            {"op": "get", "scope": "acme", "target_id": "vm1"},
            {"op": "get", "scope": "other", "target_id": "vm1"},
            {"op": "list", "scope": "other"},
            {"op": "get", "scope": "system", "target_id": "vm1"},
        ],
    ),
    (
        "a tenant creating a target for somebody else",
        [
            {
                "op": "create",
                "scope": "acme",
                "fields": {"target_id": "vm1", "tenant_id": "other", "endpoint": "vm.example:5900"},
            },
        ],
    ),
    (
        "a tenant creating an unowned target",
        [
            {"op": "create", "scope": "acme", "fields": {"target_id": "vm1", "endpoint": "vm.example:5900"}},
        ],
    ),
    (
        "an untenanted target belongs to the system alone",
        [
            {"op": "create", "scope": "system", "fields": {"target_id": "vm1", "endpoint": "vm.example:5900"}},
            {"op": "get", "scope": "acme", "target_id": "vm1"},
            {"op": "list", "scope": "acme"},
        ],
    ),
    (
        "working under a scope that is neither",
        [
            {"op": "create", "scope": "broken", "fields": {"target_id": "vm1", "endpoint": "vm.example:5900"}},
            {"op": "get", "scope": "broken", "target_id": "vm1"},
            {"op": "list", "scope": "broken"},
            {"op": "delete", "scope": "broken", "target_id": "vm1"},
        ],
    ),
    (
        "updating what is there",
        [
            {
                "op": "create",
                "scope": "system",
                "fields": {"target_id": "vm1", "endpoint": "vm.example:5900", "display_name": "first"},
            },
            {
                "op": "update",
                "scope": "system",
                "fields": {"target_id": "vm1", "endpoint": "vm.example:5901", "display_name": "second"},
            },
            {"op": "get", "scope": "system", "target_id": "vm1"},
        ],
    ),
    (
        "updating what is not there",
        [
            {"op": "update", "scope": "system", "fields": {"target_id": "vm1", "endpoint": "vm.example:5900"}},
        ],
    ),
    (
        "a tenant updating a target into somebody else's name",
        [
            {
                "op": "create",
                "scope": "acme",
                "fields": {"target_id": "vm1", "tenant_id": "acme", "endpoint": "vm.example:5900"},
            },
            {
                "op": "update",
                "scope": "acme",
                "fields": {"target_id": "vm1", "tenant_id": "other", "endpoint": "vm.example:5901"},
            },
            {"op": "get", "scope": "acme", "target_id": "vm1"},
        ],
    ),
    (
        "one tenant updating another's target",
        [
            {
                "op": "create",
                "scope": "acme",
                "fields": {"target_id": "vm1", "tenant_id": "acme", "endpoint": "vm.example:5900"},
            },
            {
                "op": "update",
                "scope": "other",
                "fields": {"target_id": "vm1", "tenant_id": "other", "endpoint": "vm.example:5901"},
            },
            {"op": "get", "scope": "acme", "target_id": "vm1"},
        ],
    ),
    (
        "deleting what is there",
        [
            {"op": "create", "scope": "system", "fields": {"target_id": "vm1", "endpoint": "vm.example:5900"}},
            {"op": "delete", "scope": "system", "target_id": "vm1"},
            {"op": "get", "scope": "system", "target_id": "vm1"},
            {"op": "delete", "scope": "system", "target_id": "vm1"},
        ],
    ),
    (
        "one tenant deleting another's target",
        [
            {
                "op": "create",
                "scope": "acme",
                "fields": {"target_id": "vm1", "tenant_id": "acme", "endpoint": "vm.example:5900"},
            },
            {"op": "delete", "scope": "other", "target_id": "vm1"},
            {"op": "get", "scope": "acme", "target_id": "vm1"},
        ],
    ),
    (
        "a seeded target is immutable",
        [
            {"op": "add_static", "fields": {"target_id": "seed", "endpoint": "seed.example:5900"}},
            {"op": "get", "scope": "system", "target_id": "seed"},
            {"op": "update", "scope": "system", "fields": {"target_id": "seed", "endpoint": "other.example:5900"}},
            {"op": "delete", "scope": "system", "target_id": "seed"},
            {"op": "create", "scope": "system", "fields": {"target_id": "seed", "endpoint": "other.example:5900"}},
        ],
    ),
    (
        "seeding the same identifier twice",
        [
            {"op": "add_static", "fields": {"target_id": "seed", "endpoint": "seed.example:5900"}},
            {"op": "add_static", "fields": {"target_id": "seed", "endpoint": "other.example:5900"}},
        ],
    ),
    (
        "seeding something that does not validate",
        [
            {"op": "add_static", "fields": {"target_id": "seed", "endpoint": "seed.example"}},
            {"op": "get", "scope": "system", "target_id": "seed"},
        ],
    ),
    (
        "a seeded target a tenant may not see",
        [
            {"op": "add_static", "fields": {"target_id": "seed", "tenant_id": "acme", "endpoint": "s.example:5900"}},
            {"op": "get", "scope": "other", "target_id": "seed"},
            {"op": "list", "scope": "other"},
            {"op": "delete", "scope": "other", "target_id": "seed"},
            {"op": "get", "scope": "acme", "target_id": "seed"},
            {"op": "delete", "scope": "acme", "target_id": "seed"},
        ],
    ),
    (
        "a seeded target shadows a runtime one of the same name",
        [
            {
                "op": "create",
                "scope": "system",
                "fields": {"target_id": "vm1", "display_name": "runtime", "endpoint": "vm.example:5900"},
            },
            {
                "op": "add_static",
                "fields": {"target_id": "vm1", "display_name": "seeded", "endpoint": "seed.example:5900"},
            },
            {"op": "get", "scope": "system", "target_id": "vm1"},
            {"op": "list", "scope": "system"},
        ],
    ),
    (
        "listing comes back in order",
        [
            {"op": "create", "scope": "system", "fields": {"target_id": "vm2", "endpoint": "b.example:5900"}},
            {"op": "create", "scope": "system", "fields": {"target_id": "vm1", "endpoint": "a.example:5900"}},
            {"op": "add_static", "fields": {"target_id": "vm0", "endpoint": "c.example:5900"}},
            {"op": "list", "scope": "system"},
        ],
    ),
    (
        "a closed registry does nothing at all",
        [
            {"op": "create", "scope": "system", "fields": {"target_id": "vm1", "endpoint": "vm.example:5900"}},
            {"op": "close"},
            {"op": "get", "scope": "system", "target_id": "vm1"},
            {"op": "list", "scope": "system"},
            {"op": "create", "scope": "system", "fields": {"target_id": "vm2", "endpoint": "vm.example:5900"}},
            {"op": "update", "scope": "system", "fields": {"target_id": "vm1", "endpoint": "vm.example:5901"}},
            {"op": "delete", "scope": "system", "target_id": "vm1"},
            {"op": "add_static", "fields": {"target_id": "seed", "endpoint": "seed.example:5900"}},
        ],
    ),
    (
        "what an update keeps from what it replaces",
        [
            {
                "op": "create",
                "scope": "system",
                "fields": {"target_id": "vm1", "endpoint": "vm.example:5900", "created_by": "ada"},
            },
            {
                "op": "update",
                "scope": "system",
                "fields": {
                    "target_id": "vm1",
                    "endpoint": "vm.example:5901",
                    "created_by": "mallory",
                    "updated_by": "grace",
                },
            },
        ],
    ),
]

# The scopes a scenario step can name. `broken` is neither system nor tenant,
# which is what an unauthenticated caller would arrive with.
SCENARIO_SCOPES: dict[str, gt.GraphicalTargetScope] = {
    "system": gt.GraphicalTargetScope(tenant_id=None, is_system=True),
    "acme": gt.GraphicalTargetScope(tenant_id="acme", is_system=False),
    "other": gt.GraphicalTargetScope(tenant_id="other", is_system=False),
    "broken": gt.GraphicalTargetScope(tenant_id=None, is_system=False),
}

SCOPES: list[tuple[str, str | None, bool, str | None]] = [
    ("the system scope against a tenant's target", None, True, "acme"),
    ("the system scope against an unowned target", None, True, None),
    ("a tenant against its own target", "acme", False, "acme"),
    ("a tenant against another's target", "acme", False, "other"),
    ("a tenant against an unowned target", "acme", False, None),
    ("a scope that is both", "acme", True, "acme"),
    ("a scope that is neither", None, False, "acme"),
]


class _Clock:
    """A clock that ticks one second per reading, so times are recorded, not observed."""

    def __init__(self) -> None:
        self.ticks = 0

    def __call__(self) -> datetime:
        self.ticks += 1
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC) + timedelta(seconds=self.ticks)


def _run_scenario(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    registry = gt.InMemoryGraphicalTargetRegistry(now=_Clock())
    recorded: list[dict[str, Any]] = []
    for step in steps:
        scope = SCENARIO_SCOPES[step["scope"]] if "scope" in step else None
        fields = step.get("fields")
        target = None if fields is None else gt.GraphicalTargetDefinition(created_at=WHEN, **fields)
        target_id = step.get("target_id")
        # Each bound to this step's own scope and target, so what a lambda
        # runs against is what the step named rather than whatever the loop
        # reached by the time it was called.
        operations: dict[str, Any] = {
            "create": lambda c=scope, t=target: registry.create(c, t).to_wire_dict(),
            "update": lambda c=scope, t=target: registry.update(c, t).to_wire_dict(),
            "delete": lambda c=scope, i=target_id: registry.delete(c, target_id=i),
            "add_static": lambda t=target: registry.add_static(t),
            "close": registry.close,
            "get": lambda c=scope, i=target_id: None if (found := registry.get(c, i)) is None else found.to_wire_dict(),
            "list": lambda c=scope: [found.to_wire_dict() for found in registry.list(c)],
        }
        recorded.append({**step, **_error(operations[step["op"]])})
    return recorded


def _error(call: Any) -> dict[str, Any]:
    try:
        value = call()
    except gt.GraphicalTargetError as exc:
        return {"error": exc.code.name, "message": exc.message}
    except ValueError as exc:
        # Not a coded refusal: the reference guards only the port lookup, so
        # an address whose bracket is never closed escapes as a bare
        # ValueError — a 500 where the operator earned a 400. Recorded as the
        # defect it is; the port refuses with a code instead.
        return {"crash": type(exc).__name__, "message": str(exc)}
    return {"value": value}


def _definition(fields: dict[str, Any]) -> dict[str, Any]:
    # Times are written as text so the case list stays JSON, and read back
    # here so the reference gets the datetime it expects.
    fields = {k: datetime.fromisoformat(v) if k == "updated_at" else v for k, v in fields.items()}
    definition = gt.GraphicalTargetDefinition(created_at=WHEN, **fields)
    outcome = _error(definition.validate)
    if "error" in outcome:
        return outcome
    return {
        "protocol": definition.protocol,
        "endpoint": definition.endpoint,
        "wire": definition.to_wire_dict(),
    }


def main() -> None:
    corpus = {
        "protocols": sorted(gt.SUPPORTED_PROTOCOLS),
        "error_codes": [code.name for code in gt.GraphicalTargetErrorCode],
        "rfb": [
            {"name": name, "endpoint": value, **_error(lambda v=value: list(gt.parse_rfb_endpoint(v)))}
            for name, value in RFB_ENDPOINTS
        ],
        "litevirt": [
            {"name": name, "endpoint": value, **_error(lambda v=value: list(gt.parse_litevirt_endpoint(v)))}
            for name, value in LITEVIRT_ENDPOINTS
        ],
        "definitions": [{"name": name, "fields": fields, **_definition(fields)} for name, fields in DEFINITIONS],
        "scopes": [
            {
                "name": name,
                "scope_tenant": scope_tenant,
                "is_system": is_system,
                "target_tenant": target_tenant,
                "is_valid": gt.GraphicalTargetScope(tenant_id=scope_tenant, is_system=is_system).is_valid,
                "permits": gt.GraphicalTargetScope(tenant_id=scope_tenant, is_system=is_system).permits(target_tenant),
            }
            for name, scope_tenant, is_system, target_tenant in SCOPES
        ],
        "scope_for_tenant": [
            {
                "tenant": tenant,
                "scope": None if scope is None else {"tenant_id": scope.tenant_id, "is_system": scope.is_system},
                "ok": ok,
            }
            for tenant in ("acme", "", "   ", "a b")
            for scope, ok in [gt.scope_for_tenant(tenant)]
        ],
        "scenarios": [{"name": name, "steps": _run_scenario(steps)} for name, steps in SCENARIOS],
        "system_scope": {"tenant_id": gt.system_scope().tenant_id, "is_system": gt.system_scope().is_system},
        "public_copy": _error(
            lambda: (
                gt.GraphicalTargetDefinition(
                    target_id="vm1",
                    endpoint="vm.example:5900",
                    secret="s3cret",  # noqa: S106  # pragma: allowlist secret
                    ca_secret_ref="env:CA",  # noqa: S106
                    client_cert_secret_ref="env:CERT",  # noqa: S106
                    client_key_secret_ref="env:KEY",  # noqa: S106
                    created_at=WHEN,
                    config={"vm_name": "guest-1"},
                )
                .public_copy()
                .to_wire_dict()
            )
        ),
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['definitions'])} definitions)")


if __name__ == "__main__":
    main()
