//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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

/** Legacy no-op share-token slot. Tunnel auth is cookie-only. */
let _shareToken: string | null = null;

/** Store the share token for compatibility with older page bootstraps. */
export function setShareToken(token: string | null | undefined): void {
  _shareToken = typeof token === "string" && token.length > 0 ? token : null;
}

/** Return the current share token, or null if in cookie mode. */
export function getShareToken(): string | null {
  return _shareToken;
}

/** Return *path* unchanged; tunnel credentials are carried by HttpOnly cookies. */
export function withShareToken(path: string): string {
  return path;
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
