//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiJson } from "./client";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function jsonResponse(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(data),
  } as Response;
}

describe("apiJson", () => {
  it("makes a GET request by default", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [] }));
    const result = await apiJson<{ items: unknown[] }>("/api/test");
    expect(result).toEqual({ items: [] });
    expect(fetchMock).toHaveBeenCalledWith("/api/test", {
      method: "GET",
      headers: { "Content-Type": "application/json" },
    });
  });

  it("sends POST with JSON body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await apiJson("/api/action", "POST", { key: "val" });
    expect(fetchMock).toHaveBeenCalledWith("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: '{"key":"val"}',
    });
  });

  it("does not set body when body is null", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await apiJson("/api/data", "GET", null);
    expect(fetchMock).toHaveBeenCalledWith("/api/data", {
      method: "GET",
      headers: { "Content-Type": "application/json" },
    });
  });

  it("sends PATCH request", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ updated: true }));
    await apiJson("/api/update", "PATCH", { name: "new" });
    expect(fetchMock).toHaveBeenCalledWith("/api/update", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: '{"name":"new"}',
    });
  });

  it("sends DELETE request", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await apiJson("/api/remove", "DELETE");
    expect(fetchMock).toHaveBeenCalledWith("/api/remove", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
    });
  });

  it("throws on non-ok response with status code", async () => {
    fetchMock.mockResolvedValue(jsonResponse(null, 404));
    await expect(apiJson("/api/missing")).rejects.toThrow("404");
  });

  it("throws on 500 response", async () => {
    fetchMock.mockResolvedValue(jsonResponse(null, 500));
    await expect(apiJson("/api/broken")).rejects.toThrow("500");
  });

  it("propagates network errors", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(apiJson("/api/offline")).rejects.toThrow("Failed to fetch");
  });
});
