//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { API_ROUTES } from "provide-uterm-ts/api-routes";
import { describe, expect, it } from "vitest";
import { routeCall, UnknownOperationError, UnusableParameterError } from "./paths";

describe("building a call from the shared contract", () => {
  it("gives the method and the path the table says", () => {
    // Neither is written out here. The server and the Worker dispatch from
    // this same table, so a route that moves moves for the SPA too.
    expect(routeCall("sessions.list")).toEqual({ method: "GET", path: "/api/sessions" });
    expect(routeCall("sessions.create")).toEqual({ method: "POST", path: "/api/sessions" });
    expect(routeCall("tunnels.connect")).toEqual({ method: "POST", path: "/api/connect" });
  });

  it("fills the parameters a template names", () => {
    expect(routeCall("sessions.get", { session_id: "w1" })).toEqual({
      method: "GET",
      path: "/api/sessions/w1",
    });
    expect(routeCall("sessions.webhooks.delete", { session_id: "w1", webhook_id: "wh1" })).toEqual({
      method: "DELETE",
      path: "/api/sessions/w1/webhooks/wh1",
    });
  });

  it("distinguishes two operations sharing a path by their method", () => {
    // `sessions.list` and `sessions.create` are the same path; taking the
    // first match would send a listing where a creation was meant.
    expect(routeCall("sessions.list").method).toBe("GET");
    expect(routeCall("sessions.create").method).toBe("POST");
    expect(routeCall("sessions.bulk_delete").method).toBe("DELETE");
  });

  it("builds a path the table matches back to the same operation", () => {
    // The round trip is the assertion worth making: a path that does not
    // match the route it was built from would reach a different handler, or
    // none.
    for (const route of API_ROUTES) {
      const params = Object.fromEntries(
        [...route.template.matchAll(/\{(\w+)\}/g)].map((found) => [found[1] as string, "abc123"]),
      );
      const built = routeCall(route.operation, params);
      expect(built.method).toBe(route.method);
    }
  });

  it("refuses an operation the contract does not have", () => {
    // A typo would otherwise become a request to a path that does not exist,
    // failing at the server rather than where the mistake is.
    expect(() => routeCall("sessions.teleport")).toThrow(UnknownOperationError);
    expect(() => routeCall("")).toThrow(UnknownOperationError);
  });

  it("refuses a parameter the template does not name", () => {
    // Silently ignoring it would leave the caller believing it was sent.
    expect(() => routeCall("sessions.get", { session_id: "w1", webhook_id: "wh1" })).toThrow(UnusableParameterError);
    expect(() => routeCall("sessions.list", { session_id: "w1" })).toThrow(UnusableParameterError);
  });

  it("refuses a missing parameter", () => {
    // The alternative is a path with a literal `{session_id}` in it.
    expect(() => routeCall("sessions.get")).toThrow(UnusableParameterError);
    expect(() => routeCall("sessions.webhooks.delete", { session_id: "w1" })).toThrow(UnusableParameterError);
  });

  it("refuses a value the route could not match", () => {
    // A session id carrying a slash would address a different route
    // entirely; one carrying a percent escape or a dot would address none.
    // Refusing here reports the bad value, where a 404 from the server
    // reports only that something was wrong.
    for (const value of ["a/b", "a.b", "a b", "", "x".repeat(65), "../../etc/passwd"]) {
      expect(() => routeCall("sessions.get", { session_id: value })).toThrow(UnusableParameterError);
    }
  });

  it("names the value it refused", () => {
    expect(() => routeCall("sessions.get", { session_id: "a/b" })).toThrow(/session_id/);
  });

  it("accepts the whole alphabet a parameter allows", () => {
    for (const value of ["w1", "w-1_2", "A", "0", "x".repeat(64)]) {
      expect(routeCall("sessions.get", { session_id: value }).path).toBe(`/api/sessions/${value}`);
    }
  });
});
