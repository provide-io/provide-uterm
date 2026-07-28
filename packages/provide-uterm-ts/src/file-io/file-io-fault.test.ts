//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Fault-injection cover for the one path `secureCreate` cannot be driven into
 * with a real filesystem: a failure *after* the descriptor is open.
 *
 * The guarantee under test is resource safety — the descriptor must be closed
 * before the error propagates, or a long-running server leaks one per failed
 * open. The mock is confined to this file so every other test runs against
 * the real filesystem.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const closed: number[] = [];

vi.mock("node:fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs")>();
  return {
    ...actual,
    fchmodSync: (): never => {
      throw new Error("injected fchmod failure");
    },
    closeSync: (fd: number): void => {
      closed.push(fd);
      actual.closeSync(fd);
    },
  };
});

const { secureCreate } = await import("./index.ts");

let workDir: string;

beforeEach(() => {
  closed.length = 0;
  workDir = mkdtempSync(join(tmpdir(), "uterm-file-io-fault-"));
});

afterEach(() => {
  rmSync(workDir, { recursive: true, force: true });
  vi.restoreAllMocks();
});

describe("secureCreate under failure after open", () => {
  it("closes the descriptor before propagating the error", () => {
    expect(() => secureCreate(join(workDir, "sink.log"))).toThrow("injected fchmod failure");
    expect(closed).toHaveLength(1);
  });
});
