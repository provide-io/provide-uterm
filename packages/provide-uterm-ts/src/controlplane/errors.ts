//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What the control plane refuses, and why.
 *
 * Port of the Python module `provide.uterm.control.plane.errors`.
 */

/** Base for every control-plane failure. */
export class ControlPlaneError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ControlPlaneError";
  }
}

// The reference also defines a configuration error and a capability error.
// Both are raised by the SQLite backend and the capability checks, neither of
// which is ported yet; defining them here with nothing to produce them would
// be a surface this port cannot honour.

/**
 * A commit lost a race with a concurrently committed transaction.
 *
 * Mirrors the serialisation failure SQLite produces under `BEGIN IMMEDIATE`:
 * two overlapping transactions that write the same key cannot both succeed.
 * The memory backend detects it optimistically at commit time, so a
 * lease-acquire race yields exactly one winner on either backend — otherwise
 * a deployment that develops against memory finds out the difference in
 * production.
 */
export class ControlPlaneConflictError extends ControlPlaneError {
  constructor(message: string) {
    super(message);
    this.name = "ControlPlaneConflictError";
  }
}
