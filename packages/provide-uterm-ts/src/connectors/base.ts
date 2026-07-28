//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The connector abstraction a hosted session runs on.
 *
 * Port of the Python module `provide.uterm.server.connectors.base`.
 */

/** A worker-protocol message. */
export type WorkerMessage = Record<string, unknown>;

/** The upstream a hosted session is attached to. */
export interface SessionConnector {
  /** Start the upstream session. */
  start(): Promise<void>;
  /** Stop it. */
  stop(): Promise<void>;
  /** Whether it is live. */
  isConnected(): boolean;
  /** Any messages the upstream produced on its own. */
  pollMessages(): Promise<WorkerMessage[]>;
  /** Process user input. */
  handleInput(data: string): Promise<WorkerMessage[]>;
  /** Process a control action. */
  handleControl(action: string): Promise<WorkerMessage[]>;
  /** The current screen, as a snapshot message. */
  getSnapshot(): Promise<WorkerMessage>;
  /** A human-readable description of the session's state. */
  getAnalysis(): Promise<string>;
  /** Change the input mode. */
  setMode(mode: string): Promise<WorkerMessage[]>;
  /** Reset the visible state. */
  clear(): Promise<WorkerMessage[]>;
}
