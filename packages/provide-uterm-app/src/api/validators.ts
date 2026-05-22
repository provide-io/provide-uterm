//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { HttpRequestEntry, HttpResponseEntry } from "./types";

export class ValidationError extends Error {
  constructor(public readonly path: string, public readonly reason: string) {
    super(`validation failed at ${path}: ${reason}`);
    this.name = "ValidationError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireString(obj: Record<string, unknown>, key: string, path: string): string {
  const v = obj[key];
  if (typeof v !== "string") throw new ValidationError(`${path}.${key}`, `expected string, got ${typeof v}`);
  return v;
}

function requireBoolean(obj: Record<string, unknown>, key: string, path: string): boolean {
  const v = obj[key];
  if (typeof v !== "boolean") throw new ValidationError(`${path}.${key}`, `expected boolean, got ${typeof v}`);
  return v;
}

function requireNumber(obj: Record<string, unknown>, key: string, path: string): number {
  const v = obj[key];
  if (typeof v !== "number" || Number.isNaN(v)) {
    throw new ValidationError(`${path}.${key}`, `expected number, got ${typeof v}`);
  }
  return v;
}

function requireStringArray(obj: Record<string, unknown>, key: string, path: string): string[] {
  const v = obj[key];
  if (!Array.isArray(v)) throw new ValidationError(`${path}.${key}`, "expected array");
  for (let i = 0; i < v.length; i += 1) {
    if (typeof v[i] !== "string") throw new ValidationError(`${path}.${key}[${i}]`, "expected string");
  }
  return v as string[];
}

function nullableString(obj: Record<string, unknown>, key: string, path: string): string | null {
  const v = obj[key];
  if (v === null || v === undefined) return null;
  if (typeof v !== "string") throw new ValidationError(`${path}.${key}`, `expected string|null, got ${typeof v}`);
  return v;
}

function requireStringRecord(obj: Record<string, unknown>, key: string, path: string): Record<string, string> {
  const v = obj[key];
  if (!isRecord(v)) throw new ValidationError(`${path}.${key}`, "expected object");
  const out: Record<string, string> = {};
  for (const [k, val] of Object.entries(v)) {
    if (typeof val !== "string") throw new ValidationError(`${path}.${key}.${k}`, "expected string");
    out[k] = val;
  }
  return out;
}

export interface RawSessionStatus {
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

export function parseRawSessionStatus(value: unknown, path = "session"): RawSessionStatus {
  if (!isRecord(value)) throw new ValidationError(path, "expected object");
  return {
    session_id: requireString(value, "session_id", path),
    display_name: requireString(value, "display_name", path),
    connector_type: requireString(value, "connector_type", path),
    lifecycle_state: requireString(value, "lifecycle_state", path),
    input_mode: requireString(value, "input_mode", path),
    connected: requireBoolean(value, "connected", path),
    auto_start: requireBoolean(value, "auto_start", path),
    tags: requireStringArray(value, "tags", path),
    recording_enabled: requireBoolean(value, "recording_enabled", path),
    recording_available: requireBoolean(value, "recording_available", path),
    owner: nullableString(value, "owner", path),
    // visibility may be omitted; normalizer handles default
    visibility: typeof value.visibility === "string" ? value.visibility : "public",
    last_error: nullableString(value, "last_error", path),
  };
}

export function parseRawSessionStatusList(value: unknown, path = "sessions"): RawSessionStatus[] {
  if (!Array.isArray(value)) throw new ValidationError(path, "expected array");
  return value.map((item, i) => parseRawSessionStatus(item, `${path}[${i}]`));
}

export interface RawRecordingEntry {
  ts?: number;
  event?: string;
  data?: Record<string, unknown>;
}

export function parseRawRecordingEntries(value: unknown, path = "entries"): RawRecordingEntry[] {
  if (!Array.isArray(value)) throw new ValidationError(path, "expected array");
  return value.map((item, i) => {
    const p = `${path}[${i}]`;
    if (!isRecord(item)) throw new ValidationError(p, "expected object");
    const out: RawRecordingEntry = {};
    if (item.ts !== undefined) {
      if (typeof item.ts !== "number") throw new ValidationError(`${p}.ts`, "expected number");
      out.ts = item.ts;
    }
    if (item.event !== undefined) {
      if (typeof item.event !== "string") throw new ValidationError(`${p}.event`, "expected string");
      out.event = item.event;
    }
    if (item.data !== undefined) {
      if (!isRecord(item.data)) throw new ValidationError(`${p}.data`, "expected object");
      out.data = item.data;
    }
    return out;
  });
}

export function parseHttpRequestEntry(value: unknown, path = "frame"): HttpRequestEntry {
  if (!isRecord(value)) throw new ValidationError(path, "expected object");
  if (value.type !== "http_req") throw new ValidationError(`${path}.type`, `expected "http_req"`);
  const out: HttpRequestEntry = {
    type: "http_req",
    id: requireString(value, "id", path),
    ts: requireNumber(value, "ts", path),
    method: requireString(value, "method", path),
    url: requireString(value, "url", path),
    headers: requireStringRecord(value, "headers", path),
    body_size: requireNumber(value, "body_size", path),
  };
  if (typeof value.body_b64 === "string") out.body_b64 = value.body_b64;
  if (typeof value.body_truncated === "boolean") out.body_truncated = value.body_truncated;
  if (typeof value.body_binary === "boolean") out.body_binary = value.body_binary;
  if (typeof value.intercepted === "boolean") out.intercepted = value.intercepted;
  return out;
}

export function parseHttpResponseEntry(value: unknown, path = "frame"): HttpResponseEntry {
  if (!isRecord(value)) throw new ValidationError(path, "expected object");
  if (value.type !== "http_res") throw new ValidationError(`${path}.type`, `expected "http_res"`);
  const out: HttpResponseEntry = {
    type: "http_res",
    id: requireString(value, "id", path),
    ts: requireNumber(value, "ts", path),
    status: requireNumber(value, "status", path),
    status_text: requireString(value, "status_text", path),
    headers: requireStringRecord(value, "headers", path),
    body_size: requireNumber(value, "body_size", path),
    duration_ms: requireNumber(value, "duration_ms", path),
  };
  if (typeof value.body_b64 === "string") out.body_b64 = value.body_b64;
  if (typeof value.body_truncated === "boolean") out.body_truncated = value.body_truncated;
  if (typeof value.body_binary === "boolean") out.body_binary = value.body_binary;
  return out;
}
