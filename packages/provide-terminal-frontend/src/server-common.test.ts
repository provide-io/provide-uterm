//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiJson, readBooleanDataset, readDataset, requireElement } from "./server-common.js";

describe("apiJson", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("calls fetch with correct method and headers for GET", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ result: "ok" }),
    });
    vi.stubGlobal("fetch", mockFetch);

    const result = await apiJson<{ result: string }>("/api/test");
    expect(result).toEqual({ result: "ok" });
    expect(mockFetch).toHaveBeenCalledWith("/api/test", {
      method: "GET",
      headers: { "Content-Type": "application/json" },
    });
  });

  it("sends body for POST requests", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ ok: true }),
    });
    vi.stubGlobal("fetch", mockFetch);

    await apiJson("/api/test", "POST", { key: "value" });
    const callArgs = mockFetch.mock.calls[0][1] as RequestInit;
    expect(callArgs.method).toBe("POST");
    expect(callArgs.body).toBe(JSON.stringify({ key: "value" }));
  });

  it("does not include body when body is null", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });
    vi.stubGlobal("fetch", mockFetch);

    await apiJson("/api/test", "GET", null);
    const callArgs = mockFetch.mock.calls[0][1] as RequestInit;
    expect(callArgs.body).toBeUndefined();
  });

  it("throws with status code on non-ok response", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
    });
    vi.stubGlobal("fetch", mockFetch);

    await expect(apiJson("/api/missing")).rejects.toThrow("404");
  });

  it("supports DELETE method", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ ok: true }),
    });
    vi.stubGlobal("fetch", mockFetch);

    await apiJson("/api/test", "DELETE");
    const callArgs = mockFetch.mock.calls[0][1] as RequestInit;
    expect(callArgs.method).toBe("DELETE");
  });

  it("supports PATCH method with body", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });
    vi.stubGlobal("fetch", mockFetch);

    await apiJson("/api/test", "PATCH", { patch: "data" });
    const callArgs = mockFetch.mock.calls[0][1] as RequestInit;
    expect(callArgs.method).toBe("PATCH");
    expect(callArgs.body).toBe(JSON.stringify({ patch: "data" }));
  });
});

describe("requireElement", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("returns element when found by selector", () => {
    const div = document.createElement("div");
    div.id = "test-el";
    document.body.appendChild(div);
    const el = requireElement<HTMLDivElement>("#test-el");
    expect(el).toBe(div);
  });

  it("throws when element not found", () => {
    expect(() => requireElement("#nonexistent")).toThrow("Missing required element: #nonexistent");
  });

  it("searches within provided root", () => {
    const root = document.createElement("div");
    const child = document.createElement("span");
    child.className = "target";
    root.appendChild(child);
    const result = requireElement<HTMLSpanElement>(".target", root);
    expect(result).toBe(child);
  });

  it("throws when element not found in root", () => {
    const root = document.createElement("div");
    expect(() => requireElement(".target", root)).toThrow("Missing required element: .target");
  });
});

describe("readDataset", () => {
  it("returns dataset value when present and non-empty", () => {
    const el = document.createElement("div");
    el.dataset.myAttr = "hello";
    expect(readDataset(el, "myAttr")).toBe("hello");
  });

  it("throws when dataset attribute is missing", () => {
    const el = document.createElement("div");
    expect(() => readDataset(el, "missingAttr")).toThrow("Missing required data attribute: missingAttr");
  });

  it("throws when dataset attribute is empty string", () => {
    const el = document.createElement("div");
    el.dataset.emptyAttr = "";
    expect(() => readDataset(el, "emptyAttr")).toThrow("Missing required data attribute: emptyAttr");
  });

  it("throws when dataset value is undefined", () => {
    const el = document.createElement("div");
    expect(() => readDataset(el, "notSet")).toThrow("Missing required data attribute: notSet");
  });
});

describe("readBooleanDataset", () => {
  it("returns true when dataset value is 'true'", () => {
    const el = document.createElement("div");
    el.dataset.flag = "true";
    expect(readBooleanDataset(el, "flag")).toBe(true);
  });

  it("returns false when dataset value is 'false'", () => {
    const el = document.createElement("div");
    el.dataset.flag = "false";
    expect(readBooleanDataset(el, "flag")).toBe(false);
  });

  it("returns false for any value other than 'true'", () => {
    const el = document.createElement("div");
    el.dataset.flag = "yes";
    expect(readBooleanDataset(el, "flag")).toBe(false);
  });

  it("throws when dataset attribute is missing (delegates to readDataset)", () => {
    const el = document.createElement("div");
    expect(() => readBooleanDataset(el, "missing")).toThrow("Missing required data attribute: missing");
  });
});
