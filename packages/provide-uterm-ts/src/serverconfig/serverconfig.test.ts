//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  AUTH_DEFAULTS,
  cleanPath,
  derivePublicBaseUrl,
  LOOPBACK_HOSTS,
  requireSecureUrl,
  UI_DEFAULTS,
  validateAuditConfig,
  validateAuthConfig,
  validateControlPlaneConfig,
  validatePamConfig,
  validateRecordingConfig,
} from "./index.ts";

interface ServerConfigGolden {
  urls: Array<{ name: string; url: string | null; error: string | null }>;
  paths: Array<{ name: string; value: string; fallback: string; cleaned: string }>;
  auth: Array<{ name: string; kwargs: Record<string, unknown>; error: string | null }>;
  models: Array<{ name: string; model: string; kwargs: Record<string, unknown>; error: string | null }>;
  ui_defaults: Record<string, string>;
  bind: { derived_public_base_url: string; explicit_public_base_url: string };
  auth_defaults: Record<string, unknown>;
  loopback_hosts: string[];
}

const golden = loadGolden<ServerConfigGolden>("serverconfig_golden.json");

/** Run and return the refusal, or nothing when accepted. */
function refused(call: () => void): string | null {
  try {
    call();
  } catch (error) {
    return (error as Error).message;
  }
  return null;
}

/** The validator each recorded model case belongs to. */
const VALIDATORS: Record<string, (input: Record<string, unknown>) => void> = {
  AuditConfig: validateAuditConfig,
  RecordingConfig: validateRecordingConfig,
  ControlPlaneConfig: validateControlPlaneConfig,
  PamConfig: validatePamConfig,
};

describe("requireSecureUrl", () => {
  it.each(golden.urls)("$name", (record) => {
    // These channels carry HMAC secrets, auth headers and the keys used to
    // validate admin tokens. Cleartext to a routable host is not a warning.
    expect(refused(() => requireSecureUrl(record.url ?? undefined, "auth.webhook_idp_url"))).toBe(record.error);
  });

  it("always allows https", () => {
    for (const name of ["https", "https on a port"]) {
      expect(golden.urls.find((entry) => entry.name === name)?.error).toBeNull();
    }
  });

  it("allows cleartext only to loopback", () => {
    // Local development still works; nothing else gets to send a secret in
    // the clear.
    for (const name of ["http to loopback by name", "http to loopback by address", "http to ipv6 loopback"]) {
      expect(golden.urls.find((entry) => entry.name === name)?.error).toBeNull();
    }
    expect(golden.urls.find((entry) => entry.name === "http to a routable host")?.error).toContain("https://");
  });

  it("allows a .localhost subdomain but not a host that merely looks like one", () => {
    // `idp.localhost` resolves to loopback by convention; `notlocalhost` and
    // `localhost.evil.example` do not, and matching them would be an SSRF.
    expect(golden.urls.find((entry) => entry.name === "http to a .localhost name")?.error).toBeNull();
    expect(
      golden.urls.find((entry) => entry.name === "http to something that ends in localhost")?.error,
    ).not.toBeNull();
    expect(
      golden.urls.find((entry) => entry.name === "http to a host that merely contains localhost")?.error,
    ).not.toBeNull();
  });

  it("matches the loopback host without regard to case", () => {
    expect(golden.urls.find((entry) => entry.name === "uppercase loopback")?.error).toBeNull();
  });

  it("does not treat a private address as loopback", () => {
    // A LAN host is still somebody else's network.
    expect(golden.urls.find((entry) => entry.name === "http to a private address")?.error).not.toBeNull();
  });

  it("refuses a scheme that is neither", () => {
    // Refused outright rather than passed to a client that might do something
    // surprising with it.
    for (const name of ["a scheme that is neither", "no scheme at all", "a file url"]) {
      expect(golden.urls.find((entry) => entry.name === name)?.error).toContain("must use http(s)");
    }
  });

  it("has nothing to say about an absent url", () => {
    for (const name of ["empty", "none"]) {
      expect(golden.urls.find((entry) => entry.name === name)?.error).toBeNull();
    }
  });

  it("names the field it refused", () => {
    expect(refused(() => requireSecureUrl("http://evil.example", "pam.relay_url"))).toContain("pam.relay_url");
  });

  it("uses the recorded loopback hosts", () => {
    expect([...LOOPBACK_HOSTS].sort()).toStrictEqual(golden.loopback_hosts);
  });
});

describe("cleanPath", () => {
  it.each(golden.paths)("$name", (record) => {
    expect(cleanPath(record.value, record.fallback)).toBe(record.cleaned);
  });

  it("adds the leading slash a mount path needs", () => {
    // Without it the route is registered under a name nothing matches.
    expect(golden.paths.find((entry) => entry.name === "no leading slash")?.cleaned).toBe("/app");
  });

  it("drops trailing slashes", () => {
    for (const name of ["trailing slash", "several trailing slashes"]) {
      expect(golden.paths.find((entry) => entry.name === name)?.cleaned).toBe("/app");
    }
  });

  it("keeps the root as a slash rather than emptying it", () => {
    // Stripping the trailing slash off "/" would leave nothing at all.
    expect(golden.paths.find((entry) => entry.name === "just a slash")?.cleaned).toBe("/");
  });

  it("falls back when there is nothing to clean", () => {
    expect(golden.paths.find((entry) => entry.name === "empty falls back")?.cleaned).toBe("/fallback");
  });

  it("keeps the inner structure of a nested path", () => {
    expect(golden.paths.find((entry) => entry.name === "nested")?.cleaned).toBe("/a/b/c");
  });

  it("falls back on an explicit undefined, not just on an empty string", () => {
    // The ternary's first arm is `value === undefined`; a caller reaching
    // this with no value at all (rather than an empty string) must take the
    // same fallback branch, not fall through to `String(undefined)`, which
    // is the literal text "undefined".
    expect(cleanPath(undefined, "/fallback")).toBe("/fallback");
  });
});

describe("the auth configuration", () => {
  it.each(golden.auth)("$name", (record) => {
    expect(refused(() => validateAuthConfig(record.kwargs))).toBe(record.error);
  });

  it("refuses to require a secret it was not given", () => {
    // A server that starts here would reject every upstream identity frame at
    // runtime instead.
    for (const name of ["a proxy secret that is required and missing", "a proxy secret that is required and blank"]) {
      expect(golden.auth.find((entry) => entry.name === name)?.error).toContain("upstream_proxy_secret is required");
    }
  });

  it("does not mind a secret nobody asked for", () => {
    expect(golden.auth.find((entry) => entry.name === "a proxy secret nobody required")?.error).toBeNull();
  });

  it("refuses a combination that can never succeed", () => {
    // Verifying an IdP response signature without a shared secret is not
    // possible. Refusing at load time beats failing every request, or
    // silently not verifying.
    for (const name of [
      "a webhook idp that must sign, with no secret",
      "a webhook idp that must sign, with a blank secret",
    ]) {
      expect(golden.auth.find((entry) => entry.name === name)?.error).toContain("webhook_idp_secret");
    }
  });

  it("says how to resolve it", () => {
    const record = golden.auth.find((entry) => entry.name === "a webhook idp that must sign, with no secret");
    expect(record?.error).toContain("webhook_idp_require_signed_response=false");
  });

  it("does not ask a local identity provider for a webhook secret", () => {
    expect(golden.auth.find((entry) => entry.name === "a local idp with no secret")?.error).toBeNull();
  });

  it("lets a webhook that need not sign go without a secret", () => {
    expect(golden.auth.find((entry) => entry.name === "a webhook idp that need not sign")?.error).toBeNull();
  });

  it("checks both outbound urls", () => {
    // The JWKS endpoint validates admin tokens; over cleartext it validates
    // whatever a network attacker substitutes.
    expect(golden.auth.find((entry) => entry.name === "a cleartext webhook url")?.error).toContain(
      "auth.webhook_idp_url",
    );
    expect(golden.auth.find((entry) => entry.name === "a cleartext jwks url")?.error).toContain("auth.jwt_jwks_url");
  });

  it("refuses a field it does not know", () => {
    // A typo in a config file is otherwise a setting that silently does
    // nothing — including a security setting the operator believes is on.
    expect(golden.auth.find((entry) => entry.name === "an unknown field")?.error).toBe(
      "Extra inputs are not permitted",
    );
  });

  it("defaults to refusing an unverified identity", () => {
    // Every one of these defaults is the safe side of its choice.
    expect(AUTH_DEFAULTS.webhookIdpOnFailure).toBe(golden.auth_defaults.webhook_idp_on_failure);
    expect(AUTH_DEFAULTS.webhookIdpOnFailure).toBe("deny");
    expect(AUTH_DEFAULTS.webhookIdpRequireSignedResponse).toBe(
      golden.auth_defaults.webhook_idp_require_signed_response,
    );
    expect(AUTH_DEFAULTS.allowAdhocBrowserObservers).toBe(golden.auth_defaults.allow_adhoc_browser_observers);
  });

  it("matches the recorded defaults", () => {
    expect(AUTH_DEFAULTS.mode).toBe(golden.auth_defaults.mode);
    expect(AUTH_DEFAULTS.identityProvider).toBe(golden.auth_defaults.identity_provider);
    expect(AUTH_DEFAULTS.delegateRoles).toBe(golden.auth_defaults.delegate_roles);
    expect(AUTH_DEFAULTS.clockSkewSeconds).toBe(golden.auth_defaults.clock_skew_seconds);
    expect([...AUTH_DEFAULTS.jwtAlgorithms]).toStrictEqual(golden.auth_defaults.jwt_algorithms);
    expect([...AUTH_DEFAULTS.trustedProxyIps]).toStrictEqual(golden.auth_defaults.trusted_proxy_ips);
  });
});

describe("the other configurations", () => {
  it.each(golden.models)("$name", (record) => {
    const validate = VALIDATORS[record.model] as (input: Record<string, unknown>) => void;
    expect(refused(() => validate(record.kwargs))).toBe(record.error);
  });

  it("refuses an audit chain with nowhere to write", () => {
    // A chain that cannot write is a misconfiguration, not a silent no-op —
    // and the whole point of the chain is that it is tamper-evident.
    for (const name of ["an audit chain with nowhere to write", "an audit chain with a blank file"]) {
      expect(golden.models.find((entry) => entry.name === name)?.error).toContain("chain_file");
    }
  });

  it("treats zero as unlimited rather than as nothing", () => {
    // A recording size of zero keeps everything; a negative one is a typo.
    expect(golden.models.find((entry) => entry.name === "a recording size of zero")?.error).toBeNull();
    expect(golden.models.find((entry) => entry.name === "a negative recording size")?.error).toContain(">= 0");
    expect(golden.models.find((entry) => entry.name === "a retention of zero")?.error).toBeNull();
  });

  it("insists a reap interval is positive", () => {
    // Zero would be a loop with no delay in it.
    expect(golden.models.find((entry) => entry.name === "a reap interval of zero")?.error).toContain("> 0");
  });

  it("insists sqlite has somewhere to store", () => {
    expect(golden.models.find((entry) => entry.name === "sqlite with no database url")?.error).toContain(
      "database_url is required",
    );
  });

  it("holds the pam relay to the same url rule", () => {
    expect(golden.models.find((entry) => entry.name === "a pam relay over cleartext")?.error).toContain(
      "pam.relay_url",
    );
    expect(golden.models.find((entry) => entry.name === "a pam relay over tls")?.error).toBeNull();
  });

  it("quotes the value it refused", () => {
    expect(golden.models.find((entry) => entry.name === "a negative recording size")?.error).toContain("-1");
  });

  it("only bounds a recording size that is actually a number", () => {
    // The guard is `typeof === "number" && ... < 0`; a value of some other
    // type must never reach the numeric comparison at all — not even one
    // that a `<` coercion would happen to read as negative.
    expect(() => validateRecordingConfig({ max_bytes: "-5" })).not.toThrow();
    expect(() => validateRecordingConfig({ retention_s: "-5" })).not.toThrow();
  });

  it("names the recording webhook url specifically", () => {
    expect(() => validateRecordingConfig({ webhook_url: "http://evil.example" })).toThrow("recording.webhook_url");
  });

  it("only bounds a reap interval or retention that is actually a number", () => {
    expect(() => validateControlPlaneConfig({ reap_interval_s: "-5" })).not.toThrow();
    expect(() => validateControlPlaneConfig({ reap_retention_s: "-5" })).not.toThrow();
  });

  it("does not refuse every reap interval, only a non-positive one", () => {
    // A guard that always fired on any number, however positive, would still
    // pass every existing "zero or negative" case — none of them exercise a
    // value the reference actually accepts.
    expect(() => validateControlPlaneConfig({ reap_interval_s: 3600 })).not.toThrow();
  });
});

describe("the UI defaults", () => {
  it("mounts where the reference mounts", () => {
    expect(UI_DEFAULTS.appPath).toBe(golden.ui_defaults.app_path);
    expect(UI_DEFAULTS.assetsPath).toBe(golden.ui_defaults.assets_path);
  });

  it("normalises a path the operator gave loosely", () => {
    expect(cleanPath("app/", UI_DEFAULTS.appPath)).toBe(golden.ui_defaults.normalised_app_path);
    expect(cleanPath("assets/", UI_DEFAULTS.assetsPath)).toBe(golden.ui_defaults.normalised_assets_path);
  });
});

describe("derivePublicBaseUrl", () => {
  it("derives one from the bind when none is given", () => {
    // Something has to go in the links the server hands out.
    expect(derivePublicBaseUrl("0.0.0.0", 8080)).toBe(golden.bind.derived_public_base_url);
  });

  it("keeps an explicit one", () => {
    // Behind a proxy the bind address is not what a browser can reach.
    expect(derivePublicBaseUrl("0.0.0.0", 8080, "https://uterm.example.org")).toBe(
      golden.bind.explicit_public_base_url,
    );
  });

  it("treats an empty one as absent", () => {
    expect(derivePublicBaseUrl("0.0.0.0", 8080, "")).toBe(golden.bind.derived_public_base_url);
  });
});
