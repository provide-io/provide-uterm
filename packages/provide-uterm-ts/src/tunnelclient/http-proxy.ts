//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What an operator is shown of the traffic crossing a tunnel.
 *
 * Port of `provide.uterm.tunnel.http_proxy`. How much of a body travels is a
 * decision with two sides: something binary is reported by size alone — a
 * megabyte of base64 nobody can read is a megabyte spent — and something too
 * large is marked truncated rather than carried. Both still say the size,
 * which is the part an operator is actually looking at.
 *
 * The content type decides, read as the type alone: `text/html; charset=utf-8`
 * is the same type as `text/html`, and a type shouted in capitals is the same
 * type again.
 */

import { pyRoundTo } from "../pycompat/rounding.ts";

/** The most body that travels in full. */
export const BODY_MAX_BYTES = 256 * 1024;

/** Types whose bodies are reported rather than carried. */
export const BINARY_CONTENT_TYPES: readonly string[] = [
  "application/gzip",
  "application/octet-stream",
  "application/pdf",
  "application/wasm",
  "application/zip",
  "audio/",
  "font/",
  "image/",
  "video/",
];

/** How a body was reported. */
export interface EncodedBody {
  body_size: number;
  /** Present when the body was reported by size rather than carried. */
  body_binary?: true;
  /** Present when the body was too large to carry. */
  body_truncated?: true;
  /** The body itself, when it was worth carrying. */
  body_b64?: string;
}

/** Whether a content type names something nobody would read as text. */
function isBinary(contentType: string): boolean {
  // The type alone: a charset is not part of what this decides on.
  //
  // No test can show that this matters while the match is by prefix —
  // parameters come after the type, so they cannot change whether it starts
  // with one. It is here because the reference does it, and because a match
  // that ever became exact would need it.
  const semicolon = contentType.indexOf(";");
  const type = (semicolon === -1 ? contentType : contentType.slice(0, semicolon)).toLowerCase().trim();
  return BINARY_CONTENT_TYPES.some((prefix) => type.startsWith(prefix));
}

/**
 * Report a body, carrying it only when it is worth reading.
 *
 * The size is always given, since that is what an operator scans for.
 */
export function encodeBody(body: Uint8Array, contentType: string): EncodedBody {
  const result: EncodedBody = { body_size: body.length };
  if (body.length === 0) {
    // Nothing to carry and nothing to say about it — checked before the type,
    // so an empty binary body is not marked binary.
    return result;
  }
  if (isBinary(contentType)) {
    result.body_binary = true;
    return result;
  }
  if (body.length > BODY_MAX_BYTES) {
    result.body_truncated = true;
    return result;
  }
  result.body_b64 = Buffer.from(body).toString("base64");
  return result;
}

/** A size somebody can read at a glance. */
function humanSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes}B`;
  }
  if (bytes < 1024 * 1024) {
    return `${pyRoundTo(bytes / 1024, 1).toFixed(1)}KB`;
  }
  return `${pyRoundTo(bytes / (1024 * 1024), 1).toFixed(1)}MB`;
}

/**
 * One line describing an exchange.
 *
 * A request going out and an answer coming back read differently on purpose:
 * the arrow says which it is without anybody having to parse the rest. A
 * failure at the far end is marked, because that is the line somebody is
 * looking for.
 */
export function formatLogLine(
  method: string,
  url: string,
  status: number | undefined,
  durationMs: number | undefined,
  bodySize: number,
): string {
  const size = humanSize(bodySize);
  if (status === undefined) {
    return `→ ${method} ${url} (${size})`;
  }
  const warn = status >= 500 ? " ⚠" : "";
  // Rounded the way Python's `%.0f` rounds — to even, so 12.5 and 11.5 are
  // both 12. A duration is not worth a divergence, but a line that differs
  // between two runtimes is a line somebody has to reconcile.
  const duration = durationMs === undefined ? "?" : `${pyRoundTo(durationMs, 0).toFixed(0)}ms`;
  return `← ${status} ${method} ${url} (${duration}, ${size})${warn}`;
}
