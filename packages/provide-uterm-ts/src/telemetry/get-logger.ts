//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Structured logging for the layers above the library modules.
 *
 * ## Why this exists rather than a re-export
 *
 * The port is meant to log through `provide.telemetry`, as the Python
 * reference and the Go port do, and `@provide-io/telemetry` is the right
 * dependency. It cannot be loaded from Node as published — see the "Blocked:
 * telemetry" section of `docs/typescript-port-roadmap.md` for the
 * reproduction and the upstream fix — so this is a deliberate stand-in, not a
 * decision to roll our own.
 *
 * It is scoped to be easy to delete. The emitted shape is the structured
 * record that package produces (a name, a level, a field object, an optional
 * message), and {@link Logger} is the interface it exports, so when the
 * dependency becomes usable this module becomes a re-export and no caller
 * changes. OpenTelemetry is not imported here and must not be: it is an
 * optional peer of that package, and importing it directly would be an
 * undeclared dependency that merely happens to resolve in a dev tree.
 *
 * What is deliberately absent, because faking it would be worse than not
 * having it: trace and span correlation, sampling, PII redaction, and OTLP
 * export. Those are the reasons to depend on the real package rather than
 * treat this as sufficient.
 */

import type { Logger } from "./logger.ts";

/** One emitted log record. */
export interface LogRecord {
  /** Emission time in seconds. */
  ts: number;
  /** The logger's name, as passed to {@link getLogger}. */
  name: string;
  /** Severity: `trace`, `debug`, `info`, `warn` or `error`. */
  level: string;
  /** The optional human-readable message. */
  msg?: string;
  /** Bound and call-site fields, merged with the call site winning. */
  fields: Record<string, unknown>;
}

/** Receives every emitted record. */
export type LogSink = (record: LogRecord) => void;

/** Write a record as one JSON line on stderr. */
function defaultSink(record: LogRecord): void {
  process.stderr.write(`${JSON.stringify(record)}\n`);
}

let sink: LogSink = defaultSink;

/**
 * Replace the log sink.
 *
 * @returns A function restoring the previous sink, so a caller can nest
 *   replacements without tracking what was there before.
 */
export function setLogSink(next: LogSink): () => void {
  const previous = sink;
  sink = next;
  return () => {
    sink = previous;
  };
}

/** Build a logger emitting under `name` with `bound` fields attached. */
function makeLogger(name: string, bound: Record<string, unknown>): Logger {
  const emit = (level: string, fields: Record<string, unknown>, msg?: string): void => {
    // The call site wins over a bound field, so a child's context can be
    // overridden where it matters rather than silently shadowing it.
    const record: LogRecord = {
      ts: Date.now() / 1000,
      name,
      level,
      fields: { ...bound, ...fields },
    };
    if (msg !== undefined) {
      record.msg = msg;
    }
    sink(record);
  };

  return {
    trace: (fields, msg) => emit("trace", fields, msg),
    debug: (fields, msg) => emit("debug", fields, msg),
    info: (fields, msg) => emit("info", fields, msg),
    warn: (fields, msg) => emit("warn", fields, msg),
    error: (fields, msg) => emit("error", fields, msg),
    // A child copies its bindings, so it cannot mutate the parent's.
    child: (bindings) => makeLogger(name, { ...bound, ...bindings }),
  };
}

/** Get a logger emitting under `name`. */
export function getLogger(name: string): Logger {
  return makeLogger(name, {});
}
