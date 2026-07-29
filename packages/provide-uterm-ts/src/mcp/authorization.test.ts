//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  authorized,
  DEFAULT_PRINCIPAL,
  denyPayload,
  hasAtLeast,
  type McpPrincipal,
  PRINCIPAL_STATE_KEY,
  primaryRole,
  type RequestContext,
  type Role,
  resolvePrincipal,
} from "./index.ts";

interface AuthGolden {
  default_principal: { subject_id: string; roles: string[] };
  principals: Array<{
    name: string;
    subject_id: string;
    roles: string[];
    primary_role: string;
    meets: Record<string, boolean>;
    may: Record<string, boolean>;
  }>;
  resolution: Array<{ name: string; subject_id: string; roles: string[] }>;
  denials: Array<Record<string, unknown>>;
}

const golden = loadGolden<AuthGolden>("mcpauth_golden.json");

function principalOf(record: { subject_id: string; roles: string[] }): McpPrincipal {
  return { subjectId: record.subject_id, roles: record.roles };
}

/** A context answering with whatever it was given. */
function contextOf(stored: unknown): RequestContext {
  return { getState: async () => stored };
}

describe("what a principal may do", () => {
  it.each(golden.principals)("$name", (record) => {
    const principal = principalOf(record);
    for (const [minimum, expected] of Object.entries(record.meets)) {
      expect(hasAtLeast(principal, minimum as Role)).toBe(expected);
    }
    expect(primaryRole(principal)).toBe(record.primary_role);
  });

  it.each(golden.principals)("$name — tool by tool", async (record) => {
    for (const [tool, mayCall] of Object.entries(record.may)) {
      const outcome = await authorized(tool, undefined, async () => "ran", principalOf(record));
      expect(outcome.allowed).toBe(mayCall);
    }
  });

  it("judges somebody on the best role they hold", () => {
    // Alternatives, not requirements: an operator who is also an admin gets
    // an admin's reach.
    expect(hasAtLeast({ subjectId: "a", roles: ["viewer", "admin"] }, "admin")).toBe(true);
    expect(hasAtLeast({ subjectId: "a", roles: ["viewer", "operator"] }, "operator")).toBe(true);
    expect(hasAtLeast({ subjectId: "a", roles: ["viewer", "operator"] }, "admin")).toBe(false);
  });

  it("gives somebody holding no role at all nothing", () => {
    // Including viewer: an empty set is not a quiet grant of the lowest tier.
    for (const minimum of ["viewer", "operator", "admin"] as const) {
      expect(hasAtLeast({ subjectId: "a", roles: [] }, minimum)).toBe(false);
    }
  });

  it("gives an unrecognised role nothing, capitals included", () => {
    for (const roles of [["superuser"], ["Admin"], ["ADMIN"], [""]]) {
      expect(hasAtLeast({ subjectId: "a", roles }, "viewer")).toBe(false);
    }
  });

  it("names a role for display that a decision never consults", () => {
    // `primaryRole` can name a role the principal cannot use — somebody
    // holding only `superuser` gets `superuser` and meets nothing. The
    // decision asks the ladder, not this.
    const stranger: McpPrincipal = { subjectId: "a", roles: ["superuser"] };
    expect(primaryRole(stranger)).toBe("superuser");
    expect(hasAtLeast(stranger, "viewer")).toBe(false);
    // And somebody holding nothing is displayed as a viewer while meeting
    // nothing.
    const nobody: McpPrincipal = { subjectId: "a", roles: [] };
    expect(primaryRole(nobody)).toBe("viewer");
    expect(hasAtLeast(nobody, "viewer")).toBe(false);
  });

  it("picks the best of several unequal roles", () => {
    // The display value follows the ladder too, whichever order they arrive
    // in.
    expect(primaryRole({ subjectId: "a", roles: ["viewer", "operator", "admin"] })).toBe("admin");
    expect(primaryRole({ subjectId: "a", roles: ["admin", "operator", "viewer"] })).toBe("admin");
    expect(primaryRole({ subjectId: "a", roles: ["viewer", "operator"] })).toBe("operator");
    // A role nobody defined ranks below every real one, so a real one wins.
    expect(primaryRole({ subjectId: "a", roles: ["superuser", "viewer"] })).toBe("viewer");
  });

  it("keeps the first of equals", () => {
    // Every unrecognised role ranks the same, so ties are reachable. The
    // reference keeps its roles in a `frozenset`, so *its* answer here depends
    // on hash order and is not a behaviour to match; this pins the port's own,
    // so a display value at least cannot change under it.
    expect(primaryRole({ subjectId: "a", roles: ["superuser", "root"] })).toBe("superuser");
    expect(primaryRole({ subjectId: "a", roles: ["root", "superuser"] })).toBe("root");
  });

  it("falls back to the least it can be", () => {
    expect(DEFAULT_PRINCIPAL).toEqual({
      subjectId: golden.default_principal.subject_id,
      roles: golden.default_principal.roles,
    });
  });
});

describe("who is calling", () => {
  it.each(golden.resolution)("$name", async (record) => {
    const fallback: McpPrincipal = { subjectId: "configured", roles: ["operator"] };
    const stored: McpPrincipal = { subjectId: "from-request", roles: ["admin"] };
    const contexts: Record<string, RequestContext | undefined> = {
      "no context at all": undefined,
      "a context carrying a principal": contextOf(stored),
      "a context carrying nothing": contextOf(null),
      "a context carrying something else": contextOf({ subject_id: "x" }),
      "a context whose lookup fails": {
        getState: async () => {
          throw new Error("state unavailable");
        },
      },
    };
    const resolved = await resolvePrincipal(contexts[record.name], fallback);
    expect({ subject_id: resolved.subjectId, roles: [...resolved.roles].sort() }).toEqual({
      subject_id: record.subject_id,
      roles: record.roles,
    });
  });

  it("prefers the request's own principal over the configured one", () => {
    expect(PRINCIPAL_STATE_KEY).toBe("uterm.principal");
  });

  it("treats a broken lookup as having said nothing", async () => {
    // A lookup that fails must not become a privilege — nor an error that
    // takes the call down.
    const fallback: McpPrincipal = { subjectId: "configured", roles: ["viewer"] };
    const resolved = await resolvePrincipal(
      {
        getState: async () => {
          throw new Error("gone");
        },
      },
      fallback,
    );
    expect(resolved).toEqual(fallback);
  });

  it("ignores something merely shaped like a principal", async () => {
    // A stored value with the wrong field names is not a principal, and
    // reading it as one would invent an identity.
    for (const stored of [{ subject_id: "x", roles: ["admin"] }, { subjectId: "x" }, "admin", 42, null]) {
      expect(await resolvePrincipal(contextOf(stored), DEFAULT_PRINCIPAL)).toEqual(DEFAULT_PRINCIPAL);
    }
  });

  it("takes a stored principal that is one", async () => {
    const stored: McpPrincipal = { subjectId: "ada", roles: ["admin"] };
    expect(await resolvePrincipal(contextOf(stored), DEFAULT_PRINCIPAL)).toEqual(stored);
  });
});

describe("refusing a call", () => {
  it.each(golden.denials)("refusing $tool", (record) => {
    const payload = denyPayload(
      record.tool as string,
      { subjectId: record.principal as string, roles: record.principal_roles as string[] },
      record.required_role as Role,
    );
    expect(payload).toEqual(record);
  });

  it("answers in the shape every other tool answers in", () => {
    // So a caller branches on it rather than special-casing authorization.
    const payload = denyPayload("hijack_begin", { subjectId: "ada", roles: ["viewer"] }, "admin");
    expect(payload.success).toBe(false);
    expect(payload.error).toBe("authorization_denied");
  });

  it("says what would be needed and what is held", () => {
    // Which is what an operator needs in order to fix the grant.
    const payload = denyPayload("session_create", { subjectId: "ada", roles: ["operator", "viewer"] }, "admin");
    expect(payload).toMatchObject({
      tool: "session_create",
      required_role: "admin",
      principal: "ada",
      principal_roles: ["operator", "viewer"],
    });
  });

  it("sorts the roles it reports", () => {
    // So two servers refusing the same call say the same thing.
    expect(denyPayload("x" as never, { subjectId: "a", roles: ["viewer", "admin"] }, "admin").principal_roles).toEqual([
      "admin",
      "viewer",
    ]);
  });
});

describe("running a tool through the chokepoint", () => {
  it("runs it for somebody who may", async () => {
    const outcome = await authorized("session_list", undefined, async () => "listed", {
      subjectId: "ada",
      roles: ["viewer"],
    });
    expect(outcome).toEqual({ allowed: true, result: "listed" });
  });

  it("does not run it for somebody who may not", async () => {
    // Refused before the body, not after: a tool that ran and then reported a
    // refusal would already have done whatever it does.
    let ran = false;
    const outcome = await authorized(
      "hijack_begin",
      undefined,
      async () => {
        ran = true;
        return "took over";
      },
      { subjectId: "ada", roles: ["viewer"] },
    );
    expect(ran).toBe(false);
    expect(outcome.allowed).toBe(false);
  });

  it("hands the tool the principal it resolved", async () => {
    const stored: McpPrincipal = { subjectId: "ada", roles: ["admin"] };
    const outcome = await authorized(
      "session_create",
      contextOf(stored),
      async (principal) => principal.subjectId,
      DEFAULT_PRINCIPAL,
    );
    expect(outcome).toEqual({ allowed: true, result: "ada" });
  });

  it("refuses a tool with no policy rather than running it", async () => {
    // What stops a newly added tool slipping through unguarded — and it is
    // checked before a principal is even resolved.
    let asked = false;
    await expect(
      authorized(
        "session_delete",
        {
          getState: async () => {
            asked = true;
            return undefined;
          },
        },
        async () => "ran",
      ),
    ).rejects.toThrow("No authorization policy registered");
    expect(asked).toBe(false);
  });

  it("uses the request's principal over the configured one", async () => {
    // A viewer configured as the default does not stop an admin who
    // authenticated.
    const outcome = await authorized(
      "hijack_begin",
      contextOf({ subjectId: "ada", roles: ["admin"] }),
      async () => "took over",
      { subjectId: "anonymous", roles: ["viewer"] },
    );
    expect(outcome).toEqual({ allowed: true, result: "took over" });
  });

  it("falls back to the default when the request carries nobody", async () => {
    const outcome = await authorized("hijack_begin", contextOf(null), async () => "took over", {
      subjectId: "anonymous",
      roles: ["viewer"],
    });
    expect(outcome.allowed).toBe(false);
  });
});
