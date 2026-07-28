//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { apiJson } from "./client";
import { normalizeRecordingEntries, normalizeSessionStatus } from "./normalize";
import { routeCall } from "./paths";
import type {
  QuickConnectPayload,
  QuickConnectResult,
  RecordingEntryView,
  SessionDetails,
  SessionSummary,
} from "./types";
import { parseRawRecordingEntries, parseRawSessionStatus, parseRawSessionStatusList } from "./validators";

/**
 * Send one operation from the shared contract.
 *
 * The path and the method both come from the table the server and the Worker
 * dispatch from, so neither is written out here.
 */
async function call<T>(
  operation: string,
  params: Readonly<Record<string, string>> = {},
  body: unknown = null,
): Promise<T> {
  const { method, path } = routeCall(operation, params);
  return apiJson<T>(path, method, body);
}

export async function fetchSessions(): Promise<SessionSummary[]> {
  const payload = await call<unknown>("sessions.list");
  return parseRawSessionStatusList(payload).map(normalizeSessionStatus);
}

export async function fetchSessionSummary(sessionId: string): Promise<SessionSummary> {
  const raw = await call<unknown>("sessions.get", { session_id: sessionId });
  return normalizeSessionStatus(parseRawSessionStatus(raw));
}

export async function fetchSessionDetails(sessionId: string): Promise<SessionDetails> {
  const [summary, snapshot] = await Promise.all([
    fetchSessionSummary(sessionId),
    call<{ prompt_detected?: { prompt_id?: string } | null } | null>("sessions.snapshot", {
      session_id: sessionId,
    }),
  ]);
  return {
    summary,
    snapshotPromptId: snapshot?.prompt_detected?.prompt_id ?? null,
  };
}

export async function setSessionMode(sessionId: string, inputMode: "open" | "hijack"): Promise<SessionSummary> {
  const raw = await call<unknown>("sessions.set_mode", { session_id: sessionId }, { input_mode: inputMode });
  return normalizeSessionStatus(parseRawSessionStatus(raw));
}

export async function clearSession(sessionId: string): Promise<SessionSummary> {
  const raw = await call<unknown>("sessions.clear", { session_id: sessionId });
  return normalizeSessionStatus(parseRawSessionStatus(raw));
}

export async function restartSession(sessionId: string): Promise<SessionSummary> {
  const raw = await call<unknown>("sessions.restart", { session_id: sessionId });
  return normalizeSessionStatus(parseRawSessionStatus(raw));
}

export async function analyzeSession(sessionId: string): Promise<string> {
  const result = await call<{ analysis: string }>("sessions.analyze", { session_id: sessionId });
  return result.analysis;
}

export async function fetchRecordingEntries(
  sessionId: string,
  filter: string,
  limit: number,
): Promise<RecordingEntryView[]> {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  if (filter) params.set("event", filter);
  // The query is the SPA's own; only the path comes from the contract.
  const { method, path } = routeCall("sessions.recording_entries", { session_id: sessionId });
  const result = await apiJson<unknown>(`${path}?${params.toString()}`, method);
  return normalizeRecordingEntries(parseRawRecordingEntries(result));
}

export async function quickConnect(payload: QuickConnectPayload): Promise<QuickConnectResult> {
  return call<QuickConnectResult>("tunnels.connect", {}, payload);
}
