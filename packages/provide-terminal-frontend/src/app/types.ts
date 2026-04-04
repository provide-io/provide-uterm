//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

export type AppPageKind = "dashboard" | "session" | "operator" | "replay" | "connect" | "inspect";
export type SessionMode = "open" | "hijack";
export type SessionSurface = "user" | "operator";
export type AsyncState<T> = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; data: T };

export interface ViewModelStatus {
  tone: "info" | "ok" | "error";
  text: string;
}

export interface CommandState {
  pending: boolean;
  lastError: string | null;
}

export interface WidgetMountState {
  mounted: boolean;
  error: string | null;
}

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

export interface SessionRuntimeState {
  summary: SessionSummary;
  snapshotPromptId: string | null;
  analysis: string | null;
}

export interface OperatorWorkspaceState {
  session: SessionRuntimeState;
  status: ViewModelStatus;
  modeCommand: CommandState;
  utilityCommand: CommandState;
  widget: WidgetMountState;
}

export interface UserWorkspaceState {
  session: SessionRuntimeState;
  status: ViewModelStatus;
  widget: WidgetMountState;
}

export interface RecordingEntryView {
  ts: number | null;
  event: string;
  payload: Record<string, unknown>;
  screen: string;
}

export interface ReplayIndexState {
  entries: RecordingEntryView[];
  index: number;
  filter: string;
  limit: number;
  total: number;
  status: ViewModelStatus;
}

export interface AppBootstrap {
  page_kind: AppPageKind;
  title: string;
  app_path: string;
  assets_path: string;
  session_id?: string;
  surface?: SessionSurface;
  share_role?: "viewer" | "operator";
  /** SECURITY: ephemeral credential — do not log, cache, or persist. Expires with tunnel TTL. */
  share_token?: string;
}

export interface ConnectionProfile {
  profile_id: string;
  owner: string;
  name: string;
  connector_type: string;
  host: string | null;
  port: number | null;
  username: string | null;
  tags: string[];
  input_mode: string;
  recording_enabled: boolean;
  visibility: string;
  created_at: number;
  updated_at: number;
}

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

export interface HttpExchangeEntry {
  id: string;
  request: HttpRequestEntry;
  response: HttpResponseEntry | null;
  intercepted: boolean;
  interceptResolved: boolean;
  interceptAction: string | null;
}

export interface HttpActionMessage {
  type: "http_action";
  id: string;
  action: "forward" | "drop" | "modify";
  headers?: Record<string, string>;
  body_b64?: string;
}

export interface HttpInterceptToggle {
  type: "http_intercept_toggle";
  enabled: boolean;
}

export interface HttpInspectToggle {
  type: "http_inspect_toggle";
  enabled: boolean;
}

export interface HttpInterceptState {
  type: "http_intercept_state";
  enabled: boolean;
  inspect_enabled: boolean;
  timeout_s: number;
  timeout_action: string;
}
