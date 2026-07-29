//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  AgentNamer,
  DEFAULT_OPERATOR_TOKEN_VAR,
  DEFAULT_WORKER_TOKEN_VAR,
  deriveAgentToken,
  parseAgentIndex,
  scopeWorkerTokens,
} from "./index.ts";

interface SupervisionGolden {
  operator_var: string;
  worker_var: string;
  parsed: Array<{ id: string; index: number | null }>;
  allocations: Array<{
    name: string;
    agents: string[];
    processes: string[];
    allocated: string[];
    next_index: number;
  }>;
  scoped: Array<{
    name: string;
    env: Record<string, string>;
    agent_id: string;
    worker_token: string;
    scoped: Record<string, string>;
  }>;
  derived: Record<string, string>;
}

const golden = loadGolden<SupervisionGolden>("managerprocess_golden.json");

describe("reading an agent's number out of its name", () => {
  it.each(golden.parsed)("$id", (record) => {
    expect(parseAgentIndex(record.id) ?? null).toBe(record.index);
  });

  it("takes the number an agent id carries", () => {
    expect(parseAgentIndex("agent_000")).toBe(0);
    expect(parseAgentIndex("agent_042")).toBe(42);
    expect(parseAgentIndex("agent_1000")).toBe(1000);
  });

  it("reads leading zeros as the same agent", () => {
    // `agent_0001` and `agent_001` are one agent, so both count towards the
    // next name.
    expect(parseAgentIndex("agent_0001")).toBe(1);
  });

  it("ignores the spaces around a name", () => {
    expect(parseAgentIndex("  agent_001  ")).toBe(1);
  });

  it("refuses anything that is not one", () => {
    // Matched whole and in lower case: `AGENT_001` and `agent_001x` are names
    // somebody else chose, and counting them would skip numbers.
    for (const id of ["agent_", "agent", "agent_abc", "agent_-1", "AGENT_001", "worker_001", "", "agent_001x"]) {
      expect(parseAgentIndex(id)).toBeUndefined();
    }
  });
});

describe("handing out an agent's name", () => {
  it.each(golden.allocations)("$name", (record) => {
    const registry = { agents: record.agents, processes: record.processes };
    expect(new AgentNamer().allocate(registry)).toBe(record.allocated[0]);
    expect(new AgentNamer().syncNextIndex(registry)).toBe(record.next_index);
  });

  it("starts at the beginning when nothing is known", () => {
    expect(new AgentNamer().allocate({ agents: [], processes: [] })).toBe("agent_000");
  });

  it("continues past the highest already taken", () => {
    // Past the highest, not into the first gap: an id that has been used is
    // one a late report may still arrive for.
    expect(new AgentNamer().allocate({ agents: ["agent_000", "agent_002"], processes: [] })).toBe("agent_003");
  });

  it("looks at both what it knows and what it is running", () => {
    expect(new AgentNamer().allocate({ agents: ["agent_000"], processes: ["agent_001"] })).toBe("agent_002");
  });

  it("counts an id appearing in both places once", () => {
    expect(new AgentNamer().allocate({ agents: ["agent_000"], processes: ["agent_000"] })).toBe("agent_001");
  });

  it("ignores names it did not choose", () => {
    expect(new AgentNamer().allocate({ agents: ["worker-a", "something"], processes: [] })).toBe("agent_000");
  });

  it("pads to three digits, and grows past them", () => {
    expect(new AgentNamer().allocate({ agents: [], processes: [] })).toBe("agent_000");
    expect(new AgentNamer().allocate({ agents: ["agent_1000"], processes: [] })).toBe("agent_1001");
  });

  it("never hands out the same name twice", () => {
    // Which is the whole point: two agents sharing a name put two agents'
    // reports in one place.
    const namer = new AgentNamer();
    const agents: string[] = [];
    const seen = new Set<string>();
    for (let index = 0; index < 50; index += 1) {
      const id = namer.allocate({ agents, processes: [] });
      expect(seen.has(id)).toBe(false);
      seen.add(id);
      agents.push(id);
    }
    expect(seen.size).toBe(50);
  });

  it("does not go backwards when an agent is forgotten", () => {
    // A name freed by a departure is a name a late report may still arrive
    // for.
    const namer = new AgentNamer();
    expect(namer.allocate({ agents: ["agent_005"], processes: [] })).toBe("agent_006");
    expect(namer.allocate({ agents: [], processes: [] })).toBe("agent_007");
  });

  it("takes note of a name chosen elsewhere", () => {
    const namer = new AgentNamer();
    namer.noteAgentId("agent_020");
    expect(namer.allocate({ agents: [], processes: [] })).toBe("agent_021");
  });

  it("ignores a note about a name it did not choose", () => {
    const namer = new AgentNamer();
    namer.noteAgentId("worker-a");
    expect(namer.allocate({ agents: [], processes: [] })).toBe("agent_000");
  });

  it("needs no search to find a free name", () => {
    // Every candidate is `agent_` and digits, so any id that could collide
    // matches the pattern — and the sync has already moved past it.
    const registry = { agents: ["agent_000", "agent_001", "agent_002"], processes: [] };
    expect(new AgentNamer().allocate(registry)).toBe("agent_003");
  });
});

describe("what a worker is allowed to hold", () => {
  it.each(golden.scoped)("$name", (record) => {
    const environment = { ...record.env };
    scopeWorkerTokens(environment, record.agent_id, record.worker_token);
    expect(environment).toEqual(record.scoped);
  });

  it("replaces the operator token with one bound to this worker", () => {
    // The manager's token can spawn and kill the fleet; a worker only needs
    // to report about itself.
    const environment = { [DEFAULT_OPERATOR_TOKEN_VAR]: "omnipotent" };
    scopeWorkerTokens(environment, "agent_001", "fleet-secret");
    expect(environment[DEFAULT_OPERATOR_TOKEN_VAR]).toBe(golden.derived.agent_001);
    expect(environment[DEFAULT_OPERATOR_TOKEN_VAR]).not.toBe("omnipotent");
  });

  it("gives each worker a token only it can use", () => {
    // A worker holding another's token could report as that other worker.
    const first = { [DEFAULT_OPERATOR_TOKEN_VAR]: "omnipotent" };
    const second = { [DEFAULT_OPERATOR_TOKEN_VAR]: "omnipotent" };
    scopeWorkerTokens(first, "agent_001", "fleet-secret");
    scopeWorkerTokens(second, "agent_002", "fleet-secret");
    expect(first[DEFAULT_OPERATOR_TOKEN_VAR]).not.toBe(second[DEFAULT_OPERATOR_TOKEN_VAR]);
    expect(second[DEFAULT_OPERATOR_TOKEN_VAR]).toBe(golden.derived.agent_002);
  });

  it("always strips the fleet secret, configured or not", () => {
    // A copy in a child's environment is a copy that can derive every
    // worker's token.
    for (const secret of ["fleet-secret", "", undefined]) {
      const environment = {
        [DEFAULT_OPERATOR_TOKEN_VAR]: "omnipotent",
        [DEFAULT_WORKER_TOKEN_VAR]: "left-over",
      };
      scopeWorkerTokens(environment, "agent_001", secret);
      expect(DEFAULT_WORKER_TOKEN_VAR in environment).toBe(false);
    }
  });

  it("leaves the operator token alone when no secret is configured", () => {
    // Unchanged behaviour for a fleet that has not been migrated.
    const environment = { [DEFAULT_OPERATOR_TOKEN_VAR]: "omnipotent", PATH: "/usr/bin" };
    scopeWorkerTokens(environment, "agent_001", undefined);
    expect(environment).toEqual({ [DEFAULT_OPERATOR_TOKEN_VAR]: "omnipotent", PATH: "/usr/bin" });
  });

  it("treats a secret of only spaces as none at all", () => {
    const environment = { [DEFAULT_OPERATOR_TOKEN_VAR]: "omnipotent" };
    scopeWorkerTokens(environment, "agent_001", "   ");
    expect(environment[DEFAULT_OPERATOR_TOKEN_VAR]).toBe("omnipotent");
  });

  it("gives a worker a token even when it inherited nothing", () => {
    const environment: Record<string, string> = {};
    scopeWorkerTokens(environment, "agent_001", "fleet-secret");
    expect(environment[DEFAULT_OPERATOR_TOKEN_VAR]).toBe(golden.derived.agent_001);
  });

  it("leaves everything else in the environment alone", () => {
    const environment = { PATH: "/usr/bin", HOME: "/home/ada", [DEFAULT_OPERATOR_TOKEN_VAR]: "x" };
    scopeWorkerTokens(environment, "agent_001", "fleet-secret");
    expect(environment.PATH).toBe("/usr/bin");
    expect(environment.HOME).toBe("/home/ada");
  });

  it("puts the token where the manager was told to", () => {
    const environment = { OPERATOR: "omnipotent", FLEET: "secret" };
    scopeWorkerTokens(environment, "agent_001", "secret", { operatorVar: "OPERATOR", workerVar: "FLEET" });
    expect(environment.OPERATOR).toBe(deriveAgentToken("secret", "agent_001"));
    expect("FLEET" in environment).toBe(false);
  });

  it("uses the variables the reference uses", () => {
    expect(DEFAULT_OPERATOR_TOKEN_VAR).toBe(golden.operator_var);
    expect(DEFAULT_WORKER_TOKEN_VAR).toBe(golden.worker_var);
  });
});
