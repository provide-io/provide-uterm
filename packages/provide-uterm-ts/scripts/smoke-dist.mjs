//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Prove the emitted package root loads under native Node ESM.
 *
 * TypeScript can typecheck named imports from CommonJS declarations that the
 * Node loader cannot provide at runtime. Importing the root catches that class
 * of packaging failure across every namespace it eagerly exports.
 */

await import("../dist/index.js");
