//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { AnalysisResponse, RecordingEntry, SessionStatus, SnapshotPayload } from "../server-common.js";
import { apiJson } from "../server-common.js";
import type { ConnectionProfile, RecordingEntryView, SessionDetails, SessionSummary, SessionSurface } from "./types.js";

function normalizeMode(value: string): "open" | "hijack" {
  return value === "hijack" ? "hijack" : "open";
}

export function normalizeSessionStatus(raw: SessionStatus): SessionSummary {
  return {
    sessionId: raw.session_id,
    displayName: raw.display_name,
    connectorType: raw.connector_type,
    lifecycleState: raw.lifecycle_state,
    inputMode: normalizeMode(raw.input_mode),
    connected: raw.connected,
    autoStart: raw.auto_start,
    tags: [...raw.tags],
    recordingEnabled: raw.recording_enabled,
    recordingAvailable: raw.recording_available,
    owner: raw.owner ?? null,
    visibility: raw.visibility ?? "public",
    lastError: raw.last_error,
  };
}

export function normalizeRecordingEntries(entries: RecordingEntry[]): RecordingEntryView[] {
  return entries.map((entry) => {
    const payload = (entry.data ?? {}) as Record<string, unknown>;
    return {
      ts: typeof entry.ts === "number" ? entry.ts : null,
      event: typeof entry.event === "string" ? entry.event : "unknown",
      payload,
      screen: typeof payload.screen === "string" ? payload.screen : "",
    };
  });
}

export async function fetchSessions(): Promise<SessionSummary[]> {
  const payload = await apiJson<SessionStatus[]>("/api/sessions");
  return payload.map(normalizeSessionStatus);
}

export async function fetchSessionSummary(sessionId: string): Promise<SessionSummary> {
  return normalizeSessionStatus(await apiJson<SessionStatus>(`/api/sessions/${encodeURIComponent(sessionId)}`));
}

export async function fetchSessionDetails(sessionId: string): Promise<SessionDetails> {
  const [summary, snapshot] = await Promise.all([
    fetchSessionSummary(sessionId),
    apiJson<SnapshotPayload | null>(`/api/sessions/${encodeURIComponent(sessionId)}/snapshot`),
  ]);
  return {
    summary,
    snapshotPromptId: snapshot?.prompt_detected?.prompt_id ?? null,
  };
}

export async function setSessionMode(sessionId: string, inputMode: "open" | "hijack"): Promise<SessionSummary> {
  return normalizeSessionStatus(
    await apiJson<SessionStatus>(`/api/sessions/${encodeURIComponent(sessionId)}/mode`, "POST", {
      input_mode: inputMode,
    }),
  );
}

export async function clearSession(sessionId: string): Promise<SessionSummary> {
  return normalizeSessionStatus(
    await apiJson<SessionStatus>(`/api/sessions/${encodeURIComponent(sessionId)}/clear`, "POST"),
  );
}

export async function restartSession(sessionId: string): Promise<SessionSummary> {
  return normalizeSessionStatus(
    await apiJson<SessionStatus>(`/api/sessions/${encodeURIComponent(sessionId)}/restart`, "POST"),
  );
}

export async function deleteSession(sessionId: string): Promise<void> {
  await apiJson<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(sessionId)}`, "DELETE");
}

export async function analyzeSession(sessionId: string): Promise<string> {
  const result = await apiJson<AnalysisResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/analyze`, "POST");
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
  const result = await apiJson<RecordingEntry[]>(
    `/api/sessions/${encodeURIComponent(sessionId)}/recording/entries?${params.toString()}`,
  );
  return normalizeRecordingEntries(result);
}

export interface QuickConnectPayload {
  connector_type: string;
  display_name?: string;
  input_mode?: string;
  tags?: string[];
  host?: string;
  port?: number;
  username?: string;
  password?: string;
}

export interface QuickConnectResult {
  session_id: string;
  url: string;
}

export async function quickConnect(payload: QuickConnectPayload): Promise<QuickConnectResult> {
  return apiJson<QuickConnectResult>("/api/connect", "POST", payload);
}

export function widgetSurface(surface: SessionSurface | undefined): { showAnalysis: boolean; mobileKeys: boolean } {
  const isOperator = surface === "operator";
  return {
    showAnalysis: isOperator,
    mobileKeys: isOperator,
  };
}

export async function fetchProfiles(): Promise<ConnectionProfile[]> {
  return apiJson<ConnectionProfile[]>("/api/profiles");
}

export async function fetchProfile(profileId: string): Promise<ConnectionProfile | null> {
  try {
    return await apiJson<ConnectionProfile>(`/api/profiles/${encodeURIComponent(profileId)}`);
  } catch {
    return null;
  }
}

export async function createProfile(
  payload: Partial<ConnectionProfile> & { name: string; connector_type: string },
): Promise<ConnectionProfile> {
  return apiJson<ConnectionProfile>("/api/profiles", "POST", payload);
}

export async function deleteProfile(profileId: string): Promise<void> {
  await apiJson<{ ok: boolean }>(`/api/profiles/${encodeURIComponent(profileId)}`, "DELETE");
}

export async function connectFromProfile(profileId: string, password?: string): Promise<QuickConnectResult> {
  return apiJson<QuickConnectResult>(
    `/api/profiles/${encodeURIComponent(profileId)}/connect`,
    "POST",
    password ? { password } : {},
  );
}
