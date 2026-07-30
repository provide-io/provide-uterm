//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it, vi } from "vitest";
import { bootstrapServer } from "../server/bootstrap.ts";
import {
  effectiveAllowLoopbackDestinations,
  isLoopbackHost,
  validateWebhookUrl,
  WEBHOOK_DNS_TIMEOUT_S,
  WebhookUrlError,
  webhookDeliveryAllowed,
} from "./index.ts";

/**
 * What each recorded host resolves to. `undefined` means it will not resolve.
 *
 * Every test resolves through this table rather than the platform: a guard
 * whose test suite depends on a real resolver is a guard whose test suite
 * reports the resolver's weather.
 */
const RESOLUTIONS: Record<string, string[] | undefined> = {
  "hook.example.org": ["93.184.216.34"],
  "rebind.example": ["169.254.169.254"],
  "internal.example": ["10.0.0.1"],
  "mixed.example": ["93.184.216.34", "10.0.0.1"],
  "loopback.example": ["127.0.0.1"],
  "empty.example": [],
  "broken.example": undefined,
  "garbage.example": ["not-an-address"],
};

/** A resolver over the recorded table, counting how often it was asked. */
function resolver() {
  const calls: string[] = [];
  return {
    calls,
    resolve: async (host: string) => {
      calls.push(host);
      const addresses = RESOLUTIONS[host];
      if (addresses === undefined) {
        throw new Error(`cannot resolve ${host}`);
      }
      return addresses;
    },
  };
}

/** Await and return the refusal message, or nothing when accepted. */
async function refusal(call: () => Promise<unknown>): Promise<string | null> {
  try {
    await call();
  } catch (error) {
    return (error as Error).message;
  }
  return null;
}

/**
 * The permission an operator's configuration actually produces: document in,
 * through the production factory, out the other side the effective answer.
 *
 * `dev_token` is the default auth mode and is refused on a routable bind, so
 * the routable rows say `jwt` — which is what an operator binding `0.0.0.0`
 * has to say too.
 */
function factoryAllowsLoopback(document: Record<string, unknown>): boolean {
  return bootstrapServer({
    document,
    authMode: String((document.server as Record<string, unknown> | undefined)?.host ?? "127.0.0.1").startsWith("127.")
      ? "dev_token"
      : "jwt",
  }).allowLoopbackDestinations;
}

/** A registration-time guard as that configuration would give an operator. */
function guard(document: Record<string, unknown>) {
  const allowLoopbackDestinations = factoryAllowsLoopback(document);
  const { resolve, calls } = resolver();
  return {
    calls,
    allowLoopbackDestinations,
    validate: (url: string) => validateWebhookUrl(url, { allowLoopbackDestinations, resolve }),
  };
}

/** The two binds the matrix distinguishes, and the key on and off. */
const LOOPBACK_BIND = { server: { host: "127.0.0.1" } };
const ROUTABLE_BIND = { server: { host: "0.0.0.0" } };
const KEY_SET = { webhooks: { allow_loopback_destinations: true } };

describe("isLoopbackHost", () => {
  it("names the hosts the reference names", () => {
    // Ported from `_LOOPBACK_HOSTS` in server/app/auth.py: exactly these three
    // spellings, matched after trimming and lowercasing.
    for (const host of ["127.0.0.1", "localhost", "::1", " LOCALHOST ", "::1 "]) {
      expect(isLoopbackHost(host)).toBe(true);
    }
  });

  it("refuses a routable bind and a name that is not loopback", () => {
    for (const host of ["0.0.0.0", "10.0.0.1", "example.com", "", "127.0.0.2"]) {
      expect(isLoopbackHost(host)).toBe(false);
    }
  });
});

describe("effectiveAllowLoopbackDestinations", () => {
  it("is the key or the bind", () => {
    // §3 of conformance/EGRESS_GUARD.md, as a truth table.
    expect(effectiveAllowLoopbackDestinations({ webhooks: {}, server: { host: "127.0.0.1" } })).toBe(true);
    expect(effectiveAllowLoopbackDestinations({ webhooks: {}, server: { host: "0.0.0.0" } })).toBe(false);
    expect(
      effectiveAllowLoopbackDestinations({
        webhooks: { allow_loopback_destinations: true },
        server: { host: "0.0.0.0" },
      }),
    ).toBe(true);
    expect(
      effectiveAllowLoopbackDestinations({
        webhooks: { allow_loopback_destinations: false },
        server: { host: "127.0.0.1" },
      }),
    ).toBe(true);
  });

  it("reads only `true` as the key being set", () => {
    // A document that wrote a string there is `serverconfig`'s to refuse; this
    // must not read `"false"` as permission on the way past.
    expect(
      effectiveAllowLoopbackDestinations({
        webhooks: { allow_loopback_destinations: "false" },
        server: { host: "0.0.0.0" },
      }),
    ).toBe(false);
  });
});

describe("the production factory computes the permission once", () => {
  it("derives it from the bind when the key is unset", () => {
    expect(guard(LOOPBACK_BIND).allowLoopbackDestinations).toBe(true);
    expect(guard(ROUTABLE_BIND).allowLoopbackDestinations).toBe(false);
  });

  it("derives it from the key on a routable bind", () => {
    expect(guard({ ...ROUTABLE_BIND, ...KEY_SET }).allowLoopbackDestinations).toBe(true);
  });
});

describe("the required matrix", () => {
  it("accepts a loopback destination on a loopback bind with the key unset", async () => {
    // The default bind is 127.0.0.1. Refusing loopback there protects nothing
    // — no remote caller can reach the listener — and breaks every single-box
    // deployment.
    expect(await refusal(() => guard(LOOPBACK_BIND).validate("http://127.0.0.1:9000/hook"))).toBeNull();
    expect(await refusal(() => guard(LOOPBACK_BIND).validate("http://localhost:9000/hook"))).toBeNull();
  });

  it("refuses a loopback destination on a routable bind with the key unset", async () => {
    const it_ = guard(ROUTABLE_BIND);
    expect(await refusal(() => it_.validate("http://127.0.0.1:9000/hook"))).toBe("webhook url host is not allowed");
    expect(await refusal(() => it_.validate("http://localhost:9000/hook"))).toBe("webhook url host is not allowed");
    expect(await refusal(() => it_.validate("http://[::1]:9000/hook"))).toBe("webhook url host is not allowed");
  });

  it("accepts a loopback destination on a routable bind once the key is set", async () => {
    const it_ = guard({ ...ROUTABLE_BIND, ...KEY_SET });
    expect(await refusal(() => it_.validate("http://127.0.0.1:9000/hook"))).toBeNull();
    expect(await refusal(() => it_.validate("http://localhost:9000/hook"))).toBeNull();
  });

  it("refuses every metadata address, with the key and without it", async () => {
    // There is deliberately no knob that re-opens these: reaching one hands
    // out cloud credentials.
    for (const document of [LOOPBACK_BIND, ROUTABLE_BIND, { ...ROUTABLE_BIND, ...KEY_SET }]) {
      for (const url of [
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
        "http://[fd00:ec2::254]/token",
      ]) {
        expect(await refusal(() => guard(document).validate(url))).toBe("webhook url host is not allowed");
      }
    }
  });

  it("refuses the private ranges, with the key and without it", async () => {
    // The key is about loopback only. A private range is refused regardless:
    // a service there at least chose a routable interface, but it is still
    // somewhere the caller could not reach on their own.
    for (const document of [LOOPBACK_BIND, { ...ROUTABLE_BIND, ...KEY_SET }]) {
      for (const url of ["https://10.0.0.1/hook", "https://192.168.1.10/hook", "https://172.16.0.1/hook"]) {
        expect(await refusal(() => guard(document).validate(url))).toBe("webhook url host is not allowed");
      }
    }
  });

  it("refuses metadata.google.internal", async () => {
    // A name, not an address: it resolves to 169.254.169.254 on GCE, and a
    // resolver that answered otherwise would let it past an address check.
    for (const document of [LOOPBACK_BIND, { ...ROUTABLE_BIND, ...KEY_SET }]) {
      const it_ = guard(document);
      expect(await refusal(() => it_.validate("http://metadata.google.internal/computeMetadata/v1/"))).toBe(
        "webhook url host is not allowed",
      );
      // Refused before anything is asked of the resolver, so no answer can
      // change the outcome.
      expect(it_.calls).toStrictEqual([]);
    }
  });

  it("refuses a wrapped metadata address", async () => {
    // In a NAT64 cluster this really does reach the v4 metadata service, and
    // it is past any check that only looks at the outer address.
    expect(await refusal(() => guard(LOOPBACK_BIND).validate("http://[64:ff9b::169.254.169.254]/token"))).toBe(
      "webhook url host is not allowed",
    );
    for (const url of ["http://[::ffff:169.254.169.254]/t", "http://[2002:a9fe:a9fe::]/t", "http://[::10.0.0.1]/t"]) {
      expect(await refusal(() => guard(LOOPBACK_BIND).validate(url))).toBe("webhook url host is not allowed");
    }
  });

  it("refuses a name that resolves into private space", async () => {
    // DNS rebinding: the name is innocuous and the answer is not.
    expect(await refusal(() => guard(LOOPBACK_BIND).validate("https://internal.example/hook"))).toBe(
      "webhook url host is not allowed",
    );
    expect(await refusal(() => guard(LOOPBACK_BIND).validate("https://rebind.example/hook"))).toBe(
      "webhook url host is not allowed",
    );
  });

  it("checks every address a name answers with", async () => {
    // One good address does not make the name safe — a rebinding reply puts
    // the private address in the same response as the public one.
    expect(await refusal(() => guard(LOOPBACK_BIND).validate("https://mixed.example/hook"))).toBe(
      "webhook url host is not allowed",
    );
  });

  it("refuses a name that will not resolve, and one that answers with nothing", async () => {
    // Both are "no usable address". Passing either would mean a hostile — or
    // merely broken — resolver turns the guard off.
    expect(await refusal(() => guard(LOOPBACK_BIND).validate("https://broken.example/hook"))).toBe(
      "webhook url host could not be resolved",
    );
    expect(await refusal(() => guard(LOOPBACK_BIND).validate("https://empty.example/hook"))).toBe(
      "webhook url host could not be resolved",
    );
  });

  it("accepts a public destination", async () => {
    // The row that proves the guard is not simply stuck closed.
    const it_ = guard(ROUTABLE_BIND);
    expect(await refusal(() => it_.validate("https://hook.example.org/hook"))).toBeNull();
    expect(await refusal(() => it_.validate("https://93.184.216.34/hook"))).toBeNull();
    expect(await refusal(() => it_.validate("http://hook.example.org:8080/hook?x=1"))).toBeNull();
  });
});

describe("validateWebhookUrl", () => {
  it("returns the url it was given", async () => {
    // The reference returns the URL so a caller can assign the validated
    // value back; normalising it here would change what gets POSTed.
    const { resolve } = resolver();
    const url = "https://hook.example.org/hook?x=1#f";
    expect(await validateWebhookUrl(url, { resolve })).toBe(url);
  });

  it("refuses loopback when asked nothing", async () => {
    // A library-level default of "allowed" would hand an embedder who has not
    // considered egress the permissive posture.
    const { resolve } = resolver();
    expect(await refusal(() => validateWebhookUrl("http://127.0.0.1/hook", { resolve }))).toBe(
      "webhook url host is not allowed",
    );
  });

  it("refuses a scheme that is not http or https", async () => {
    const { resolve } = resolver();
    for (const url of ["ftp://hook.example.org/hook", "ws://hook.example.org/hook", "gopher://x/y"]) {
      expect(await refusal(() => validateWebhookUrl(url, { resolve }))).toBe("webhook url must use http or https");
    }
  });

  it("refuses a url with no host", async () => {
    const { resolve } = resolver();
    expect(await refusal(() => validateWebhookUrl("file:///etc/passwd", { resolve }))).toBe(
      "webhook url must use http or https",
    );
    expect(await refusal(() => validateWebhookUrl("http:///hook", { resolve }))).toBe(
      "webhook url must include a host",
    );
    expect(await refusal(() => validateWebhookUrl("http://", { resolve }))).toBe("webhook url must include a host");
  });

  it("reads the scheme case-insensitively", async () => {
    // `urlparse` lowercases the scheme, so the reference accepts this. A check
    // that did not would refuse a working URL — and, worse, a check that read
    // the scheme case-sensitively somewhere *else* would be a way past this
    // one.
    const { resolve } = resolver();
    expect(await validateWebhookUrl("HTTPS://hook.example.org/hook", { resolve })).toBe(
      "HTTPS://hook.example.org/hook",
    );
    expect(await refusal(() => validateWebhookUrl("HTTP://169.254.169.254/latest/", { resolve }))).toBe(
      "webhook url host is not allowed",
    );
  });

  it("refuses an authority nothing can be made of", async () => {
    // A bracket form that is not an address, a space inside the host, a null
    // byte: the authority is there but unusable, and the reference's
    // `parsed.hostname` raises rather than answering.
    const { resolve } = resolver();
    for (const url of ["http://[bad]/hook", "http://ho st/x", "http://%00/x"]) {
      expect(await refusal(() => validateWebhookUrl(url, { resolve }))).toBe("webhook url must include a host");
    }
  });

  it("refuses a resolver answer that is not an address", async () => {
    // A resolver — or a stub standing in for one — that answers with
    // something unparseable must fail closed, where the reference's
    // `ipaddress.ip_address` raises and the caller turns that into a refusal.
    const { resolve } = resolver();
    expect(await refusal(() => validateWebhookUrl("https://garbage.example/hook", { resolve }))).toBe(
      "webhook url host is not allowed",
    );
  });

  it("reads the host out from behind userinfo", async () => {
    // `http://hook.example.org@169.254.169.254/` names the metadata service,
    // and a check that took everything before the first `/` as the host would
    // see the innocuous name.
    const { resolve } = resolver();
    expect(await refusal(() => validateWebhookUrl("http://hook.example.org@169.254.169.254/x", { resolve }))).toBe(
      "webhook url host is not allowed",
    );
  });

  it("refuses something that is not a url at all", async () => {
    // Unlike the connector peer check, this input comes from whoever is
    // registering a webhook, so a refusal is the answer rather than a bug
    // report.
    const { resolve } = resolver();
    for (const url of ["", "not a url", "hook.example.org/hook"]) {
      expect(await refusal(() => validateWebhookUrl(url, { resolve }))).toBe("webhook url must use http or https");
    }
  });

  it("raises its own error type", async () => {
    const { resolve } = resolver();
    await expect(validateWebhookUrl("http://127.0.0.1/hook", { resolve })).rejects.toThrow(WebhookUrlError);
    // The class name, not merely membership: distinct from EgressBlockedError
    // is the whole reason this module has its own error type.
    try {
      await validateWebhookUrl("http://127.0.0.1/hook", { resolve });
      expect.unreachable("expected validateWebhookUrl to reject");
    } catch (error) {
      expect((error as Error).name).toBe("WebhookUrlError");
    }
  });

  it("honors the full timeout window, not a thousandth of it", async () => {
    // The deadline converts seconds to milliseconds by multiplying. A
    // resolver that answers well within the real window, but not within a
    // thousandth of it, must still succeed.
    const slow = () => new Promise<string[]>((resolve) => setTimeout(() => resolve(["93.184.216.34"]), 20));
    expect(
      await refusal(() => validateWebhookUrl("https://slow.example/hook", { resolve: slow, timeoutS: 1 })),
    ).toBeNull();
  });

  it("uses the default timeout when none is given, not none at all", async () => {
    // `??` must fall back only on null/undefined. A caller who never set
    // timeoutS gets WEBHOOK_DNS_TIMEOUT_S (2s) — not a deadline computed from
    // `undefined`, which multiplies to NaN and fires before anything answers.
    const slow = () => new Promise<string[]>((resolve) => setTimeout(() => resolve(["93.184.216.34"]), 20));
    expect(await refusal(() => validateWebhookUrl("https://slow.example/hook", { resolve: slow }))).toBeNull();
  });

  it("clears its deadline timer once the race is already settled", async () => {
    // A cleared deadline and one left dangling look identical in the return
    // value — both resolve the same URL — but a real timer that outlives the
    // call it was guarding is a handle leak. Fake timers make the pending
    // handle countable.
    vi.useFakeTimers();
    try {
      const { resolve } = resolver();
      await validateWebhookUrl("https://hook.example.org/hook", { resolve });
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("strips a trailing dot and lowercases before deciding", async () => {
    // `metadata.google.internal.` is the same name to a resolver, and an
    // uppercase spelling is the same name to DNS.
    const { resolve } = resolver();
    for (const url of [
      "http://metadata.google.internal./x",
      "http://METADATA.GOOGLE.INTERNAL/x",
      "http://Metadata.Google.Internal../x",
    ]) {
      expect(await refusal(() => validateWebhookUrl(url, { resolve }))).toBe("webhook url host is not allowed");
    }
  });

  it("treats localhost and any name under it as loopback", async () => {
    // `*.localhost` is reserved for loopback (RFC 6761) and resolvers answer
    // 127.0.0.1 for it, so a check that only knew the bare name would be past.
    const { resolve } = resolver();
    for (const host of ["localhost", "api.localhost", "LOCALHOST", "deep.nested.localhost", "localhost."]) {
      expect(await refusal(() => validateWebhookUrl(`http://${host}/hook`, { resolve }))).toBe(
        "webhook url host is not allowed",
      );
      expect(
        await refusal(() => validateWebhookUrl(`http://${host}/hook`, { resolve, allowLoopbackDestinations: true })),
      ).toBeNull();
    }
    // Not under `localhost`, whatever it looks like.
    expect(await refusal(() => validateWebhookUrl("http://notlocalhost/hook", { resolve }))).toBe(
      "webhook url host could not be resolved",
    );
  });

  it("does not resolve a literal address", async () => {
    // There is nothing to look up, and a lookup would be a rebinding window.
    const { calls, resolve } = resolver();
    await refusal(() => validateWebhookUrl("https://10.0.0.1/hook", { resolve }));
    await validateWebhookUrl("https://93.184.216.34/hook", { resolve });
    expect(calls).toStrictEqual([]);
  });

  it("does not resolve localhost either", async () => {
    // The name is decided by rule, so a resolver answering something public
    // for it cannot re-open the loopback case.
    const { calls, resolve } = resolver();
    expect(await refusal(() => validateWebhookUrl("http://localhost/hook", { resolve }))).toBe(
      "webhook url host is not allowed",
    );
    expect(calls).toStrictEqual([]);
  });

  it("accepts a loopback address a name resolved to once the key is set", async () => {
    // The rule is about the destination, not about how it was spelled.
    const { resolve } = resolver();
    expect(await refusal(() => validateWebhookUrl("https://loopback.example/hook", { resolve }))).toBe(
      "webhook url host is not allowed",
    );
    expect(
      await refusal(() =>
        validateWebhookUrl("https://loopback.example/hook", { resolve, allowLoopbackDestinations: true }),
      ),
    ).toBeNull();
  });

  it("gives up on a resolver that never answers", async () => {
    // A hostile resolver that simply holds the connection open would
    // otherwise hang registration for as long as it liked.
    const never = async () => new Promise<string[]>(() => {});
    expect(
      await refusal(() => validateWebhookUrl("https://slow.example/hook", { resolve: never, timeoutS: 0.01 })),
    ).toBe("webhook url host could not be resolved");
  });

  it("bounds resolution by default", async () => {
    // Ported from `_REGISTER_DNS_TIMEOUT_S` in server/webhooks.py.
    expect(WEBHOOK_DNS_TIMEOUT_S).toBe(2.0);
  });

  it("resolves through the platform when given no resolver", async () => {
    // Every other case injects one; without this the real path could be
    // broken and the suite would not notice. `localhost.` is decided without
    // the network, so this is the address form — from the hosts file, which
    // needs no network either.
    await expect(validateWebhookUrl("http://127.0.0.1/hook", { allowLoopbackDestinations: true })).resolves.toBe(
      "http://127.0.0.1/hook",
    );
    expect(await refusal(() => validateWebhookUrl("http://this-name-does-not-exist.invalid/hook"))).toBe(
      "webhook url host could not be resolved",
    );
  });
});

describe("webhookDeliveryAllowed", () => {
  /**
   * A metric sink and the delivery predicate over it.
   *
   * The permission comes from the production factory, as it does at
   * registration: the delivery-time rule is an *additional* refusal on top of
   * §3, and pinning the flag by hand here would not show that.
   */
  function delivery(document: Record<string, unknown>, options: { tunnelShared?: boolean } = {}) {
    const seen: Array<[string, number]> = [];
    const { resolve } = resolver();
    return {
      seen,
      allowed: (url: string) =>
        webhookDeliveryAllowed(url, {
          ...options,
          allowLoopbackDestinations: factoryAllowsLoopback(document),
          resolve,
          onMetric: (name, value) => seen.push([name, value]),
        }),
    };
  }

  it("answers rather than throwing", async () => {
    // Delivery is a background task: a refusal is a counter and a log line,
    // not an exception nobody is waiting on.
    const it_ = delivery(ROUTABLE_BIND);
    expect(await it_.allowed("https://hook.example.org/hook")).toBe(true);
    expect(await it_.allowed("https://10.0.0.1/hook")).toBe(false);
    expect(await it_.allowed("not a url")).toBe(false);
  });

  it("refuses a loopback destination while the session holds a tunnel share", async () => {
    // Tunnel sharing exposes a loopback-bound server through a relay, so
    // "bound to loopback" stops implying "only local callers exist". This
    // holds even where §3 permits loopback.
    const it_ = delivery(LOOPBACK_BIND, { tunnelShared: true });
    expect(await it_.allowed("http://127.0.0.1:9000/hook")).toBe(false);
    // Its own counter, never the generic one: that counter feeds the
    // three-strike auto-unregister, and a share can be revoked at any moment,
    // so a few minutes of sharing must not permanently delete a healthy
    // webhook. Pinned in EGRESS_GUARD.md §4 across all four ports.
    expect(it_.seen).toStrictEqual([["webhook_delivery_blocked_tunnel_total", 1]]);
  });

  it("refuses it however the loopback destination was spelled", async () => {
    const it_ = delivery(LOOPBACK_BIND, { tunnelShared: true });
    for (const url of ["http://localhost:9000/hook", "http://[::1]/hook", "https://loopback.example/hook"]) {
      expect(await it_.allowed(url)).toBe(false);
    }
  });

  it("lets a public destination through to a tunnel-shared session", async () => {
    // The share says nothing about a destination that was never local.
    const it_ = delivery(LOOPBACK_BIND, { tunnelShared: true });
    expect(await it_.allowed("https://hook.example.org/hook")).toBe(true);
    expect(it_.seen).toStrictEqual([]);
  });

  it("proceeds when no share is live", async () => {
    // An expired share must not keep the guard closed: the caller answers
    // "is a share live *now*", and a swept or never-created share is a `false`
    // here.
    const it_ = delivery(LOOPBACK_BIND, { tunnelShared: false });
    expect(await it_.allowed("http://127.0.0.1:9000/hook")).toBe(true);
    expect(it_.seen).toStrictEqual([]);
  });

  it("defaults to no share", async () => {
    const it_ = delivery(LOOPBACK_BIND);
    expect(await it_.allowed("http://127.0.0.1:9000/hook")).toBe(true);
  });

  it("counts a destination that was refused before the share was consulted", async () => {
    // Destination safety is evaluated first, so this lands on the generic
    // counter even though a share is live. That is the intended split rather
    // than an accident of ordering: on a routable bind this destination is
    // refused by configuration and can never deliver, so it belongs on the
    // counter that eventually retires the webhook. Only a destination that
    // would otherwise be fine gets the share counter.
    const it_ = delivery(ROUTABLE_BIND, { tunnelShared: true });
    expect(await it_.allowed("http://127.0.0.1:9000/hook")).toBe(false);
    expect(it_.seen).toStrictEqual([["webhook_delivery_blocked_total", 1]]);
  });

  it("never puts a share refusal on the counter that retires webhooks", async () => {
    // The failure this guards against is silent and slow: a session shared for
    // a few minutes drives three loopback refusals, the auto-unregister fires,
    // and a webhook that was never misconfigured is gone. Go and C# both hit
    // this; C# shipped it briefly, incrementing both counters on the same path.
    const it_ = delivery(LOOPBACK_BIND, { tunnelShared: true });
    for (let attempt = 0; attempt < 4; attempt++) {
      expect(await it_.allowed("http://127.0.0.1:9000/hook")).toBe(false);
    }

    expect(it_.seen.filter(([name]) => name === "webhook_delivery_blocked_total")).toStrictEqual([]);
    expect(it_.seen).toHaveLength(4);
  });

  it("needs no metric sink", async () => {
    // A caller that asked for no counters must not be made to pay for them,
    // and must not crash for not having asked.
    const { resolve } = resolver();
    expect(await webhookDeliveryAllowed("http://127.0.0.1/hook", { resolve })).toBe(false);
  });

  it("resolves through the platform when given no resolver", async () => {
    expect(await webhookDeliveryAllowed("http://127.0.0.1/hook", { allowLoopbackDestinations: true })).toBe(true);
  });
});
