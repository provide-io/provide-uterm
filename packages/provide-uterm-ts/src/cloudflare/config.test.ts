//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { DEFAULT_AUTO_TRANSFER_IDLE_S } from "../deckmux/index.ts";
import { loadGolden } from "../testing/golden.ts";
import { type CloudflareConfig, CloudflareConfigError, configFromEnv, defaultConfig } from "./index.ts";

interface ConfigGolden {
  good_token: string;
  defaults: CloudflareConfig;
  valid: Array<{ name: string; env: Record<string, string>; config: CloudflareConfig }>;
  invalid: Array<{ name: string; env: Record<string, string>; type: string; message: string }>;
  non_string: Array<{ name: string; env: Record<string, unknown>; config: CloudflareConfig }>;
  from_vars_attribute: CloudflareConfig;
  min_bearer_token_chars: number;
}

const golden = loadGolden<ConfigGolden>("cfconfig_golden.json");

/** The recorded configuration for a named case. */
function validCase(name: string) {
  return golden.valid.find((entry) => entry.name === name);
}

/** The recorded refusal for a named case. */
function invalidCase(name: string) {
  return golden.invalid.find((entry) => entry.name === name);
}

/** An environment that differs from the minimum by one setting. */
function envWith(overrides: Record<string, string>): Record<string, string> {
  return { WORKER_BEARER_TOKEN: golden.good_token, ...overrides };
}

describe("reading a configuration", () => {
  it.each(golden.valid)("$name", (record) => {
    expect(configFromEnv(record.env)).toStrictEqual(record.config);
  });

  it("matches the reference's defaults", () => {
    expect(defaultConfig()).toStrictEqual(golden.defaults);
  });

  it("reads an environment that carries its variables under `vars`", () => {
    // Which is the shape a Worker binding arrives in.
    expect(configFromEnv({ vars: envWith({}) })).toStrictEqual(golden.from_vars_attribute);
  });

  it("needs nothing but a bearer token", () => {
    expect(configFromEnv(envWith({}))).toStrictEqual(validCase("only what is required")?.config);
  });
});

describe("refusing to start", () => {
  it.each(golden.invalid)("$name", (record) => {
    expect(() => configFromEnv(record.env)).toThrow(CloudflareConfigError);
  });

  it("demands a bearer token at all", () => {
    // A Worker is always internet-facing. There is no loopback to fall back
    // on, so this token is the outermost auth boundary the deployment has.
    expect(() => configFromEnv({})).toThrow(/WORKER_BEARER_TOKEN is required/);
    expect(() => configFromEnv({ WORKER_BEARER_TOKEN: "" })).toThrow(/required/);
  });

  it("refuses a known placeholder token", () => {
    // Unconditionally — not only in production. A placeholder on an edge
    // deployment is an open door whatever the environment says.
    for (const name of [
      "a placeholder bearer token",
      "a placeholder in different case",
      "a placeholder with padding",
      "a placeholder as a marker inside a longer token",
    ]) {
      const record = invalidCase(name);
      expect(() => configFromEnv(record?.env as Record<string, string>)).toThrow(/known placeholder/);
    }
  });

  it("refuses a placeholder the substring markers would miss", () => {
    // Two separate checks: an exact list of short generic words, and a
    // shorter list of compound phrases matched anywhere. "worker-secret" is
    // only in the first, so dropping it would let it through.
    for (const name of ["an exact placeholder, not a marker", "another exact placeholder"]) {
      const record = invalidCase(name);
      expect(() => configFromEnv(record?.env as Record<string, string>)).toThrow(/known placeholder/);
    }
  });

  it("trims a token before judging it", () => {
    // Padding is not entropy. An exact placeholder wrapped in spaces is the
    // case that tells trimming apart from the substring markers, which would
    // have found it either way.
    expect(() => configFromEnv({ WORKER_BEARER_TOKEN: "   worker-secret   " })).toThrow(/known placeholder/);
  });

  it("refuses a token below the entropy floor", () => {
    expect(() => configFromEnv(envWith({ WORKER_BEARER_TOKEN: "abc123" }))).toThrow(/at least 32 characters/);
    // One character short is still short.
    expect(() => configFromEnv(envWith({ WORKER_BEARER_TOKEN: golden.good_token.slice(0, -1) }))).toThrow(/at least/);
    expect(golden.good_token).toHaveLength(golden.min_bearer_token_chars);
    expect(() => configFromEnv(envWith({}))).not.toThrow();
  });

  it("refuses every auth mode but jwt", () => {
    // dev and none would be an admin bypass on a Worker regardless of
    // environment, so they are gone rather than gated.
    for (const mode of ["none", "dev", "header"]) {
      expect(() => configFromEnv(envWith({ AUTH_MODE: mode }))).toThrow(/must be 'jwt'/);
    }
  });

  it("refuses HMAC combined with anything asymmetric", () => {
    // With both an HS* algorithm and an asymmetric key or JWKS URL, a token
    // can be forged by using the public key bytes as the HMAC secret. The
    // resulting deployment looks fine and accepts forged tokens.
    for (const name of [
      "HMAC combined with an asymmetric algorithm",
      "HMAC combined with a JWKS URL",
      "HMAC combined with a public key PEM",
      "HMAC combined with a certificate",
    ]) {
      const record = invalidCase(name);
      expect(() => configFromEnv(record?.env as Record<string, string>)).toThrow(
        /algorithm-confusion|must not combine/,
      );
    }
  });

  it("allows HMAC on its own", () => {
    // The rule is about the combination. A deployment using only shared
    // secrets is not at risk from it.
    expect(validCase("an HMAC algorithm on its own")).toBeDefined();
    expect(validCase("several HMAC algorithms")).toBeDefined();
    expect(() => configFromEnv(envWith({ JWT_ALGORITHMS: "HS256" }))).not.toThrow();
  });

  it("does not mistake a raw shared secret for an asymmetric key", () => {
    // A PEM marker is what makes a key asymmetric. An HMAC secret has none,
    // so refusing it would block a legitimate configuration.
    expect(() => configFromEnv(envWith({ JWT_ALGORITHMS: "HS256", JWT_PUBLIC_KEY_PEM: "sekrit" }))).not.toThrow();
  });

  it("refuses a numeric setting that is not a number", () => {
    // Parsed the way the reference parses it, which raises rather than
    // coercing. A Worker that started with a silently-zeroed limit would have
    // the protection disabled and nothing to show for it.
    expect(() => configFromEnv(envWith({ MAX_WS_MESSAGE_BYTES: "lots" }))).toThrow(CloudflareConfigError);
    expect(() => configFromEnv(envWith({ MAX_INPUT_CHARS: "1000.5" }))).toThrow(CloudflareConfigError);
    expect(() => configFromEnv(envWith({ UPSTREAM_HEARTBEAT_S: "" }))).toThrow(CloudflareConfigError);
    expect(() => configFromEnv(envWith({ BACKPRESSURE_ACK_GRACE_S: "soon" }))).toThrow(CloudflareConfigError);
  });

  it("accepts a fractional grace period, which is a float", () => {
    // Unlike the byte counts, this one is genuinely fractional.
    expect(configFromEnv(envWith({ BACKPRESSURE_ACK_GRACE_S: "20.5" })).limits.backpressure_ack_grace_s).toBe(20.5);
  });
});

describe("bindings that are not strings", () => {
  it.each(golden.non_string)("$name", (record) => {
    // A Worker binding may arrive as a number or a boolean; the reference
    // stringifies whatever it finds. A null means the variable is unset.
    expect(configFromEnv(record.env)).toStrictEqual(record.config);
  });

  it("reads a number as its text", () => {
    expect(
      configFromEnv({ WORKER_BEARER_TOKEN: golden.good_token, MAX_INPUT_CHARS: 20000 }).limits.max_input_chars,
    ).toBe(20000);
  });

  it("reads a boolean as its text", () => {
    // `True` stringifies to "True", which lower-cases into the truthy set.
    expect(configFromEnv({ WORKER_BEARER_TOKEN: golden.good_token, TUNNEL_IP_BINDING: true }).tunnel_ip_binding).toBe(
      true,
    );
  });

  it("treats a null binding as an unset one", () => {
    // Rather than as the text "null", which would become an issuer nothing
    // matches or a limit that fails to parse.
    expect(configFromEnv({ WORKER_BEARER_TOKEN: golden.good_token, JWT_ISSUER: null }).jwt.issuer).toBeNull();
    expect(
      configFromEnv({ WORKER_BEARER_TOKEN: golden.good_token, MAX_INPUT_CHARS: null }).limits.max_input_chars,
    ).toBe(10000);
  });
});

describe("clamping what it was given", () => {
  it("raises every setting to its floor", () => {
    // A zero-byte message limit or a one-second token lifetime disables a
    // protection. Clamping keeps it while letting an operator raise it.
    const clamped = validCase("numbers below their floors")?.config as CloudflareConfig;
    expect(configFromEnv(validCase("numbers below their floors")?.env as Record<string, string>)).toStrictEqual(
      clamped,
    );
    expect(clamped.limits.max_ws_message_bytes).toBe(1024);
    expect(clamped.limits.max_input_chars).toBe(100);
    expect(clamped.limits.max_events_per_worker).toBe(100);
    expect(clamped.limits.max_buffer_bytes).toBe(1024);
    expect(clamped.limits.backpressure_high_water_bytes).toBe(1024);
    expect(clamped.upstream.connect_timeout_ms).toBe(100);
    expect(clamped.upstream.heartbeat_s).toBe(1);
    expect(clamped.upstream.max_backoff_s).toBe(1);
    expect(clamped.tunnel_token_ttl_s).toBe(60);
    expect(clamped.resume_ttl_s).toBe(30);
    expect(clamped.deckmux_auto_transfer_idle_s).toBe(1);
  });

  it("lets the ones with a zero floor reach zero", () => {
    // The low-water mark and the acknowledgement grace are allowed to be
    // nothing; they are not protections in the way a size ceiling is.
    const clamped = validCase("numbers below their floors")?.config as CloudflareConfig;
    expect(clamped.limits.backpressure_low_water_bytes).toBe(0);
    expect(clamped.limits.backpressure_ack_grace_s).toBe(0);
    expect(clamped.jwt.clock_skew_seconds).toBe(0);
  });

  it("leaves a setting above its floor alone", () => {
    expect(configFromEnv(envWith({ MAX_INPUT_CHARS: "20000" })).limits.max_input_chars).toBe(20000);
  });
});

describe("falling back", () => {
  it("keeps an unrecognised security mode strict", () => {
    // The safe end of the range, so a typo does not quietly loosen headers.
    expect(validCase("an unknown security mode falls back to strict")?.config.security_mode).toBe("strict");
    expect(validCase("an empty security mode falls back to strict")?.config.security_mode).toBe("strict");
    expect(configFromEnv(envWith({ SECURITY_MODE: "wide-open" })).security_mode).toBe("strict");
  });

  it("reads the modes without regard to case", () => {
    expect(configFromEnv(envWith({ SECURITY_MODE: "DEV" })).security_mode).toBe("dev");
    expect(configFromEnv(envWith({ AUTH_MODE: "JWT" })).jwt.mode).toBe("jwt");
  });

  it("falls back to RS256 when no algorithm survives parsing", () => {
    // An empty list would accept nothing at all, which is a Worker that
    // rejects every token rather than one that is secure.
    expect(configFromEnv(envWith({ JWT_ALGORITHMS: " , , " })).jwt.algorithms).toStrictEqual(["RS256"]);
  });

  it("trims the algorithms it is given", () => {
    expect(configFromEnv(envWith({ JWT_ALGORITHMS: " RS256 , ES384 " })).jwt.algorithms).toStrictEqual([
      "RS256",
      "ES384",
    ]);
  });

  it("turns an empty optional into nothing", () => {
    const config = configFromEnv(envWith({ JWT_ISSUER: "", JWT_AUDIENCE: "", JWT_JWKS_URL: "" }));
    expect(config.jwt.issuer).toBeNull();
    expect(config.jwt.audience).toBeNull();
    expect(config.jwt.jwks_url).toBeNull();
  });

  it("keeps a security header that was set to nothing", () => {
    // Present-but-empty and absent are different here: an operator writing an
    // empty CSP is switching the header off deliberately, and defaulting it
    // back on would override them.
    expect(configFromEnv(envWith({ SECURITY_CSP: "" })).security_csp).toBe("");
    expect(configFromEnv(envWith({})).security_csp).toBeNull();
  });

  it("falls back on empty names rather than using an empty one", () => {
    const config = configFromEnv(envWith({ JWT_ROLES_CLAIM: "", JWT_SCOPES_CLAIM: "", JWT_DEFAULT_ROLE: "" }));
    expect(config.jwt.jwt_roles_claim).toBe("roles");
    expect(config.jwt.jwt_scopes_claim).toBe("scope");
    expect(config.jwt.jwt_default_role).toBe("viewer");
  });

  it("falls back on an empty transport and queue mode", () => {
    expect(configFromEnv(envWith({ TUNNEL_TOKEN_TRANSPORT: "" })).tunnel_token_transport).toBe("cookie");
    expect(configFromEnv(envWith({ DECKMUX_KEYSTROKE_QUEUE: "" })).deckmux_keystroke_queue).toBe("display");
  });
});

describe("the role map", () => {
  it("reads an object of strings", () => {
    expect(configFromEnv(envWith({ JWT_ROLE_MAP: '{"engineering": "admin"}' })).jwt.jwt_role_map).toStrictEqual({
      engineering: "admin",
    });
  });

  it("stringifies whatever it was given", () => {
    // A rules file may quote its numbers or not; both mean the same mapping.
    expect(configFromEnv(envWith({ JWT_ROLE_MAP: '{"a": 1, "2": "admin"}' })).jwt.jwt_role_map).toStrictEqual({
      a: "1",
      "2": "admin",
    });
  });

  it("ignores one that is not usable rather than refusing to start", () => {
    // It maps IdP groups to roles. Losing it costs a mapping; refusing to
    // start over it costs the deployment.
    for (const value of ['["a", "b"]', "{not json", "   "]) {
      expect(configFromEnv(envWith({ JWT_ROLE_MAP: value })).jwt.jwt_role_map).toStrictEqual({});
    }
  });
});

describe("booleans", () => {
  it("reads the words that mean yes", () => {
    for (const value of ["1", "true", "yes", "y", "on", "TRUE", "Yes", "ON"]) {
      expect(configFromEnv(envWith({ TUNNEL_IP_BINDING: value })).tunnel_ip_binding).toBe(true);
    }
  });

  it("reads anything else as no", () => {
    for (const value of ["0", "false", "no", "maybe", ""]) {
      expect(configFromEnv(envWith({ TUNNEL_IP_BINDING: value })).tunnel_ip_binding).toBe(false);
    }
  });

  it("trims before reading", () => {
    // A variable set from a shell here-doc keeps its whitespace.
    expect(configFromEnv(envWith({ TUNNEL_IP_BINDING: "  yes  " })).tunnel_ip_binding).toBe(true);
    expect(configFromEnv(envWith({ RESUME_ENABLED: "  NO  " })).resume_enabled).toBe(false);
  });

  it("keeps each default when the variable is absent", () => {
    // Resume is on and IP binding is off; the two defaults differ, so a
    // shared fallback would break one of them.
    const config = configFromEnv(envWith({}));
    expect(config.resume_enabled).toBe(true);
    expect(config.tunnel_ip_binding).toBe(false);
    expect(config.jwt.jwt_service_token_admin).toBe(false);
  });

  it("turns resume off when asked", () => {
    expect(configFromEnv(envWith({ RESUME_ENABLED: "no" })).resume_enabled).toBe(false);
  });
});

describe("settings shared with the rest of the port", () => {
  it("takes the DeckMux idle window from DeckMux", () => {
    // The Python packages cannot import from one another, so the reference
    // repeats this number. There is one package here and no reason to.
    expect(golden.defaults.deckmux_auto_transfer_idle_s).toBe(DEFAULT_AUTO_TRANSFER_IDLE_S);
    expect(configFromEnv(envWith({})).deckmux_auto_transfer_idle_s).toBe(DEFAULT_AUTO_TRANSFER_IDLE_S);
  });
});
