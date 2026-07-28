//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Signing helpers for governance webhook payloads.
 *
 * Port of the Python module `provide.uterm.server.webhook_signing`.
 *
 * Signed over `"{timestamp}.{body}"`, so a captured request cannot be
 * replayed once the window has passed — and verified fail-closed when there
 * is no signing secret, because an empty key HMACs to something anyone who
 * knows the body and the timestamp can forge.
 */

import { createHmac } from "node:crypto";
import { digestsMatch } from "./digests.ts";

/** How far a timestamp may be from now, in either direction. */
export const WEBHOOK_MAX_AGE_S = 300.0;

/** The `sha256=<hex>` signature over `timestamp` and `body`. */
export function buildWebhookSignature(secret: string, body: Uint8Array, timestamp: string): string {
  // The separator matters: concatenating the two would let a body that
  // starts with digits masquerade as a different timestamp.
  const signed = Buffer.concat([Buffer.from(`${timestamp}.`, "utf8"), Buffer.from(body)]);
  return `sha256=${createHmac("sha256", Buffer.from(secret, "utf8")).update(signed).digest("hex")}`;
}

/**
 * Whether a webhook request carries a fresh, valid signature.
 *
 * @returns `false` for every failure. There is no partial success here: a
 *   request either proved it came from the holder of the secret or it did not.
 */
export function verifyWebhookSignature(
  secret: string | undefined,
  body: Uint8Array,
  signatureHeader: string | undefined,
  timestampHeader: string | undefined,
  options: { maxAgeS?: number; now?: number } = {},
): boolean {
  // Before anything else: a signature cannot be authenticated without a
  // shared secret, and HMAC with an empty key is forgeable by anyone. This
  // has to hold however the function is reached, including from a directly
  // constructed provider that never saw the config validator.
  if ((secret ?? "").trim() === "") {
    return false;
  }
  if (
    signatureHeader === undefined ||
    signatureHeader === "" ||
    timestampHeader === undefined ||
    timestampHeader === ""
  ) {
    return false;
  }
  // Trimmed first: an empty or whitespace header parses as zero, which would
  // be a timestamp from 1970 and so outside every window — but relying on
  // that is relying on an accident.
  const trimmedTimestamp = timestampHeader.trim();
  const timestamp = Number(trimmedTimestamp);
  if (trimmedTimestamp === "" || !Number.isFinite(timestamp)) {
    return false;
  }
  const now = options.now ?? Date.now() / 1000;
  const maxAge = options.maxAgeS ?? WEBHOOK_MAX_AGE_S;
  // Both directions: a clock ahead of the sender is as much of a replay
  // window as one behind.
  if (Math.abs(now - timestamp) > maxAge) {
    return false;
  }
  let supplied = signatureHeader.trim();
  if (supplied.toLowerCase().startsWith("sha256=")) {
    supplied = supplied.slice(supplied.indexOf("=") + 1).trim();
  }
  // Not load-bearing on its own — an empty string is a different length
  // from a digest, so the comparison refuses it either way — but a bare
  // prefix is a malformed header, not a wrong signature, and saying so here
  // does not depend on that.
  if (supplied === "") {
    return false;
  }
  const expected = buildWebhookSignature(secret as string, body, timestampHeader).split("=")[1] as string;
  return digestsMatch(supplied, expected);
}
