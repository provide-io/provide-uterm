//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

export type AppPageKind = "dashboard" | "session" | "operator" | "replay" | "connect" | "inspect";
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

// ── HTTP Inspect types ──────────────────────────────────────────────────────

export interface HttpRequestEntry {
  type: "http_req";
  id: string;
  ts: number;
  method: string;
  url: string;
  headers: Record<string, string>;
  body_size: number;
  body_b64?: string;
  body_truncated?: boolean;
  body_binary?: boolean;
  intercepted?: boolean;
}

export interface HttpResponseEntry {
  type: "http_res";
  id: string;
  ts: number;
  status: number;
  status_text: string;
  headers: Record<string, string>;
  body_size: number;
  body_b64?: string;
  body_truncated?: boolean;
  body_binary?: boolean;
  duration_ms: number;
}

export interface HttpInterceptStateFrame {
  type: "http_intercept_state";
  inspect_enabled: boolean;
  enabled: boolean;
  timeout_s: number;
  timeout_action: string;
}

export interface HttpExchangeEntry {
  id: string;
  request: HttpRequestEntry;
  response: HttpResponseEntry | null;
  intercepted: boolean;
  interceptResolved: boolean;
  interceptAction: string | null;
}

export interface HttpInspectToggle {
  type: "http_inspect_toggle";
  enabled: boolean;
}

export interface HttpInterceptToggle {
  type: "http_intercept_toggle";
  enabled: boolean;
}
