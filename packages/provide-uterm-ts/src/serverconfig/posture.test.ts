//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { computeSecurityPosture, type PostureConfig, type SecurityPosture } from "./index.ts";

interface PostureGolden {
  postures: Array<{ name: string; overrides: Record<string, unknown>; result: SecurityPosture }>;
}

const golden = loadGolden<PostureGolden>("posture_golden.json");

/** The reference's own defaults, which the corpus was recorded against. */
function baseConfig(): PostureConfig {
  return {
    environment: "production",
    server: { host: "127.0.0.1" },
    auth: {
      mode: "dev_token",
      header_mode_acknowledged: false,
      allow_adhoc_browser_observers: false,
      webhook_idp_on_failure: "deny",
      identity_provider: "local",
      webhook_idp_require_signed_response: true,
      webhook_idp_require_response_nonce: false,
    },
    security: { mode: "prod", dev_mode_acknowledged: false, block_private_connector_targets: false },
    audit: { chain_enabled: false },
  };
}

/** The base configuration with the corpus's dotted overrides applied. */
function configFrom(overrides: Record<string, unknown>): PostureConfig {
  const config = baseConfig() as unknown as Record<string, Record<string, unknown>>;
  for (const [dotted, value] of Object.entries(overrides)) {
    const [section, field] = dotted.split("__");
    if (field === undefined) {
      (config as Record<string, unknown>)[section as string] = value;
    } else {
      (config[section as string] as Record<string, unknown>)[field] = value;
    }
  }
  return config as unknown as PostureConfig;
}

describe("the server's security posture", () => {
  it.each(golden.postures)("$name", (record) => {
    expect(computeSecurityPosture(configFrom(record.overrides))).toEqual(record.result);
  });

  it("counts an acknowledgement only where it unlocks something", () => {
    // Two of the knobs weaken nothing on their own. Listing them regardless
    // would report a posture worse than the deployment has, and a report that
    // cries wolf stops being read.
    const alone = computeSecurityPosture(configFrom({ security__dev_mode_acknowledged: true }));
    expect(alone.dev_opt_outs).not.toContain("security.dev_mode_acknowledged");
    const paired = computeSecurityPosture(configFrom({ security__mode: "dev", security__dev_mode_acknowledged: true }));
    expect(paired.dev_opt_outs).toContain("security.dev_mode_acknowledged");

    const headerAlone = computeSecurityPosture(configFrom({ auth__header_mode_acknowledged: true }));
    expect(headerAlone.dev_opt_outs).not.toContain("auth.header_mode_acknowledged");
    const headerPaired = computeSecurityPosture(
      configFrom({ auth__mode: "header", auth__header_mode_acknowledged: true }),
    );
    expect(headerPaired.dev_opt_outs).toContain("auth.header_mode_acknowledged");
  });

  it("does not let a loopback relaxation demote the posture", () => {
    // A deployment that cannot be reached off-box is not made insecure by
    // relaxing a control on it.
    const loopback = configFrom({ auth__allow_adhoc_browser_observers: true });
    const routable = configFrom({ server__host: "0.0.0.0", auth__allow_adhoc_browser_observers: true });
    expect(computeSecurityPosture(loopback).secure).toBe(true);
    expect(computeSecurityPosture(routable).secure).toBe(false);
  });

  it("is never secure outside production", () => {
    const clean = { auth__mode: "jwt", security__block_private_connector_targets: true };
    expect(computeSecurityPosture(configFrom({ ...clean, environment: "development" })).secure).toBe(false);
    expect(computeSecurityPosture(configFrom({ ...clean, environment: "staging" })).secure).toBe(false);
    expect(computeSecurityPosture(configFrom(clean)).secure).toBe(true);
  });

  it("reads a bind host a person typed", () => {
    // It comes from a config file, so it is normalised before it is compared.
    for (const host of [" 127.0.0.1 ", "LOCALHOST", "localhost", "::1"]) {
      expect(computeSecurityPosture(configFrom({ server__host: host })).is_loopback).toBe(true);
    }
    expect(computeSecurityPosture(configFrom({ server__host: "10.0.0.5" })).is_loopback).toBe(false);
  });

  it("warns about an unsigned webhook idp only for the webhook provider", () => {
    const webhook = configFrom({
      auth__identity_provider: "webhook",
      auth__webhook_idp_require_signed_response: false,
    });
    expect(computeSecurityPosture(webhook).dev_opt_outs).toContain("auth.webhook_idp_require_signed_response=false");
    const local = configFrom({ auth__webhook_idp_require_signed_response: false });
    expect(computeSecurityPosture(local).dev_opt_outs).not.toContain("auth.webhook_idp_require_signed_response=false");
  });

  it("reports a compliance gap without demoting the posture", () => {
    // A plain audit log does not relax an existing control, so it warns
    // rather than counting as an opt-out.
    const clean = configFrom({ auth__mode: "jwt", security__block_private_connector_targets: true });
    const posture = computeSecurityPosture(clean);
    expect(posture.audit_chain_enabled).toBe(false);
    expect(posture.warnings.some((warning) => warning.includes("tamper-evident"))).toBe(true);
    expect(posture.dev_opt_outs).toEqual([]);
    expect(posture.secure).toBe(true);
  });

  it("sorts the opt-outs so the report reads the same every time", () => {
    const posture = computeSecurityPosture(
      configFrom({
        auth__mode: "dev_token",
        security__mode: "dev",
        auth__allow_adhoc_browser_observers: true,
      }),
    );
    expect(posture.dev_opt_outs).toEqual([...posture.dev_opt_outs].sort());
  });

  it("reports nothing for a signing setting a config predates", () => {
    // Defensive in the reference too: an embedder's config object may not
    // carry the field, and the report has to stay JSON-safe.
    const config = configFrom({});
    delete (config.auth as Record<string, unknown>).webhook_idp_require_signed_response;
    expect(computeSecurityPosture(config).idp_signing_required).toBeNull();
  });

  it("reads a mode a config wrote with no value at all", () => {
    // `text` supplies the empty string, which is neither dev_token nor
    // header, so nothing is claimed about a config that says nothing.
    const config = configFrom({});
    delete (config.auth as Record<string, unknown>).mode;
    delete (config.security as Record<string, unknown>).mode;
    const posture = computeSecurityPosture(config);
    expect(posture.auth_mode).toBe("");
    expect(posture.dev_opt_outs).not.toContain("auth.mode=dev_token");
    expect(posture.dev_opt_outs).not.toContain("security.mode=dev");
  });

  it("reports the mode a deployment declared, not the one it became", () => {
    // `dev_token` collapses to `jwt` once the dev IdP is set up at startup.
    // Reporting the collapsed mode would hide the opt-out entirely.
    const config = configFrom({ auth__mode: "jwt" });
    (config.auth as Record<string, unknown>)._declared_auth_mode = "dev_token";
    const posture = computeSecurityPosture(config);
    expect(posture.auth_mode).toBe("dev_token");
    expect(posture.dev_opt_outs).toContain("auth.mode=dev_token");
  });

  it("does not count header auth without the acknowledgement", () => {
    // The mode alone is not the opt-out; trusting the caller's role header is.
    const posture = computeSecurityPosture(configFrom({ auth__mode: "header" }));
    expect(posture.dev_opt_outs).not.toContain("auth.header_mode_acknowledged");
  });

  it("defaults an absent IDP-failure setting to denying", () => {
    // Secure-by-default: a config that says nothing must not read as the
    // anonymous-viewer fallback.
    const config = configFrom({});
    delete (config.auth as Record<string, unknown>).webhook_idp_on_failure;
    expect(computeSecurityPosture(config).dev_opt_outs).not.toContain("auth.webhook_idp_on_failure=viewer");
  });

  it("defaults an absent signing setting to required", () => {
    // Also secure-by-default: a config predating the field is not thereby
    // running unsigned.
    const config = configFrom({ auth__identity_provider: "webhook" });
    delete (config.auth as Record<string, unknown>).webhook_idp_require_signed_response;
    expect(computeSecurityPosture(config).dev_opt_outs).not.toContain("auth.webhook_idp_require_signed_response=false");
  });

  it("defaults an absent identity provider to the local one", () => {
    // A config that names no provider is not using the webhook one, so the
    // webhook warnings do not apply to it.
    const config = configFrom({});
    delete (config.auth as Record<string, unknown>).identity_provider;
    delete (config.auth as Record<string, unknown>).webhook_idp_require_signed_response;
    const posture = computeSecurityPosture(config);
    expect(posture.warnings.some((warning) => warning.includes("webhook IdP"))).toBe(false);
  });

  it("treats a config that carries no audit section at all as unenabled", () => {
    // `audit` is optional on PostureConfig — an embedder's config object may
    // not carry the section at all, not merely carry it with the field unset.
    // Reading `config.audit.chain_enabled` without the `?.` would throw on
    // `undefined` instead of reporting the compliance gap.
    const config = configFrom({}) as unknown as Record<string, unknown>;
    delete config.audit;
    expect(() => computeSecurityPosture(config as unknown as PostureConfig)).not.toThrow();
    expect(computeSecurityPosture(config as unknown as PostureConfig).audit_chain_enabled).toBe(false);
  });

  it("always reports replay protection as on", () => {
    // The per-instance cache is unconditional; the nonce binding that extends
    // it to several nodes is surfaced as a warning instead.
    expect(computeSecurityPosture(baseConfig()).idp_response_replay_protected).toBe(true);
  });
});
