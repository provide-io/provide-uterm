//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * `close_cases` from `spec/behavior_vectors.json`: the client-side transport
 * close contract Python, TypeScript, Go and C# are all tested against.
 *
 * The file is generated from the Python reference by
 * `scripts/generate_behavior_vectors.py`; a divergence here is a divergence
 * between implementations, not a stale local fixture.
 */

import type { CloseInitiator } from "../transports/index.ts";
import { loadSpec } from "./golden.ts";

/** The close a vector expects: who ended it, and the protocol's code and reason. */
export interface ExpectedClose {
  initiator: CloseInitiator;
  code: number | null;
  reason: string;
}

/** One `summary()` rendering. */
export interface SummaryCase extends ExpectedClose {
  detail: string;
  summary: string;
}

/** A WebSocket close frame as the vectors spell it. */
export interface CloseFrameVector {
  code: number;
  reason: string;
}

/** Which close frames were exchanged, and the close they attribute. */
export interface WebSocketCase extends ExpectedClose {
  name: string;
  received: CloseFrameVector | null;
  sent: CloseFrameVector | null;
  /** Null unless both frames were exchanged: only two frames have an order. */
  received_then_sent: boolean | null;
  /** The reference's description of the frames (websockets' `ConnectionClosed`). */
  detail: string;
}

/** A socket event on a stream transport, and the close it must report. */
export interface EventCase extends ExpectedClose {
  name: string;
  event: string;
  operation: "receive" | "send";
}

export interface CloseCases {
  summary: SummaryCase[];
  websocket: WebSocketCase[];
  telnet: EventCase[];
  chaos: EventCase[];
}

/** The shared close vectors. */
export function loadCloseCases(): CloseCases {
  return loadSpec<{ close_cases: CloseCases }>("behavior_vectors.json").close_cases;
}

/** A vector's code in this port's spelling: absent is `undefined`, not `null`. */
export function vectorCode(code: number | null): number | undefined {
  return code ?? undefined;
}
