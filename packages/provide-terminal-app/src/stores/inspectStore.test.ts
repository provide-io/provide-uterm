//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { HttpRequestEntry, HttpResponseEntry } from "../api/types";
import { useInspectStore } from "./inspectStore";

const REQ: HttpRequestEntry = {
  type: "http_req",
  id: "r1",
  ts: 1000,
  method: "GET",
  url: "https://example.com/api/test",
  headers: { "content-type": "application/json" },
  body_size: 0,
};

const RES: HttpResponseEntry = {
  type: "http_res",
  id: "r1",
  ts: 1001,
  status: 200,
  status_text: "OK",
  headers: { "content-type": "application/json" },
  body_size: 42,
  duration_ms: 120,
};

function resetStore() {
  useInspectStore.getState().clear();
}

beforeEach(resetStore);
afterEach(resetStore);

describe("inspectStore", () => {
  describe("initial state", () => {
    it("starts with empty exchanges", () => {
      expect(useInspectStore.getState().exchanges).toEqual([]);
    });

    it("has no selection", () => {
      expect(useInspectStore.getState().selected).toBeNull();
    });

    it("starts with inspect enabled", () => {
      expect(useInspectStore.getState().inspectEnabled).toBe(true);
    });

    it("starts with intercept disabled", () => {
      expect(useInspectStore.getState().interceptEnabled).toBe(false);
    });

    it("starts with connecting status", () => {
      expect(useInspectStore.getState().wsStatus).toBe("connecting");
    });
  });

  describe("addRequest", () => {
    it("adds an exchange from a request", () => {
      useInspectStore.getState().addRequest(REQ);
      const exchanges = useInspectStore.getState().exchanges;
      expect(exchanges).toHaveLength(1);
      expect(exchanges[0]!.id).toBe("r1");
      expect(exchanges[0]!.request).toBe(REQ);
      expect(exchanges[0]!.response).toBeNull();
    });

    it("marks intercepted requests", () => {
      useInspectStore.getState().addRequest({ ...REQ, intercepted: true });
      expect(useInspectStore.getState().exchanges[0]!.intercepted).toBe(true);
    });
  });

  describe("addResponse", () => {
    it("pairs response with existing exchange", () => {
      useInspectStore.getState().addRequest(REQ);
      useInspectStore.getState().addResponse(RES);
      const ex = useInspectStore.getState().exchanges[0]!;
      expect(ex.response).toBe(RES);
    });

    it("ignores response for unknown id", () => {
      useInspectStore.getState().addRequest(REQ);
      useInspectStore.getState().addResponse({ ...RES, id: "unknown" });
      expect(useInspectStore.getState().exchanges[0]!.response).toBeNull();
    });
  });

  describe("resolveIntercept", () => {
    it("marks exchange as resolved with action", () => {
      useInspectStore.getState().addRequest({ ...REQ, intercepted: true });
      useInspectStore.getState().resolveIntercept("r1", "forward");
      const ex = useInspectStore.getState().exchanges[0]!;
      expect(ex.interceptResolved).toBe(true);
      expect(ex.interceptAction).toBe("forward");
    });
  });

  describe("syncInterceptState", () => {
    it("updates inspect/intercept toggle state", () => {
      useInspectStore.getState().syncInterceptState({
        inspect_enabled: true,
        enabled: true,
        timeout_s: 60,
        timeout_action: "drop",
      });
      const s = useInspectStore.getState();
      expect(s.inspectEnabled).toBe(true);
      expect(s.interceptEnabled).toBe(true);
      expect(s.interceptTimeout).toBe(60);
      expect(s.interceptTimeoutAction).toBe("drop");
    });
  });

  describe("filters", () => {
    it("updates method filter", () => {
      useInspectStore.getState().setMethodFilter("POST");
      expect(useInspectStore.getState().methodFilter).toBe("POST");
    });

    it("updates url filter", () => {
      useInspectStore.getState().setUrlFilter("example");
      expect(useInspectStore.getState().urlFilter).toBe("example");
    });
  });

  describe("selection", () => {
    it("selects an exchange", () => {
      useInspectStore.getState().select("r1");
      expect(useInspectStore.getState().selected).toBe("r1");
    });

    it("deselects", () => {
      useInspectStore.getState().select("r1");
      useInspectStore.getState().select(null);
      expect(useInspectStore.getState().selected).toBeNull();
    });
  });

  describe("toggles", () => {
    it("toggles inspect", () => {
      useInspectStore.getState().setInspectEnabled(false);
      expect(useInspectStore.getState().inspectEnabled).toBe(false);
    });

    it("toggles intercept", () => {
      useInspectStore.getState().setInterceptEnabled(true);
      expect(useInspectStore.getState().interceptEnabled).toBe(true);
    });
  });

  describe("wsStatus", () => {
    it("updates websocket status", () => {
      useInspectStore.getState().setWsStatus("connected");
      expect(useInspectStore.getState().wsStatus).toBe("connected");
    });
  });

  describe("clear", () => {
    it("resets all state", () => {
      useInspectStore.getState().addRequest(REQ);
      useInspectStore.getState().select("r1");
      useInspectStore.getState().setMethodFilter("GET");
      useInspectStore.getState().setWsStatus("connected");
      useInspectStore.getState().clear();
      const s = useInspectStore.getState();
      expect(s.exchanges).toEqual([]);
      expect(s.selected).toBeNull();
      expect(s.methodFilter).toBe("");
      expect(s.wsStatus).toBe("connecting");
    });
  });
});
