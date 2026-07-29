//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Relaying a graphical session.
 *
 * What is here is the boundary: which of a viewer's RFB messages reach the
 * session. Everything that only reads the screen passes through; the three
 * that act on it are gated.
 */

export * from "./human-relay.ts";
export * from "./rfb-filter.ts";
