//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The server side of the shared API contract.
 *
 * Framework-neutral: a method and a path go in, a handler or a refusal comes
 * out. Node's `http` and a Worker's `fetch` each supply the first two.
 */

export * from "./route-binding.ts";
