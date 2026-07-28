//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Embedding a terminal session inside another application.
 *
 * A host application drives the session directly rather than over a socket,
 * so what is here is the contract between the two: which attached clients a
 * broadcast reaches, what an interceptor may decide, and how the session
 * presents itself to a telnet server upstream.
 */

export * from "./telnet-parse.ts";
export * from "./types.ts";
