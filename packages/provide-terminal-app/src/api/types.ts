//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

export type AppPageKind = "dashboard" | "session" | "operator" | "replay" | "connect";
export type SessionMode = "open" | "hijack";
export type SessionSurface = "user" | "operator";

export interface SessionSummary {
  sessionId: string;
  displayName: string;
  connectorType: string;
  lifecycleState: string;
  inputMode: SessionMode;
  connected: boolean;
  autoStart: boolean;
  tags: string[];
  recordingEnabled: boolean;
  recordingAvailable: boolean;
  owner: string | null;
  visibility: string;
  lastError: string | null;
}

export interface SessionDetails {
  summary: SessionSummary;
  snapshotPromptId: string | null;
}

export interface RecordingEntryView {
  ts: number | null;
  event: string;
  payload: Record<string, unknown>;
  screen: string;
}

export interface AppBootstrap {
  page_kind: AppPageKind;
  title: string;
  app_path: string;
  assets_path: string;
  session_id?: string;
  surface?: SessionSurface;
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
