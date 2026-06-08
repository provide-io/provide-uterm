//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

export type ApprovalMode = "modal" | "statusbar";

export function computeRemainingSeconds(expiresAt: number, nowMs: number = Date.now()): number {
  return Math.max(0, Math.round(expiresAt - nowMs / 1000));
}
