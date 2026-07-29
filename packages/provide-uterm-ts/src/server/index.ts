//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The server side of the shared API contract.
 *
 * Framework-neutral: a method and a path go in, a handler or a refusal comes
 * out. Node's `http` and a Worker's `fetch` each supply the first two.
 *
 * `node-http.ts` is deliberately absent from this barrel. It is the one file
 * here that imports `node:http`, and this barrel is on the package's default
 * entry — a Worker that imported `provide-uterm-ts` would pull the runtime in
 * behind it and fail to start. It is reached by path, the way `react` and
 * `conformance` are kept off the entry for the same reason.
 */

export * from "./app.ts";
export * from "./authorization.ts";
export * from "./bootstrap.ts";
export * from "./health.ts";
export * from "./hijack-routes.ts";
export * from "./route-binding.ts";
export * from "./session-hub.ts";
export * from "./session-registry.ts";
export * from "./session-runtime.ts";
export * from "./session-status.ts";
export * from "./worker-attach.ts";
