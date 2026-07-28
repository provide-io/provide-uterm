//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * WebSocket wire-format frames.
 *
 * The types in `generated/frames.ts` are produced from the Pydantic models in
 * `provide.uterm.bridge.schemas` by `scripts/codegen_frames.py`, in the same
 * run that regenerates the browser frontend's copy. Hand-editing either is
 * caught by the codegen drift check.
 *
 * Port of the Python modules `provide.uterm.bridge.schemas` and
 * `provide.uterm.server.bridge.frames`, and the Go package `frames`.
 */

export * from "./builders.ts";
export type * from "./generated/frames.ts";
