//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { RecordingEntryView, SessionSummary } from "./types";

interface RawSessionStatus {
  session_id: string;
  display_name: string;
  connector_type: string;
  lifecycle_state: string;
  input_mode: string;
  connected: boolean;
  auto_start: boolean;
  tags: string[];
  recording_enabled: boolean;
  recording_available: boolean;
  owner: string | null;
  visibility: string;
  last_error: string | null;
}

interface RawRecordingEntry {
  ts?: number;
  event?: string;
  data?: Record<string, unknown>;
}

function normalizeMode(value: string): "open" | "hijack" {
  return value === "hijack" ? "hijack" : "open";
}

export function normalizeSessionStatus(raw: RawSessionStatus): SessionSummary {
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

export function normalizeRecordingEntries(entries: RawRecordingEntry[]): RecordingEntryView[] {
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
