//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The RBAC table, held to `serverauthz_golden`.
 *
 * A table is the easiest kind of thing to get subtly wrong in a port: a role
 * granted one capability too many is not a crash, it is a caller who can read
 * somebody else's session while every test still passes. So every cell comes
 * from the reference rather than from a reading of it.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  type AuthorizableSession,
  canMutateSession,
  canReadSession,
  capabilitiesFor,
  isAdmin,
  isOwner,
  ROLE_CAPABILITIES,
} from "./authorization.ts";

interface PrincipalCase {
  name: string;
  subject_id: string;
  roles: string[];
  scopes: string[];
  admin_session_scope?: string;
  why?: string;
  capabilities: string[];
  is_admin: boolean;
  can_read_session: Record<string, boolean>;
  is_owner: Record<string, boolean>;
}

const CORPUS = JSON.parse(
  readFileSync(join(import.meta.dirname, "..", "..", "testdata", "serverauthz_golden.json"), "utf8"),
) as {
  role_capabilities: Record<string, string[]>;
  sessions: (AuthorizableSession & { name: string })[];
  principals: PrincipalCase[];
};

/** One of the corpus's principals, as this port's decisions read one. */
function principalOf(one: PrincipalCase) {
  return {
    subject_id: one.subject_id,
    roles: new Set(one.roles),
    scopes: new Set(one.scopes),
    admin_session_scope: one.admin_session_scope ?? null,
  };
}

describe("what each role grants", () => {
  it("grants exactly what the reference grants", () => {
    const ported = Object.fromEntries(
      Object.entries(ROLE_CAPABILITIES).map(([role, capabilities]) => [role, [...capabilities].sort()]),
    );
    expect(ported).toEqual(CORPUS.role_capabilities);
  });

  it("knows the three roles and no more", () => {
    expect(Object.keys(ROLE_CAPABILITIES).sort()).toEqual(Object.keys(CORPUS.role_capabilities).sort());
  });
});

describe("what a principal holds", () => {
  for (const one of CORPUS.principals) {
    describe(`${one.name}${one.why === undefined ? "" : `: ${one.why}`}`, () => {
      const principal = principalOf(one);

      it("holds the capabilities the reference computed", () => {
        expect([...capabilitiesFor(principal)].sort()).toEqual(one.capabilities);
      });

      it("is an administrator of the whole server, or is not", () => {
        expect(isAdmin(principal)).toBe(one.is_admin);
      });

      for (const session of CORPUS.sessions) {
        it(`reads the ${session.name} session, or does not`, () => {
          expect(canReadSession(principal, session)).toBe(one.can_read_session[session.name]);
        });

        it(`owns the ${session.name} session, or does not`, () => {
          expect(isOwner(principal, session)).toBe(one.is_owner[session.name]);
        });
      }
    });
  }
});

describe("the two cases a port gets wrong", () => {
  it("does not let a session-scoped admin grant reach another session", () => {
    // A tunnel share hands out `admin` confined to one session. Without the
    // scope check that grant would be a global administrator.
    const scoped = CORPUS.principals.find((one) => one.name === "session_admin") as PrincipalCase;
    expect(scoped.roles).toContain("admin");
    expect(isAdmin(principalOf(scoped))).toBe(false);
    expect(canReadSession(principalOf(scoped), { session_id: "other", owner: null, visibility: "private" })).toBe(
      false,
    );
  });

  it("never lets a token's scopes widen what its roles allow", () => {
    // The narrowing is an intersection, so a scope naming a capability the
    // role does not hold grants nothing.
    const principal = { subject_id: "x", roles: new Set(["viewer"]), scopes: new Set(["session.control.delete"]) };
    expect([...capabilitiesFor(principal)]).toEqual([]);
  });

  it("grants nothing for a role the table does not name", () => {
    // Roles reach here through the allow-list, so this cannot arrive from a
    // token — but a caller building a principal by hand must not be able to
    // invent a role that grants something by not being known.
    expect([...capabilitiesFor({ subject_id: "x", roles: new Set(["superuser"]), scopes: new Set<string>() })]).toEqual(
      [],
    );
  });

  it("treats a principal with no admin scope field as a global admin", () => {
    // The field is optional; absent is not "confined to nothing".
    expect(isAdmin({ subject_id: "x", roles: new Set(["admin"]), scopes: new Set<string>() })).toBe(true);
  });

  it("refuses a visibility nobody defined rather than falling open", () => {
    const principal = { subject_id: "x", roles: new Set(["operator"]), scopes: new Set<string>() };
    expect(canReadSession(principal, { session_id: "s", owner: null, visibility: "brand-new" })).toBe(false);
  });
});

describe("who may change a session, as against who may watch it", () => {
  /** A principal holding every capability its roles grant. */
  function principal(roles: string[], subject = "x") {
    return { subject_id: subject, roles: new Set(roles), scopes: new Set<string>() };
  }

  it("refuses a role that does not hold the action at all", () => {
    // An operator may change the mode and may not take the lease. The two are
    // separate capabilities precisely so that this is expressible.
    const session = { session_id: "s", owner: "x", visibility: "public" };
    expect(canMutateSession(principal(["operator"]), session, "session.control.mode")).toBe(true);
    expect(canMutateSession(principal(["operator"]), session, "session.control.hijack")).toBe(false);
  });

  it("lets an administrator of the session through", () => {
    expect(
      canMutateSession(
        principal(["admin"]),
        { session_id: "s", owner: null, visibility: "private" },
        "session.control.hijack",
      ),
    ).toBe(true);
  });

  it("lets the owner through without any administrative role", () => {
    expect(
      canMutateSession(
        principal(["operator"], "owner"),
        { session_id: "s", owner: "owner", visibility: "private" },
        "session.control.mode",
      ),
    ).toBe(true);
  });

  it("refuses everyone else, however visible the session is", () => {
    // Visibility says who may watch, never who may change: a public session
    // is still writable only by its owner or an administrator of it.
    expect(
      canMutateSession(
        principal(["operator"]),
        { session_id: "s", owner: "someone-else", visibility: "public" },
        "session.control.mode",
      ),
    ).toBe(false);
  });

  it("refuses an unowned session to anyone who is not an administrator of it", () => {
    // Nobody's consent could be attributed to the change.
    expect(
      canMutateSession(
        principal(["operator"]),
        { session_id: "s", owner: null, visibility: "public" },
        "session.control.mode",
      ),
    ).toBe(false);
  });
});
