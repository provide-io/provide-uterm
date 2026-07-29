//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The ten hijack and control tools a model can call.
 *
 * Port of `provide.uterm.ai.server_tools_hijack`. Six drive a hijack lease
 * and four ask the server or a worker to change state. Each body is short,
 * and all of the substance is in the order of what it does:
 *
 * * **Validate before you reach.** Every id is checked before the client is
 *   touched, so a caller-supplied path segment never reaches a request path.
 *   Where a tool takes two ids, whichever is bad first is the one reported.
 * * **A pattern is checked as an id is.** `hijack_send` takes a regex the
 *   caller wrote and refuses it here rather than forwarding it to a server
 *   that would also have to compile it.
 * * **Keystrokes are capped and sanitised** on the way through: what a model
 *   sends is not what reaches a terminal until it has been through that.
 * * **One answer shape**, so a caller never has to guess at the envelope.
 *
 * Authorization sits outside these — every one is wrapped by the chokepoint
 * in `./authorization.ts` before it is registered.
 */

import { prepareKeystrokes } from "../sanitizer/index.ts";
import { rejectBadId, rejectBadIds, rejectBadPattern } from "./guards.ts";
import { MAX_KEYSTROKE_BYTES } from "./policy.ts";
import { cleanSnapshot } from "./tools.ts";

export { MAX_KEYSTROKE_BYTES };

/** What the client answers with: whether it worked, and whatever it said. */
export interface ClientAnswer {
  ok: boolean;
  data: unknown;
}

/** The hijack client these tools drive. */
export interface HijackToolClient {
  acquire(workerId: string, options: { owner: string; lease_s: number }): Promise<ClientAnswer>;
  heartbeat(workerId: string, hijackId: string, options: { lease_s: number }): Promise<ClientAnswer>;
  snapshot(workerId: string, hijackId: string, options: { wait_ms: number }): Promise<ClientAnswer>;
  events(workerId: string, hijackId: string, options: { after_seq: number; limit: number }): Promise<ClientAnswer>;
  send(
    workerId: string,
    hijackId: string,
    options: {
      keys: string;
      expect_prompt_id: string | null;
      expect_regex: string | null;
      timeout_ms: number;
      poll_interval_ms: number;
    },
  ): Promise<ClientAnswer>;
  step(workerId: string, hijackId: string): Promise<ClientAnswer>;
  release(workerId: string, hijackId: string): Promise<ClientAnswer>;
  health(): Promise<ClientAnswer>;
  setSessionMode(sessionId: string, mode: string): Promise<ClientAnswer>;
  setInputMode(workerId: string, mode: string): Promise<ClientAnswer>;
  disconnectWorker(workerId: string): Promise<ClientAnswer>;
}

/** What every tool answers with. */
export type ToolResult = Record<string, unknown>;

/** The tools registered here, in the order the reference registers them. */
export const HIJACK_TOOL_NAMES: readonly string[] = [
  "hijack_begin",
  "hijack_heartbeat",
  "hijack_read",
  "hijack_send",
  "hijack_step",
  "hijack_release",
  "server_health",
  "session_set_mode",
  "worker_input_mode",
  "worker_disconnect",
];

/**
 * Fold a client answer into the one shape every tool returns.
 *
 * A mapping is spread; anything else goes under `data`, so a list can never
 * invent fields on the envelope.
 *
 * `success` is written first, which means a body carrying its own `success`
 * overrides the verdict. That reads as a mistake and it is what the reference
 * does, so it is carried over rather than quietly corrected — and pinned by a
 * test so nobody has to wonder.
 */
export function toolAnswer(ok: boolean, data: unknown): ToolResult {
  if (typeof data === "object" && data !== null && !Array.isArray(data)) {
    return { success: ok, ...(data as Record<string, unknown>) };
  }
  return { success: ok, data };
}

/**
 * Whether a snapshot is a mapping with something in it.
 *
 * Two things at once. An empty mapping is false in Python and true here, so
 * testing it as the reference does keeps a snapshot with nothing in it from
 * coming back rewritten as `{"screen": ""}`.
 *
 * And only a mapping counts: the reference reads `.get` off whatever is
 * there, so a server answering with a bare string raises inside the tool and
 * reaches the caller as an error. Left alone here instead — a malformed
 * answer passed through says more than a stack trace does.
 */
function isCleanableSnapshot(value: unknown): boolean {
  return typeof value === "object" && value !== null && Object.keys(value).length > 0;
}

/** A body field as text, or the default. */
function text(args: Record<string, unknown>, key: string, fallback = ""): string {
  const raw = args[key];
  return typeof raw === "string" ? raw : fallback;
}

/** A body field as a number, or the default. */
function count(args: Record<string, unknown>, key: string, fallback: number): number {
  const raw = args[key];
  return typeof raw === "number" ? raw : fallback;
}

/** A field that may simply be absent. */
function optional(args: Record<string, unknown>, key: string): string | null {
  const raw = args[key];
  return typeof raw === "string" ? raw : null;
}

/**
 * Call one of the tools by name.
 *
 * @throws {Error} For a name nobody registered, rather than answering as
 *   though it had run.
 */
export async function callHijackTool(
  client: HijackToolClient,
  tool: string,
  args: Record<string, unknown>,
): Promise<ToolResult> {
  switch (tool) {
    case "hijack_begin":
      return hijackBegin(client, args);
    case "hijack_heartbeat":
      return hijackHeartbeat(client, args);
    case "hijack_read":
      return hijackRead(client, args);
    case "hijack_send":
      return hijackSend(client, args);
    case "hijack_step":
      return hijackStep(client, args);
    case "hijack_release":
      return hijackRelease(client, args);
    case "server_health":
      return serverHealth(client);
    case "session_set_mode":
      return sessionSetMode(client, args);
    case "worker_input_mode":
      return workerInputMode(client, args);
    case "worker_disconnect":
      return workerDisconnect(client, args);
    default:
      throw new Error(`unknown tool: ${tool}`);
  }
}

/** Take a lease on a running worker. */
export async function hijackBegin(client: HijackToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const workerId = text(args, "worker_id");
  const rejection = rejectBadId(workerId, "worker_id");
  if (rejection !== undefined) {
    return { ...rejection };
  }
  const { ok, data } = await client.acquire(workerId, {
    owner: text(args, "owner", "operator"),
    lease_s: count(args, "lease_s", 90),
  });
  return toolAnswer(ok, data);
}

/** Extend a lease before it runs out. */
export async function hijackHeartbeat(client: HijackToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const workerId = text(args, "worker_id");
  const hijackId = text(args, "hijack_id");
  const rejection = rejectBadIds([workerId, "worker_id"], [hijackId, "hijack_id"]);
  if (rejection !== undefined) {
    return { ...rejection };
  }
  const { ok, data } = await client.heartbeat(workerId, hijackId, { lease_s: count(args, "lease_s", 90) });
  return toolAnswer(ok, data);
}

/** Read the screen, or the event log. */
export async function hijackRead(client: HijackToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const workerId = text(args, "worker_id");
  const hijackId = text(args, "hijack_id");
  const rejection = rejectBadIds([workerId, "worker_id"], [hijackId, "hijack_id"]);
  if (rejection !== undefined) {
    return { ...rejection };
  }

  const mode = text(args, "mode", "snapshot");
  // Compared against `events` alone, as the reference compares it: anything
  // else — a typo included — reads the screen rather than failing.
  const answer =
    mode === "events"
      ? await client.events(workerId, hijackId, {
          after_seq: count(args, "after_seq", 0),
          limit: count(args, "limit", 200),
        })
      : await client.snapshot(workerId, hijackId, { wait_ms: count(args, "wait_ms", 1500) });

  const result = toolAnswer(answer.ok, answer.data);
  const snapshot = result.snapshot;
  // Only a snapshot that was actually read, and only when it worked: cleaning
  // a failure's payload would hide what the server said about it, and an
  // event log is not a screen.
  //
  // By the reference's truth, not this runtime's: an empty mapping is false
  // in Python and true here, so a snapshot with nothing in it would come back
  // rewritten as `{"screen": ""}` instead of being left as the nothing it is.
  if (answer.ok && mode !== "events" && isCleanableSnapshot(snapshot)) {
    // Read once and only as a number: absent, null and anything that is not
    // a count all mean "do not trim", which is what the reference's `None`
    // means too.
    const tailLines = typeof args.tail_lines === "number" ? args.tail_lines : undefined;
    result.snapshot = cleanSnapshot(snapshot as Record<string, unknown>, text(args, "output", "text"), tailLines);
  }
  return result;
}

/** Type at a hijacked worker, optionally guarded by a prompt or a pattern. */
export async function hijackSend(client: HijackToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const workerId = text(args, "worker_id");
  const hijackId = text(args, "hijack_id");
  const badId = rejectBadIds([workerId, "worker_id"], [hijackId, "hijack_id"]);
  if (badId !== undefined) {
    return { ...badId };
  }
  // Refused here rather than forwarded: the server would have to compile it
  // too, so passing it on is handing the problem along.
  const badPattern = rejectBadPattern(optional(args, "expect_regex"));
  if (badPattern !== undefined) {
    return { ...badPattern };
  }
  const { ok, data } = await client.send(workerId, hijackId, {
    keys: prepareKeystrokes(text(args, "keys"), MAX_KEYSTROKE_BYTES),
    expect_prompt_id: optional(args, "expect_prompt_id"),
    expect_regex: optional(args, "expect_regex"),
    timeout_ms: count(args, "timeout_ms", 2000),
    poll_interval_ms: count(args, "poll_interval_ms", 120),
  });
  return toolAnswer(ok, data);
}

/** Let a hijacked worker's loop run once. */
export async function hijackStep(client: HijackToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const workerId = text(args, "worker_id");
  const hijackId = text(args, "hijack_id");
  const rejection = rejectBadIds([workerId, "worker_id"], [hijackId, "hijack_id"]);
  if (rejection !== undefined) {
    return { ...rejection };
  }
  const { ok, data } = await client.step(workerId, hijackId);
  return toolAnswer(ok, data);
}

/** Give the lease back and let automation resume. */
export async function hijackRelease(client: HijackToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const workerId = text(args, "worker_id");
  const hijackId = text(args, "hijack_id");
  const rejection = rejectBadIds([workerId, "worker_id"], [hijackId, "hijack_id"]);
  if (rejection !== undefined) {
    return { ...rejection };
  }
  const { ok, data } = await client.release(workerId, hijackId);
  return toolAnswer(ok, data);
}

/** Ask whether the server is well. */
export async function serverHealth(client: HijackToolClient): Promise<ToolResult> {
  const { ok, data } = await client.health();
  return toolAnswer(ok, data);
}

/** Set a session's input mode. */
export async function sessionSetMode(client: HijackToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const sessionId = text(args, "session_id");
  const rejection = rejectBadId(sessionId, "session_id");
  if (rejection !== undefined) {
    return { ...rejection };
  }
  const { ok, data } = await client.setSessionMode(sessionId, text(args, "mode"));
  return toolAnswer(ok, data);
}

/** Set a worker's input mode directly. */
export async function workerInputMode(client: HijackToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const workerId = text(args, "worker_id");
  const rejection = rejectBadId(workerId, "worker_id");
  if (rejection !== undefined) {
    return { ...rejection };
  }
  const { ok, data } = await client.setInputMode(workerId, text(args, "mode"));
  return toolAnswer(ok, data);
}

/** Close a worker's socket. */
export async function workerDisconnect(client: HijackToolClient, args: Record<string, unknown>): Promise<ToolResult> {
  const workerId = text(args, "worker_id");
  const rejection = rejectBadId(workerId, "worker_id");
  if (rejection !== undefined) {
    return { ...rejection };
  }
  const { ok, data } = await client.disconnectWorker(workerId);
  return toolAnswer(ok, data);
}
