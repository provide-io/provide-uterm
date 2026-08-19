//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/**
 * bootstrapServer applies the Cloudflare Access team-domain fill for every
 * auth mode.
 *
 * bootstrap.ts calls applyCfAccessTeamDomain before the mode branch, and says
 * in a comment that this is deliberate -- Go, C# and Python all apply the fill
 * as part of AuthConfig validation regardless of auth.mode, so a TypeScript
 * server that only filled it in one mode would diverge from the other three.
 * Nothing asserted it, so deleting that call left the whole suite green; the
 * mutation gate reported it as the one unexcused survivor in the package
 * (src/server/bootstrap.ts CallExpression).
 */
import { describe, expect, it } from "vitest";
import { bootstrapServer } from "./bootstrap.ts";

const TEAM_JWKS = "https://myteam.cloudflareaccess.com/cdn-cgi/access/certs";

/** Every mode bootstrapServer accepts, so none of them can skip the fill. */
const MODES = ["dev_token", "jwt", "header", "api_key"] as const;

function jwksFor(mode: string, auth: Record<string, unknown>): string {
  const { auth: resolved } = bootstrapServer({ authMode: mode, document: { auth } });
  return String((resolved as unknown as Record<string, unknown>).jwt_jwks_url ?? "");
}

describe("the Cloudflare Access team-domain fill", () => {
  it.each(MODES)("derives the JWKS URL under auth.mode=%s", (mode) => {
    expect(jwksFor(mode, { cf_access_team_domain: "myteam" })).toBe(TEAM_JWKS);
  });

  it("leaves a JWKS URL the document already set alone", () => {
    const explicit = "https://idp.example/keys";
    expect(jwksFor("header", { cf_access_team_domain: "myteam", jwt_jwks_url: explicit })).toBe(explicit);
  });

  it("fills nothing when no team domain is configured", () => {
    expect(jwksFor("header", {})).not.toBe(TEAM_JWKS);
  });

  it("accepts a team domain written as a full URL", () => {
    expect(jwksFor("header", { cf_access_team_domain: "https://myteam.cloudflareaccess.com/" })).toBe(TEAM_JWKS);
  });
});
