//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, describe, expect, it } from "vitest";
import { useTerminalStore } from "./terminalStore";

function resetStore() {
  useTerminalStore.setState({
    mounted: false,
    error: null,
    connectionStatus: "disconnected",
    cols: 0,
    rows: 0,
  });
}

afterEach(() => {
  resetStore();
});

describe("terminalStore", () => {
  describe("initial state", () => {
    it("has mounted false", () => {
      expect(useTerminalStore.getState().mounted).toBe(false);
    });

    it("has error null", () => {
      expect(useTerminalStore.getState().error).toBeNull();
    });

    it("has connectionStatus disconnected", () => {
      expect(useTerminalStore.getState().connectionStatus).toBe("disconnected");
    });

    it("has cols 0", () => {
      expect(useTerminalStore.getState().cols).toBe(0);
    });

    it("has rows 0", () => {
      expect(useTerminalStore.getState().rows).toBe(0);
    });
  });

  describe("setMounted", () => {
    it("sets mounted to true", () => {
      useTerminalStore.getState().setMounted(true);
      expect(useTerminalStore.getState().mounted).toBe(true);
      expect(useTerminalStore.getState().error).toBeNull();
    });

    it("sets mounted to false", () => {
      useTerminalStore.getState().setMounted(true);
      useTerminalStore.getState().setMounted(false);
      expect(useTerminalStore.getState().mounted).toBe(false);
    });

    it("sets error when provided", () => {
      useTerminalStore.getState().setMounted(false, "Widget failed");
      expect(useTerminalStore.getState().mounted).toBe(false);
      expect(useTerminalStore.getState().error).toBe("Widget failed");
    });

    it("clears error when not provided", () => {
      useTerminalStore.getState().setMounted(false, "old error");
      useTerminalStore.getState().setMounted(true);
      expect(useTerminalStore.getState().error).toBeNull();
    });
  });

  describe("setConnectionStatus", () => {
    it("sets to connecting", () => {
      useTerminalStore.getState().setConnectionStatus("connecting");
      expect(useTerminalStore.getState().connectionStatus).toBe("connecting");
    });

    it("sets to connected", () => {
      useTerminalStore.getState().setConnectionStatus("connected");
      expect(useTerminalStore.getState().connectionStatus).toBe("connected");
    });

    it("sets back to disconnected", () => {
      useTerminalStore.getState().setConnectionStatus("connected");
      useTerminalStore.getState().setConnectionStatus("disconnected");
      expect(useTerminalStore.getState().connectionStatus).toBe("disconnected");
    });
  });

  describe("setDimensions", () => {
    it("sets cols and rows", () => {
      useTerminalStore.getState().setDimensions(120, 40);
      const { cols, rows } = useTerminalStore.getState();
      expect(cols).toBe(120);
      expect(rows).toBe(40);
    });

    it("updates dimensions on subsequent calls", () => {
      useTerminalStore.getState().setDimensions(80, 24);
      useTerminalStore.getState().setDimensions(132, 50);
      const { cols, rows } = useTerminalStore.getState();
      expect(cols).toBe(132);
      expect(rows).toBe(50);
    });
  });
});
