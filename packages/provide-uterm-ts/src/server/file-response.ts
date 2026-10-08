//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Serving a file the way the reference's framework serves one.
 *
 * Port of Starlette's `FileResponse`, which the reference's recording
 * download returns: `accept-ranges`, an attachment `content-disposition`, the
 * `content-length`, `last-modified` and `etag` validators derived from the
 * file's status, and byte ranges — one range as a 206 with `content-range`,
 * several as `multipart/byteranges`, an unsatisfiable one as a 416, and a
 * malformed one as a 400 naming what was wrong. `If-Range` decides whether a
 * range is honoured at all.
 *
 * The validators matter beyond politeness: a client resuming a download sends
 * back the `etag` it was given, and one this port computed differently would
 * never match a file the reference served, or the other way round. So the
 * `etag` is the reference's: the MD5 of `"{st_mtime}-{st_size}"` with the
 * modification time rendered as CPython renders the float.
 */

import { createHash, randomBytes } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { floatRepr } from "../pycompat/index.ts";

/** The most ranges one request may name before the header is ignored. */
export const MAX_RANGES = 100;

/** A `[start, end)` byte range. */
type Range = [number, number];

/** A `Range` header that cannot be served, and how it is refused. */
class RangeRefusal {
  readonly response: Response;
  constructor(response: Response) {
    this.response = response;
  }
}

/** A plain-text answer, as Starlette's `PlainTextResponse` makes one. */
function plainText(body: string, status: number, headers: Record<string, string> = {}): Response {
  return new Response(body, {
    status,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "content-length": String(Buffer.byteLength(body)),
      ...headers,
    },
  });
}

/** A malformed `Range` header, refused with what was wrong. */
function malformed(detail = "Malformed range header."): RangeRefusal {
  return new RangeRefusal(plainText(detail, 400));
}

/**
 * The modification time as CPython's `st_mtime` holds it.
 *
 * CPython builds the float as `sec + nsec * 1e-9` from the nanosecond stamp,
 * which is not always the double nearest to the nanosecond value; doing the
 * same arithmetic here is what makes the two renderings, and so the two
 * `etag`s, agree.
 */
export function pyMtime(mtimeNs: bigint): number {
  const seconds = mtimeNs / 1_000_000_000n;
  const nanoseconds = mtimeNs % 1_000_000_000n;
  return Number(seconds) + Number(nanoseconds) * 1e-9;
}

/**
 * Python's `int()` on a string: whitespace, a sign, digits grouped by single
 * underscores. Anything else is a `ValueError`, here `undefined`.
 */
function pyInt(text: string): number | undefined {
  const match = /^\s*([+-]?)(\d(?:_?\d)*)\s*$/.exec(text);
  if (match === null) {
    return undefined;
  }
  const value = Number((match[2] as string).replaceAll("_", ""));
  return match[1] === "-" ? -value : value;
}

/** Starlette's `_parse_ranges`: every well-formed part, the rest skipped. */
function parseRanges(spec: string, size: number): Range[] {
  const ranges: Range[] = [];
  for (const raw of spec.split(",")) {
    // Stripped as a whole, which is what lets a suffix written ` -5` count.
    // The halves need no stripping of their own: `pyInt` allows the space a
    // half can still carry, and an empty half can only come from the dash
    // sitting at an end of the stripped part. An empty part and a lone `-`
    // need no case either: the first has no dash, and the second's halves
    // are both empty, so it is skipped below.
    const part = raw.trim();
    const dash = part.indexOf("-");
    if (dash === -1) {
      continue;
    }
    const startText = part.slice(0, dash);
    const endText = part.slice(dash + 1);
    const endValue = pyInt(endText);
    if (startText === "") {
      if (endValue === undefined) {
        continue;
      }
      ranges.push([Math.max(size - endValue, 0), size]);
      continue;
    }
    const start = pyInt(startText);
    if (start === undefined || (endText !== "" && endValue === undefined)) {
      continue;
    }
    // An end past the file is the file's end, as Starlette clamps it.
    ranges.push([start, endValue === undefined ? size : Math.min(endValue + 1, size)]);
  }
  return ranges;
}

/**
 * Starlette's `_parse_range_header`: the ranges to serve, in order and merged.
 *
 * @throws {RangeRefusal} For a header that is malformed or unsatisfiable.
 */
function parseRangeHeader(header: string, size: number): Range[] {
  const equals = header.indexOf("=");
  if (equals === -1) {
    throw malformed();
  }
  if (header.slice(0, equals).trim().toLowerCase() !== "bytes") {
    throw malformed("Only support bytes range");
  }
  const spec = header.slice(equals + 1);
  if (spec.split(",").length > MAX_RANGES) {
    return [];
  }
  const ranges = parseRanges(spec, size);
  if (ranges.length === 0) {
    throw malformed("Range header: range must be requested");
  }
  // Starlette asks `0 <= start < file_size`. No start here can be negative —
  // a written start cannot carry a minus sign past the dash it is split on,
  // and a suffix's start is clamped at zero — so only the upper bound can fail.
  if (ranges.some(([start]) => start >= size)) {
    throw new RangeRefusal(plainText("", 416, { "content-range": `bytes */${size}` }));
  }
  if (ranges.some(([start, end]) => start >= end)) {
    throw malformed("Range header: start must be less than end");
  }
  // Starlette returns a single range as it is and merges several. Merging one
  // range is that range, so both go through the merge. Sorted by start alone:
  // where two starts tie, the merge keeps the larger end whichever comes
  // first, so the tuple order Starlette sorts by changes nothing.
  ranges.sort((a, b) => a[0] - b[0]);
  const merged: Range[] = [];
  for (const [start, end] of ranges) {
    const last = merged[merged.length - 1];
    if (merged.length > 0 && start <= (last as Range)[1]) {
      (last as Range)[1] = Math.max((last as Range)[1], end);
    } else {
      merged.push([start, end]);
    }
  }
  return merged;
}

/** How a file is served. */
export interface FileResponseOptions {
  /** The name a client saves it under. */
  filename: string;
  /** Its media type. */
  mediaType: string;
  /** The request's headers, for `Range` and `If-Range`. */
  requestHeaders: Headers;
  /** The multipart boundary. Random, as Starlette's is, unless a test pins it. */
  boundary?: (() => string) | undefined;
}

/** Serve the file at `path`. It must exist and be a regular file. */
export function fileResponse(path: string, options: FileResponseOptions): Response {
  const stat = statSync(path, { bigint: true });
  const size = Number(stat.size);
  const mtime = pyMtime(stat.mtimeNs);
  const encoded = encodeURIComponent(options.filename);
  const headers: Record<string, string> = {
    "content-type": options.mediaType,
    "accept-ranges": "bytes",
    // Starlette quotes with `urllib.parse.quote`, whose safe set differs from
    // `encodeURIComponent` only in characters a route's session id cannot hold.
    "content-disposition":
      encoded === options.filename
        ? `attachment; filename="${options.filename}"`
        : `attachment; filename*=utf-8''${encoded}`,
    "content-length": String(size),
    // `formatdate(st_mtime, usegmt=True)` drops the fraction.
    "last-modified": new Date(Math.floor(mtime) * 1000).toUTCString(),
    etag: `"${createHash("md5")
      .update(`${floatRepr(mtime)}-${size}`)
      .digest("hex")}"`,
  };
  const body = readFileSync(path);

  const range = options.requestHeaders.get("range");
  const ifRange = options.requestHeaders.get("if-range");
  if (range === null || (ifRange !== null && ifRange !== headers["last-modified"] && ifRange !== headers.etag)) {
    return new Response(body, { status: 200, headers });
  }

  let ranges: Range[];
  try {
    ranges = parseRangeHeader(range, size);
  } catch (refusal) {
    return (refusal as RangeRefusal).response;
  }
  if (ranges.length === 0) {
    return new Response(body, { status: 200, headers });
  }
  if (ranges.length === 1) {
    const [start, end] = ranges[0] as Range;
    return new Response(body.subarray(start, end), {
      status: 206,
      headers: {
        ...headers,
        "content-range": `bytes ${start}-${end - 1}/${size}`,
        "content-length": String(end - start),
      },
    });
  }

  const boundary = (options.boundary ?? (() => randomBytes(13).toString("hex")))();
  const parts: Buffer[] = [];
  for (const [start, end] of ranges) {
    parts.push(
      Buffer.from(
        `--${boundary}\r\nContent-Type: ${options.mediaType}\r\nContent-Range: bytes ${start}-${end - 1}/${size}\r\n\r\n`,
      ),
      body.subarray(start, end),
      Buffer.from("\r\n"),
    );
  }
  // Starlette encodes these as latin-1. Every character in them is ASCII — a
  // hex boundary, digits, and this server's own media type — so the default
  // encoding writes the same bytes.
  parts.push(Buffer.from(`--${boundary}--`));
  const multipart = Buffer.concat(parts);
  return new Response(multipart, {
    status: 206,
    headers: {
      ...headers,
      "content-type": `multipart/byteranges; boundary=${boundary}`,
      "content-length": String(multipart.length),
    },
  });
}
