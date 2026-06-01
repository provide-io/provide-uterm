#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Schema v0002: durable audit-chain head.

A single-row table (keyed ``id = 1``) that records the latest audit-chain head
``(seq, record_hash)``.  Persisting the head lets a restart resume the chain and
detect end-truncation / rollback (a pure in-file chain can be silently truncated
from the end; the persisted head is the out-of-band high-water mark).
"""

from __future__ import annotations

SQL = """
CREATE TABLE IF NOT EXISTS cp_audit_head (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    seq INTEGER NOT NULL,
    record_hash TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""
