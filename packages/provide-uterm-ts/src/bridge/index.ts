//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The worker side of the bridge: lease arbitration and the shared
 * authorization contract.
 *
 * Port of the Python package `provide.uterm.bridge`.
 */

export * from "./contracts.ts";
export * from "./coordinator.ts";
export * from "./hijackable.ts";
export * from "./policy.ts";
export * from "./worker-link.ts";
