//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * CPython's base64url decoding, as every JWT path in the reference sees it.
 *
 * `jwt.utils.base64url_decode` pads from the *original* length and then calls
 * `base64.urlsafe_b64decode`, which discards anything outside the alphabet
 * rather than refusing it. That combination is not what any strict decoder
 * does, and the difference is observable: a header of `!!!` decodes to nothing
 * and reaches the JSON parser as empty, while one of `YWJ!jZA` fails outright,
 * because discarding shifts padding arithmetic that was computed before it.
 *
 * A strict decoder would report the *wrong failure* for a malformed token —
 * "invalid header padding" where the reference says "invalid header string",
 * or the reverse — and those are the two branches a server's refusal of a
 * forged credential runs down.
 *
 * Lives in `pycompat` because both JWT paths in this port need it: the
 * Worker's own verifier and the server's `dev_token`/`jwt` mode.
 */

/** The standard base64 alphabet, after the url alphabet is translated into it. */
const BASE64_ALPHABET = /[A-Za-z0-9+/]/;

/**
 * Decode one base64url segment the way CPython does.
 *
 * @throws {Error} When the padding cannot be made to work out, which is what
 *   `binascii.Error` is in the reference.
 */
export function pyB64UrlDecode(text: string): Uint8Array {
  const padding = 4 - (text.length % 4);
  const padded = padding === 4 ? text : text + "=".repeat(padding);

  // Everything outside the alphabet is dropped first; only the pads and the
  // data survive to be counted.
  const kept = [...padded.replaceAll("-", "+").replaceAll("_", "/")].filter(
    (character) => character === "=" || BASE64_ALPHABET.test(character),
  );

  let data = "";
  for (const [index, character] of kept.entries()) {
    if (character !== "=") {
      data += character;
      continue;
    }
    const group = data.length % 4;
    // A pad arriving on a whole group is stray and simply skipped — which is
    // why `YWJj=YWJj` decodes both groups and `====` decodes nothing.
    if (group === 0) {
      continue;
    }
    // A single character carries no whole byte, however much padding follows.
    if (group === 1) {
      throw new Error(`invalid base64: ${text}`);
    }
    // Otherwise the padding has to complete the group exactly, and the data
    // ends here: `YWJjZA==` decodes and `YWJjZA=` does not.
    if (kept.slice(index).filter((pad) => pad === "=").length < 4 - group) {
      throw new Error(`invalid base64: ${text}`);
    }
    return Uint8Array.from(Buffer.from(data, "base64"));
  }

  const remainder = data.length % 4;
  // Nothing padded the last group at all.
  if (remainder !== 0) {
    throw new Error(`invalid base64: ${text}`);
  }
  return Uint8Array.from(Buffer.from(data, "base64"));
}
