//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

export type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

export interface SessionStatus {
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

export interface SnapshotPrompt {
  prompt_id?: string;
}

export interface SnapshotPayload {
  prompt_detected?: SnapshotPrompt | null;
}

export interface AnalysisResponse {
  session_id: string;
  analysis: string;
}

export interface RecordingEntry {
  ts?: number;
  event?: string;
  data?: Record<string, unknown>;
}

export interface ProvideHijackConfig {
  workerId: string;
  showAnalysis?: boolean;
  mobileKeys?: boolean;
  authToken?: string;
}

export interface ProvideHijackConstructor {
  new (container: HTMLElement, config: ProvideHijackConfig): unknown;
}

declare global {
  interface Window {
    ProvideHijack?: ProvideHijackConstructor;
  }
}

/**
 * Module-level share token for tunnel session authentication.
 * Set from bootstrap JSON on page load; null when using cookie-based auth.
 * SECURITY: ephemeral credential — do not log or persist to storage.
 */
let _shareToken: string | null = null;

/** Store the share token for subsequent API/WS calls. Pass null for cookie mode. */
export function setShareToken(token: string | null | undefined): void {
  _shareToken = typeof token === "string" && token.length > 0 ? token : null;
}

/** Return the current share token, or null if in cookie mode. */
export function getShareToken(): string | null {
  return _shareToken;
}

/** Append the share token as a query parameter if set (query transport mode). No-op in cookie mode. */
export function withShareToken(path: string): string {
  if (_shareToken === null) return path;
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}token=${encodeURIComponent(_shareToken)}`;
}

export async function apiJson<T>(path: string, method: HttpMethod = "GET", body: unknown = null): Promise<T> {
  const init: RequestInit = {
    method,
    headers: {
      "Content-Type": "application/json",
    },
  };
  if (body !== null) {
    init.body = JSON.stringify(body);
  }
  const response = await fetch(withShareToken(path), init);
  if (!response.ok) {
    throw new Error(String(response.status));
  }
  return (await response.json()) as T;
}

export function requireElement<T extends Element>(selector: string, root: ParentNode = document): T {
  const element = root.querySelector<T>(selector);
  if (element === null) {
    throw new Error(`Missing required element: ${selector}`);
  }
  return element;
}

export function readDataset(element: HTMLElement, name: string): string {
  const value = element.dataset[name];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`Missing required data attribute: ${name}`);
  }
  return value;
}

export function readBooleanDataset(element: HTMLElement, name: string): boolean {
  return readDataset(element, name) === "true";
}
