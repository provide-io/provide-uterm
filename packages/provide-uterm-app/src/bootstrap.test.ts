//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, describe, expect, it } from "vitest";
import { readBootstrap } from "./bootstrap";

afterEach(() => document.body.replaceChildren());

function installPayload(value: unknown) {
  const script = document.createElement("script");
  script.id = "app-bootstrap";
  script.type = "application/json";
  script.textContent = JSON.stringify(value);
  document.body.appendChild(script);
}

describe("readBootstrap", () => {
  it("reads a complete supported page payload", () => {
    installPayload({ page_kind: "operator", title: "Ops", app_path: "/app", assets_path: "/assets", session_id: "s1" });
    expect(readBootstrap()).toMatchObject({ page_kind: "operator", title: "Ops", session_id: "s1" });
  });

  it("accepts a consumer page kind", () => {
    installPayload({ page_kind: "consumer-reports", title: "Reports", app_path: "/app", assets_path: "/assets" });
    expect(readBootstrap().page_kind).toBe("consumer-reports");
  });

  it("rejects missing, unknown, and incomplete payloads", () => {
    expect(() => readBootstrap()).toThrow("Missing #app-bootstrap payload");
    installPayload({ page_kind: "../future", title: "Ops", app_path: "/app", assets_path: "/assets" });
    expect(() => readBootstrap()).toThrow("Invalid page bootstrap");
    document.body.replaceChildren();
    installPayload({ page_kind: "connect", title: "Ops" });
    expect(() => readBootstrap()).toThrow("Incomplete page bootstrap");
  });
});
