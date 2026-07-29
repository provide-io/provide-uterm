//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What the reconnect suites are built from.
 *
 * The reconnect tests outgrew one file — the 777-line cap is per file — so
 * the corpus shape and the two stand-ins live here rather than being copied
 * into each half, where they would drift apart one edit at a time.
 *
 * `src/testing/` is excluded from coverage: it is scaffolding, not subject.
 */

import { loadGolden } from "./golden.ts";
export interface ReconnectGolden {
  defaults: { max_retries: number; base_backoff_s: number; max_backoff_s: number };
  schedules: Array<{
    name: string;
    max_retries: number;
    base_backoff_s: number;
    max_backoff_s: number;
    delays: number[];
  }>;
  classification: Array<{ name: string; error: string; retryable: boolean }>;
  exhausted_message: string;
  connect_exhausted_message: string;
  sequences: Array<{
    name: string;
    log: Array<Array<string | number>>;
    error: string | null;
    message: string | null;
  }>;
}

export const golden = loadGolden<ReconnectGolden>("reconnect_golden.json");

/** The least a reconnecting session can wrap: a name and a close. */
export function named(name: string): { name: string; close(): Promise<void> } {
  return { name, close: async () => {} };
}

/** Records what it was asked to sleep for, without sleeping. */
export function recorder() {
  const slept: number[] = [];
  return { slept, sleep: async (seconds: number) => void slept.push(seconds) };
}
