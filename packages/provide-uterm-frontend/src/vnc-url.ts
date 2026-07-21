//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/** Pure URL/status helpers for the VNC console (no RFB client import). */

export type VncStatusState =
  | "idle"
  | "connecting"
  | "connected"
  | "denied"
  | "unavailable"
  | "error"
  | "disconnected";

export interface VncPageParams {
  workerId: string;
  hijackId: string;
  targetId: string;
  viewOnly: boolean;
  token: string | null;
}

export interface VncStatusInfo {
  state: VncStatusState;
  message: string;
}

/** Sanitize path segment used in WS URL (worker / hijack / target ids). */
export function sanitizeId(value: string | null | undefined, fallback = ""): string {
  if (!value) return fallback;
  const trimmed = value.trim();
  if (!/^[A-Za-z0-9_.-]{1,128}$/.test(trimmed)) {
    return fallback;
  }
  return trimmed;
}

export function readVncPageParams(
  search: string = typeof window !== "undefined" ? window.location.search : "",
): VncPageParams {
  const params = new URLSearchParams(search);
  const viewRaw = (params.get("view_only") || params.get("viewOnly") || "").toLowerCase();
  const token = params.get("token") || params.get("access_token") || params.get("accessToken");
  return {
    workerId: sanitizeId(params.get("worker_id") || params.get("worker"), ""),
    hijackId: sanitizeId(params.get("hijack_id") || params.get("hijack"), ""),
    targetId: sanitizeId(params.get("target_id") || params.get("target"), ""),
    viewOnly: viewRaw === "1" || viewRaw === "true" || viewRaw === "yes",
    token: token && token.trim() ? token.trim() : null,
  };
}

/**
 * Build the binary WebSocket URL for the human VNC relay.
 * Path matches server route:
 *   /worker/{worker_id}/hijack/{hijack_id}/gui/vnc?target_id=…
 */
export function buildVncWsUrl(
  params: Pick<VncPageParams, "workerId" | "hijackId" | "targetId">,
  loc: Pick<Location, "protocol" | "host"> = typeof window !== "undefined"
    ? window.location
    : { protocol: "http:", host: "localhost" },
): string {
  if (!params.workerId || !params.hijackId || !params.targetId) {
    throw new Error("worker_id, hijack_id, and target_id are required");
  }
  const wsProto = loc.protocol === "https:" ? "wss:" : "ws:";
  const path =
    `/worker/${encodeURIComponent(params.workerId)}` +
    `/hijack/${encodeURIComponent(params.hijackId)}` +
    `/gui/vnc?target_id=${encodeURIComponent(params.targetId)}`;
  return `${wsProto}//${loc.host}${path}`;
}

export function statusFromCloseCode(code: number | undefined): VncStatusInfo {
  if (code === 1008) {
    return { state: "denied", message: "Denied (authz / lease)" };
  }
  if (code === 1013) {
    return { state: "unavailable", message: "Upstream unavailable (1013)" };
  }
  if (code === 1000 || code === 1001) {
    return { state: "disconnected", message: "Disconnected" };
  }
  return { state: "error", message: `Connection closed (${code ?? "unknown"})` };
}
