//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The port's single entry point for logging.
 *
 * Logging goes through `provide.telemetry`, never through OpenTelemetry
 * directly — the rule the Python reference and the Go port both follow.
 *
 * Layering follows the Go port, where library packages accept an injectable
 * logger and only transports and above call `ptel.GetLogger`. Library modules
 * here take a {@link Logger} defaulting to {@link noopLogger}, so a module
 * stays free of global state and a test can capture its output by passing a
 * double.
 *
 * {@link Logger} is declared structurally rather than imported, and matches
 * the interface `@provide-io/telemetry` exports. That is deliberate: it keeps
 * every library module decoupled from the concrete implementation, and it
 * works today. The concrete `getLogger` is **not** re-exported yet because
 * `@provide-io/telemetry@0.5.2` cannot be loaded by Node — see the "Telemetry:
 * fixed upstream" section of `docs/typescript-port-roadmap.md` for the
 * reproduction and the upstream fix. When that lands, this module re-exports
 * the real `getLogger` and swaps this interface for the imported one, which
 * is a type-level no-op for every caller.
 */

/**
 * Structured logger.
 *
 * Each call takes a field object first and an optional message, matching
 * pino's signature and the Python reference's structured-logging style.
 */
export interface Logger {
  trace(obj: Record<string, unknown>, msg?: string): void;
  debug(obj: Record<string, unknown>, msg?: string): void;
  info(obj: Record<string, unknown>, msg?: string): void;
  warn(obj: Record<string, unknown>, msg?: string): void;
  error(obj: Record<string, unknown>, msg?: string): void;
  /** Create a child logger with additional bound fields. */
  child(bindings: Record<string, unknown>): Logger;
}

/**
 * A logger that discards everything.
 *
 * The default for library modules that accept an injectable logger. It
 * returns itself from `child`, so binding fields on a hot path costs
 * nothing.
 */
export const noopLogger: Logger = {
  trace: () => {},
  debug: () => {},
  info: () => {},
  warn: () => {},
  error: () => {},
  child: () => noopLogger,
};
