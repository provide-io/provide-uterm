//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Pure-helper module split out of hijack.ts: mobile-key data consumed by the
 * Lit session-element toolbar (no widget state).
 */

export interface MobileKey {
  label: string;
  data: string;
}

export const MOBILE_KEYS: ReadonlyArray<MobileKey> = [
  { label: "ESC", data: "\x1b" },
  { label: "↑", data: "\x1b[A" },
  { label: "↓", data: "\x1b[B" },
  { label: "→", data: "\x1b[C" },
  { label: "←", data: "\x1b[D" },
  { label: "Tab", data: "\t" },
  { label: "^C", data: "\x03" },
  { label: "^D", data: "\x04" },
  { label: "^Z", data: "\x1a" },
];
