#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Schema v0003: durable graphical-target definitions.

Graphical targets describe a remote console (memory / rfb / litevirt).  Until
now they lived only in an in-process registry, so every restart lost the
tenant's runtime targets and left only the config-seeded static ones.  This
table makes runtime targets durable.

Only ``runtime`` targets are persisted.  Static targets are re-seeded from the
config file on every boot and are immutable at the API boundary, so persisting
them would create a second source of truth that could drift from the config.

``config`` holds the protocol-specific parameter object (e.g. the litevirt
``vm_name``) as a JSON document rather than a column per protocol, so adding a
protocol does not require a migration.  It is NOT a secret and is returned
across the REST boundary.
"""

from __future__ import annotations

SQL = """
CREATE TABLE IF NOT EXISTS cp_graphical_targets (
    target_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    protocol TEXT NOT NULL,
    endpoint TEXT,
    secret TEXT,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    is_system INTEGER NOT NULL DEFAULT 0,
    is_static INTEGER NOT NULL DEFAULT 0,
    ca_secret_ref TEXT,
    client_cert_secret_ref TEXT,
    client_key_secret_ref TEXT,
    config TEXT NOT NULL DEFAULT '{}',
    created_by TEXT,
    created_at REAL NOT NULL,
    updated_by TEXT,
    updated_at REAL
);

CREATE INDEX IF NOT EXISTS ix_cp_graphical_targets_tenant
    ON cp_graphical_targets(tenant_id);
"""
