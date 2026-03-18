//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { apiJson } from "./client";
import { normalizeRecordingEntries, normalizeSessionStatus } from "./normalize";
import type { QuickConnectPayload, QuickConnectResult, RecordingEntryView, SessionDetails, SessionSummary } from "./types";

export async function fetchSessions(): Promise<SessionSummary[]> {
  const payload = await apiJson<Record<string, unknown>[]>("/api/sessions");
  // biome-ignore lint/suspicious/noExplicitAny: raw API response
  return payload.map((raw) => normalizeSessionStatus(raw as any));
}

export async function fetchSessionSummary(sessionId: string): Promise<SessionSummary> {
  const raw = await apiJson<Record<string, unknown>>(`/api/sessions/${encodeURIComponent(sessionId)}`);
  // biome-ignore lint/suspicious/noExplicitAny: raw API response
  return normalizeSessionStatus(raw as any);
}

export async function fetchSessionDetails(sessionId: string): Promise<SessionDetails> {
  const [summary, snapshot] = await Promise.all([
    fetchSessionSummary(sessionId),
    apiJson<{ prompt_detected?: { prompt_id?: string } | null } | null>(
      `/api/sessions/${encodeURIComponent(sessionId)}/snapshot`,
    ),
  ]);
  return {
    summary,
    snapshotPromptId: snapshot?.prompt_detected?.prompt_id ?? null,
  };
}

export async function setSessionMode(sessionId: string, inputMode: "open" | "hijack"): Promise<SessionSummary> {
  const raw = await apiJson<Record<string, unknown>>(
    `/api/sessions/${encodeURIComponent(sessionId)}/mode`,
    "POST",
    { input_mode: inputMode },
  );
  // biome-ignore lint/suspicious/noExplicitAny: raw API response
  return normalizeSessionStatus(raw as any);
}

export async function clearSession(sessionId: string): Promise<SessionSummary> {
  const raw = await apiJson<Record<string, unknown>>(
    `/api/sessions/${encodeURIComponent(sessionId)}/clear`,
    "POST",
  );
  // biome-ignore lint/suspicious/noExplicitAny: raw API response
  return normalizeSessionStatus(raw as any);
}

export async function restartSession(sessionId: string): Promise<SessionSummary> {
  const raw = await apiJson<Record<string, unknown>>(
    `/api/sessions/${encodeURIComponent(sessionId)}/restart`,
    "POST",
  );
  // biome-ignore lint/suspicious/noExplicitAny: raw API response
  return normalizeSessionStatus(raw as any);
}

export async function analyzeSession(sessionId: string): Promise<string> {
  const result = await apiJson<{ analysis: string }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/analyze`,
    "POST",
  );
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
  const result = await apiJson<Record<string, unknown>[]>(
    `/api/sessions/${encodeURIComponent(sessionId)}/recording/entries?${params.toString()}`,
  );
  // biome-ignore lint/suspicious/noExplicitAny: raw API response
  return normalizeRecordingEntries(result as any);
}

export async function quickConnect(payload: QuickConnectPayload): Promise<QuickConnectResult> {
  return apiJson<QuickConnectResult>("/api/connect", "POST", payload);
}
