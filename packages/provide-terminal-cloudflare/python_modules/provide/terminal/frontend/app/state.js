import { analyzeSession, clearSession, fetchRecordingEntries, fetchSessionDetails, fetchSessions, setSessionMode, } from "./api.js";
export function summarizeSessions(sessions) {
    return {
        running: sessions.filter((session) => session.connected && session.lifecycleState === "running"),
        stopped: sessions.filter((session) => !session.connected && session.lifecycleState !== "error"),
        degraded: sessions.filter((session) => session.lifecycleState === "error" || session.lastError !== null),
    };
}
export function sessionStatus(summary) {
    if (summary.lastError !== null) {
        return { tone: "error", text: summary.lastError };
    }
    if (summary.connected) {
        return { tone: "ok", text: `${summary.displayName} is live in ${summary.inputMode} mode.` };
    }
    return { tone: "info", text: `${summary.displayName} is currently offline.` };
}
export async function loadDashboardState() {
    return fetchSessions();
}
export async function loadSessionRuntimeState(sessionId) {
    const details = await fetchSessionDetails(sessionId);
    return {
        summary: details.summary,
        snapshotPromptId: details.snapshotPromptId,
        analysis: null,
    };
}
export async function loadOperatorWorkspaceState(sessionId) {
    const session = await loadSessionRuntimeState(sessionId);
    return {
        session,
        status: sessionStatus(session.summary),
        modeCommand: { pending: false, lastError: null },
        utilityCommand: { pending: false, lastError: null },
        widget: { mounted: false, error: null },
    };
}
export async function loadUserWorkspaceState(sessionId) {
    const session = await loadSessionRuntimeState(sessionId);
    return {
        session,
        status: sessionStatus(session.summary),
        widget: { mounted: false, error: null },
    };
}
export async function switchSessionMode(sessionId, nextMode) {
    const summary = await setSessionMode(sessionId, nextMode);
    const details = await fetchSessionDetails(sessionId);
    return {
        summary,
        snapshotPromptId: details.snapshotPromptId,
        analysis: null,
    };
}
export async function clearRuntime(sessionId) {
    const summary = await clearSession(sessionId);
    const details = await fetchSessionDetails(sessionId);
    return {
        summary,
        snapshotPromptId: details.snapshotPromptId,
        analysis: null,
    };
}
export async function requestAnalysis(sessionId) {
    return analyzeSession(sessionId);
}
export async function loadReplayState(sessionId, filter, limit) {
    const entries = await fetchRecordingEntries(sessionId, filter, limit);
    return {
        entries,
        index: entries.length > 0 ? entries.length - 1 : 0,
        filter,
        limit,
        total: entries.length,
        status: entries.length > 0
            ? { tone: "ok", text: `Loaded ${entries.length} recording entries.` }
            : { tone: "info", text: "No entries match the current filter." },
    };
}
