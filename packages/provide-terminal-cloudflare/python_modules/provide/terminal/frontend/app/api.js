//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { apiJson } from "../server-common.js";
function normalizeMode(value) {
    return value === "hijack" ? "hijack" : "open";
}
export function normalizeSessionStatus(raw) {
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
export function normalizeRecordingEntries(entries) {
    return entries.map((entry) => {
        const payload = (entry.data ?? {});
        return {
            ts: typeof entry.ts === "number" ? entry.ts : null,
            event: typeof entry.event === "string" ? entry.event : "unknown",
            payload,
            screen: typeof payload.screen === "string" ? payload.screen : "",
        };
    });
}
export async function fetchSessions() {
    const payload = await apiJson("/api/sessions");
    return payload.map(normalizeSessionStatus);
}
export async function fetchSessionSummary(sessionId) {
    return normalizeSessionStatus(await apiJson(`/api/sessions/${encodeURIComponent(sessionId)}`));
}
export async function fetchSessionDetails(sessionId) {
    const [summary, snapshot] = await Promise.all([
        fetchSessionSummary(sessionId),
        apiJson(`/api/sessions/${encodeURIComponent(sessionId)}/snapshot`),
    ]);
    return {
        summary,
        snapshotPromptId: snapshot?.prompt_detected?.prompt_id ?? null,
    };
}
export async function setSessionMode(sessionId, inputMode) {
    return normalizeSessionStatus(await apiJson(`/api/sessions/${encodeURIComponent(sessionId)}/mode`, "POST", {
        input_mode: inputMode,
    }));
}
export async function clearSession(sessionId) {
    return normalizeSessionStatus(await apiJson(`/api/sessions/${encodeURIComponent(sessionId)}/clear`, "POST"));
}
export async function restartSession(sessionId) {
    return normalizeSessionStatus(await apiJson(`/api/sessions/${encodeURIComponent(sessionId)}/restart`, "POST"));
}
export async function deleteSession(sessionId) {
    await apiJson(`/api/sessions/${encodeURIComponent(sessionId)}`, "DELETE");
}
export async function analyzeSession(sessionId) {
    const result = await apiJson(`/api/sessions/${encodeURIComponent(sessionId)}/analyze`, "POST");
    return result.analysis;
}
export async function fetchRecordingEntries(sessionId, filter, limit) {
    const params = new URLSearchParams();
    params.set("limit", String(limit));
    if (filter)
        params.set("event", filter);
    const result = await apiJson(`/api/sessions/${encodeURIComponent(sessionId)}/recording/entries?${params.toString()}`);
    return normalizeRecordingEntries(result);
}
export async function quickConnect(payload) {
    return apiJson("/api/connect", "POST", payload);
}
export function widgetSurface(surface) {
    const isOperator = surface === "operator";
    return {
        showAnalysis: isOperator,
        mobileKeys: isOperator,
    };
}
