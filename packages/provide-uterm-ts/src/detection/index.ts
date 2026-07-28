//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Prompt detection.
 *
 * Port of the Python package `provide.uterm.detection` and the Go package
 * `detection`. The screen buffer and the input-type heuristic land here; the
 * rule engine, extractor and flow controller are still outstanding.
 */

export * from "./buffer.ts";
export * from "./detector.ts";
export * from "./input-type.ts";
