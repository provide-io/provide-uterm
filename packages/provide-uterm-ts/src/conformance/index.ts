//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The live cross-language conformance driver.
 *
 * `conformance/live/PROTOCOL.md` is the contract: a driver performs a
 * scenario's steps and writes down what it saw, and the harness — one
 * implementation, shared by every language — decides whether that was right.
 * Nothing here evaluates an expectation.
 *
 * Kept off the package's default entry alongside `react`: it reads files and
 * writes to a process's standard output, which a browser and a Worker have no
 * use for. `provide-uterm-ts/conformance` reaches it by name.
 */

export * from "./cli.ts";
export * from "./client-driver.ts";
export * from "./flags.ts";
export * from "./serve.ts";
export * from "./transport.ts";
