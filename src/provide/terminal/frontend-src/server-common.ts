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
  recording_path: string | null;
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
}

export interface ProvideHijackConstructor {
  new (container: HTMLElement, config: ProvideHijackConfig): unknown;
}

declare global {
  interface Window {
    ProvideHijack?: ProvideHijackConstructor;
  }
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
  const response = await fetch(path, init);
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
