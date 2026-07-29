//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Naming agents, and deciding what they are allowed to hold.
 *
 * Port of the decisions in `provide.uterm.manager.process_impl`. Both of them
 * bound what a compromised worker can do:
 *
 * * **A worker never inherits the operator token.** The manager holds one that
 *   can spawn and kill the whole fleet; a worker only needs to report about
 *   itself. Where a fleet secret is configured, the worker's environment gets
 *   a token derived from it and bound to that worker's own agent id, and the
 *   raw secret is stripped so it never reaches the child.
 * * **Every agent gets a name nobody else has.** A repeated id would put two
 *   agents' reports in one place.
 */

import { deriveAgentToken } from "./auth.ts";

/** What an agent id looks like: the word, then a number. */
const AGENT_ID_PATTERN = /^agent_(\d+)$/;

/** The variable a worker's client reads its token from. */
export const DEFAULT_OPERATOR_TOKEN_VAR = "UTERM_MANAGER_API_TOKEN";

/** The variable the manager keeps the fleet secret in. */
export const DEFAULT_WORKER_TOKEN_VAR = "UTERM_MANAGER_WORKER_TOKEN";

/**
 * The number in an agent id, or nothing when it is not one.
 *
 * Trimmed first, as the reference does, so an id that arrived with whitespace
 * around it still counts towards the next one.
 */
export function parseAgentIndex(agentId: string): number | undefined {
  const match = AGENT_ID_PATTERN.exec(String(agentId).trim());
  if (match === null) {
    return undefined;
  }
  // Leading zeros and all: `agent_0001` is the same agent as `agent_001`.
  return Number.parseInt(match[1] as string, 10);
}

/** Where the manager keeps track of what it has named. */
export interface AgentRegistry {
  /** Agents it knows about. */
  agents: Iterable<string>;
  /** Processes it is running. */
  processes: Iterable<string>;
}

/** Hands out agent names nobody else holds. */
export class AgentNamer {
  #nextIndex = 0;

  /**
   * Move past every id already known, and say where that is.
   *
   * Never moves backwards: an agent that has been forgotten does not free its
   * name for reuse, because a report arriving late would then land on
   * somebody else.
   */
  syncNextIndex(registry: AgentRegistry): number {
    let highest = -1;
    for (const id of new Set([...registry.agents, ...registry.processes])) {
      const index = parseAgentIndex(id);
      if (index !== undefined) {
        highest = Math.max(highest, index);
      }
    }
    this.#nextIndex = Math.max(this.#nextIndex, highest + 1);
    return this.#nextIndex;
  }

  /** Take note of an id somebody else chose. */
  noteAgentId(agentId: string): void {
    const index = parseAgentIndex(agentId);
    if (index !== undefined) {
      this.#nextIndex = Math.max(this.#nextIndex, index + 1);
    }
  }

  /**
   * The next free name.
   *
   * No search is needed, and the reference's defensive loop is left out with
   * the reason stated: every candidate is `agent_` followed by digits, so any
   * known id that could collide with one necessarily matches the pattern —
   * and {@link syncNextIndex} has therefore already moved past it. The first
   * candidate is free by construction.
   */
  allocate(registry: AgentRegistry): string {
    const index = this.syncNextIndex(registry);
    this.#nextIndex = index + 1;
    return `agent_${String(index).padStart(3, "0")}`;
  }
}

/** Which variables the tokens live in. */
export interface TokenVars {
  operatorVar?: string;
  workerVar?: string;
}

/**
 * Cut a worker's environment down to what it may hold.
 *
 * Where a fleet secret is configured, the operator token is replaced by one
 * derived from that secret and bound to this worker's id — so a worker holds
 * something it cannot use to impersonate any other. The raw secret is always
 * stripped, configured or not: it is the manager's, and a copy left in a
 * child's environment is a copy that can derive every worker's token.
 *
 * @param environment The child's environment, changed in place as the
 *   reference changes it.
 * @param fleetSecret What the manager holds, or nothing when none is set.
 */
export function scopeWorkerTokens(
  environment: Record<string, string>,
  agentId: string,
  fleetSecret: string | undefined,
  vars: TokenVars = {},
): void {
  const operatorVar = vars.operatorVar ?? DEFAULT_OPERATOR_TOKEN_VAR;
  const workerVar = vars.workerVar ?? DEFAULT_WORKER_TOKEN_VAR;

  // Trimmed before it is judged: a variable set to spaces is a variable
  // somebody meant to unset.
  const secret = (fleetSecret ?? "").trim();
  if (secret !== "") {
    environment[operatorVar] = deriveAgentToken(secret, agentId);
  }
  // Always, even when none was configured — a stale copy in the environment
  // this inherited is the same secret.
  delete environment[workerVar];
}
