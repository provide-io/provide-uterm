//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import type { JwtConfig } from "./index.ts";
import {
  browserRoleForRequest,
  browserSubjectForRequest,
  defaultConfig,
  elevateOwner,
  JwtValidationError,
  type Principal,
  readCookie,
  type SessionAuthContext,
  shareCookieName,
  shareRoleForRequest,
} from "./index.ts";

interface AuthGolden {
  worker_id: string;
  cookie_name: string;
  control_token: string;
  share_token: string;
  control_hash: string;
  share_hash: string;
  share_roles: Array<{ name: string; token: string; role: string | null }>;
  roles: Array<{
    name: string;
    roles: string[];
    owner: string | null;
    subject: string;
    jwt_role: string;
    result: string;
  }>;
}

const golden = loadGolden<AuthGolden>("sessionauth_golden.json");
const NOW = 1_700_000_000;

/** A session with both tokens issued. */
function context(overrides: Partial<SessionAuthContext> = {}): SessionAuthContext {
  return {
    workerId: golden.worker_id,
    jwt: { ...defaultConfig().jwt, mode: "jwt" } as JwtConfig,
    controlTokenHash: golden.control_hash,
    shareTokenHash: golden.share_hash,
    ...overrides,
  };
}

/** A request carrying headers. */
function request(headers: Record<string, string> = {}): { headers: Headers } {
  return { headers: new Headers(headers) };
}

/** A request carrying the share cookie for this session. */
function withCookie(token: string, extra: Record<string, string> = {}) {
  return request({ Cookie: `${golden.cookie_name}=${token}`, ...extra });
}

/** A decoder that hands back a fixed principal. */
function decoder(principal: Principal): (token: string, config: JwtConfig) => Promise<Principal> {
  return async () => principal;
}

/** A decoder that refuses. */
const refusing = async (): Promise<Principal> => {
  throw new JwtValidationError("token has expired");
};

describe("the share cookie", () => {
  it("is named for the session it is for", () => {
    // A token issued for one session cannot authorise another; a browser
    // holding several is not one that may use any of them anywhere.
    expect(shareCookieName(golden.worker_id)).toBe(golden.cookie_name);
    expect(shareCookieName("other")).not.toBe(golden.cookie_name);
  });

  it.each(golden.share_roles)("$name", (record) => {
    const found = shareRoleForRequest(withCookie(record.token), context(), NOW);
    expect(found ?? null).toBe(record.role);
  });

  it("grants admin for control and viewer for share", () => {
    expect(shareRoleForRequest(withCookie(golden.control_token), context(), NOW)).toBe("admin");
    expect(shareRoleForRequest(withCookie(golden.share_token), context(), NOW)).toBe("viewer");
  });

  it("ignores a cookie for another session", () => {
    const other = request({ Cookie: `uterm_tunnel_somebody-else=${golden.control_token}` });
    expect(shareRoleForRequest(other, context(), NOW)).toBeUndefined();
  });

  it("ignores a request with no cookie at all", () => {
    expect(shareRoleForRequest(request(), context(), NOW)).toBeUndefined();
    expect(shareRoleForRequest({}, context(), NOW)).toBeUndefined();
  });

  it("grants nothing when the request cannot say what it carries", () => {
    // It runs on the request path, so an unreadable header bag is no cookie
    // rather than a failed request.
    const hostile = {
      headers: {
        get(): string {
          throw new Error("headers unreadable");
        },
      },
    };
    expect(shareRoleForRequest(hostile, context(), NOW)).toBeUndefined();
  });

  it("grants nothing when no token was issued", () => {
    const none = context({ controlTokenHash: undefined, shareTokenHash: undefined });
    expect(shareRoleForRequest(withCookie(golden.control_token), none, NOW)).toBeUndefined();
  });

  it("stops working when the tunnel expires", () => {
    // Checked here as well as at the Worker, because a Durable Object is
    // reachable directly — a cookie outliving its tunnel would keep a revoked
    // share working.
    const expiring = context({ expiresAt: NOW });
    expect(shareRoleForRequest(withCookie(golden.control_token), expiring, NOW)).toBe("admin");
    expect(shareRoleForRequest(withCookie(golden.control_token), expiring, NOW + 1)).toBeUndefined();
  });

  it("binds to the address it was issued to, when asked", () => {
    const bound = context({ ipBinding: true, issuedIp: "10.0.0.2" });
    expect(shareRoleForRequest(withCookie(golden.control_token, { "CF-Connecting-IP": "10.0.0.2" }), bound, NOW)).toBe(
      "admin",
    );
    expect(
      shareRoleForRequest(withCookie(golden.control_token, { "CF-Connecting-IP": "10.0.0.3" }), bound, NOW),
    ).toBeUndefined();
    expect(shareRoleForRequest(withCookie(golden.control_token), bound, NOW)).toBeUndefined();
  });

  it("does not bind a tunnel issued before binding was switched on", () => {
    // There is nothing recorded to compare against, and refusing every such
    // cookie would revoke every share issued earlier.
    const unbound = context({ ipBinding: true, issuedIp: "" });
    expect(shareRoleForRequest(withCookie(golden.control_token), unbound, NOW)).toBe("admin");
  });

  it("treats an unrecorded address as unbound", () => {
    // A tunnel whose issuing address was never stored has nothing to compare
    // against, and refusing every such cookie would revoke every share
    // issued before the field existed.
    const missing = context({ ipBinding: true });
    expect(
      shareRoleForRequest(withCookie(golden.control_token, { "CF-Connecting-IP": "10.0.0.9" }), missing, NOW),
    ).toBe("admin");
  });

  it("does not check the address unless binding is on", () => {
    const loose = context({ issuedIp: "10.0.0.2" });
    expect(shareRoleForRequest(withCookie(golden.control_token, { "CF-Connecting-IP": "10.0.0.9" }), loose, NOW)).toBe(
      "admin",
    );
  });
});

describe("reading one cookie", () => {
  it("finds it among others", () => {
    expect(readCookie("a=1; b=2; c=3", "b")).toBe("2");
  });

  it("matches the whole name", () => {
    expect(readCookie("xb=2", "b")).toBeUndefined();
    expect(readCookie("bx=2", "b")).toBeUndefined();
  });

  it("keeps an equals sign inside the value", () => {
    expect(readCookie("b=a=c", "b")).toBe("a=c");
  });

  it("ignores one with no value", () => {
    expect(readCookie("b=", "b")).toBeUndefined();
    expect(readCookie("b", "b")).toBeUndefined();
  });

  it("tolerates space around it", () => {
    expect(readCookie(" b = 2 ", "b")).toBe("2");
  });
});

describe("raising an owner", () => {
  it.each(golden.roles)("$name", (record) => {
    expect(elevateOwner(record.jwt_role, record.owner ?? undefined, record.subject)).toBe(record.result);
  });

  it("is a floor, not an assignment", () => {
    // An admin stays admin; raising them to operator would demote them.
    expect(elevateOwner("admin", "u1", "u1")).toBe("admin");
    expect(elevateOwner("viewer", "u1", "u1")).toBe("operator");
    expect(elevateOwner("operator", "u1", "u1")).toBe("operator");
  });

  it("raises nobody else", () => {
    expect(elevateOwner("viewer", "u2", "u1")).toBe("viewer");
    expect(elevateOwner("viewer", undefined, "u1")).toBe("viewer");
  });

  it("does not treat an empty owner or subject as a match", () => {
    // Two absences are not an identity.
    expect(elevateOwner("viewer", "", "")).toBe("viewer");
    expect(elevateOwner("viewer", "", "u1")).toBe("viewer");
    expect(elevateOwner("viewer", "u1", "")).toBe("viewer");
  });
});

describe("the role a caller acts with", () => {
  const principal = (roles: string[], subjectId = "u1"): Principal => ({ subjectId, roles });

  it("admits everybody in an open mode", async () => {
    for (const mode of ["none", "dev"]) {
      const open = context({ jwt: { ...defaultConfig().jwt, mode } as JwtConfig });
      expect(await browserRoleForRequest(request(), open, refusing, NOW)).toBe("admin");
    }
  });

  it("prefers a share cookie over a token", async () => {
    // The narrower grant: issued for this session alone.
    const both = withCookie(golden.share_token, { Authorization: "Bearer t" });
    expect(await browserRoleForRequest(both, context(), decoder(principal(["admin"])), NOW)).toBe("viewer");
  });

  it("falls back to viewer with no token", async () => {
    expect(await browserRoleForRequest(request(), context(), refusing, NOW)).toBe("viewer");
  });

  it("falls back to viewer on a token it cannot validate", async () => {
    // Role extraction, not authentication — the token was already validated
    // before this runs, so refusing here would turn a role question into a
    // 500.
    const bearer = request({ Authorization: "Bearer t" });
    expect(await browserRoleForRequest(bearer, context(), refusing, NOW)).toBe("viewer");
  });

  it("lets a failure that is not a validation failure through", async () => {
    // A JWKS endpoint that cannot be reached is not a viewer; answering 5xx
    // is right where silently downgrading is not.
    const bearer = request({ Authorization: "Bearer t" });
    const broken = async (): Promise<Principal> => {
      throw new Error("jwks unreachable");
    };
    await expect(browserRoleForRequest(bearer, context(), broken, NOW)).rejects.toThrow(/jwks unreachable/);
  });

  it("raises the session's owner", async () => {
    const bearer = request({ Authorization: "Bearer t" });
    const owned = context({ owner: "u1" });
    expect(await browserRoleForRequest(bearer, owned, decoder(principal(["viewer"])), NOW)).toBe("operator");
    expect(await browserRoleForRequest(bearer, owned, decoder(principal(["viewer"], "u2")), NOW)).toBe("viewer");
  });
});

describe("the subject a caller is", () => {
  it("is nobody in an open mode", async () => {
    // Inventing one would make every session look owned.
    const open = context({ jwt: { ...defaultConfig().jwt, mode: "none" } as JwtConfig });
    const bearer = request({ Authorization: "Bearer t" });
    expect(await browserSubjectForRequest(bearer, open, decoder({ subjectId: "u1", roles: [] }))).toBeUndefined();
  });

  it("is the token's subject", async () => {
    const bearer = request({ Authorization: "Bearer t" });
    expect(await browserSubjectForRequest(bearer, context(), decoder({ subjectId: "u1", roles: [] }))).toBe("u1");
  });

  it("is nobody without a token, or with one that will not validate", async () => {
    expect(await browserSubjectForRequest(request(), context(), refusing)).toBeUndefined();
    const bearer = request({ Authorization: "Bearer t" });
    expect(await browserSubjectForRequest(bearer, context(), refusing)).toBeUndefined();
  });

  it("lets a failure that is not a validation failure through", async () => {
    const bearer = request({ Authorization: "Bearer t" });
    const broken = async (): Promise<Principal> => {
      throw new Error("jwks unreachable");
    };
    await expect(browserSubjectForRequest(bearer, context(), broken)).rejects.toThrow(/jwks unreachable/);
  });

  it("does not consult a share cookie", async () => {
    // A share cookie carries a role, not an identity — there is nobody to
    // name, and naming the session's owner would make a shared link look like
    // its owner.
    const shared = withCookie(golden.control_token);
    expect(await browserSubjectForRequest(shared, context(), refusing)).toBeUndefined();
  });
});
