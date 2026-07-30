//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Where a registered webhook is allowed to deliver to.
 *
 * Port of `validate_webhook_url` / `_address_allowed` / `_delivery_url_allowed`
 * in `provide.uterm.server.webhooks`, against the contract in
 * `conformance/EGRESS_GUARD.md`.
 *
 * A webhook destination is a URL the *server* fetches, supplied by whoever can
 * mutate a session. That is attacker-influenced input driving a request from a
 * more privileged position than the caller holds — the classic SSRF shape — so
 * the reasoning behind each refusal is worth stating:
 *
 * - **Metadata is never negotiable.** There is deliberately no key that
 *   re-opens `169.254.169.254`, `100.100.100.200`, `fd00:ec2::254` or the name
 *   `metadata.google.internal`: reaching one hands out cloud credentials, and a
 *   webhook has no legitimate business there.
 * - **Loopback is the one conditional case.** Binding to `127.0.0.1` is itself
 *   an access control — services listen there precisely so the network cannot
 *   reach them, and skip authentication on that basis. Loopback SSRF converts
 *   "unreachable" into "reachable". A service on a private range at least chose
 *   a routable interface, which is why loopback gets a key and the private
 *   ranges do not.
 * - **Every resolved address is checked.** One good answer does not make a name
 *   safe: a DNS-rebinding reply puts the metadata address in the same response,
 *   and a loop that stopped at the first address would take the good one.
 * - **No answer is a refusal.** A resolution that fails, times out, or comes
 *   back empty means "no usable address", and a guard that read that as "fine"
 *   would be switched off by a hostile — or merely broken — resolver.
 *
 * This module holds no address tables of its own. It classifies through
 * {@link classifyEgressAddress}, which the connector guard also uses: two
 * classifiers that can disagree is a worse failure than the gap either closes.
 */

import { ipAddress } from "../pycompat/index.ts";
// The direct module rather than the `serverconfig` barrel: this needs one
// exported constant, and going through the barrel would pull the configuration
// loader — TOML parser and all — into the egress module's import graph.
import { LOOPBACK_HOSTS } from "../serverconfig/validators.ts";
import { classifyEgressAddress, cleanEgressHost, type Resolver, resolveThroughPlatform } from "./egress.ts";

/**
 * Raised when a webhook URL is refused.
 *
 * Deliberately not {@link EgressBlockedError}: this refusal answers whoever is
 * *registering* a webhook and belongs in their `400`, where an
 * `EgressBlockedError` means a request the server was already committed to
 * making could not be made. The reference draws the same line by raising
 * `ValueError` here and its egress error there.
 */
export class WebhookUrlError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WebhookUrlError";
  }
}

/**
 * Bound on one resolution, so a hostile resolver cannot hang registration.
 *
 * Ported from `_REGISTER_DNS_TIMEOUT_S`. Shorter than the connector guard's
 * five seconds because this one runs inside a request an operator is waiting
 * on, and because a webhook host that cannot answer in two seconds is not a
 * webhook host worth registering.
 */
export const WEBHOOK_DNS_TIMEOUT_S = 2.0;

/** The counter a delivery refused for an unsafe destination increments. */
export const WEBHOOK_DELIVERY_BLOCKED_TOTAL = "webhook_delivery_blocked_total";

/**
 * The counter a delivery refused for §4 — loopback while the session holds a
 * live tunnel share — increments.
 *
 * Deliberately **not** {@link WEBHOOK_DELIVERY_BLOCKED_TOTAL}. That counter feeds
 * the three-strike auto-unregister in the reference's delivery loop, and a
 * tunnel share can be revoked at any moment, so counting a share refusal there
 * would let a few minutes of sharing permanently delete a healthy webhook. Go
 * and C# reached the same conclusion independently; the name is pinned in
 * `conformance/EGRESS_GUARD.md` §4 so the four ports cannot drift apart on it.
 */
export const WEBHOOK_DELIVERY_BLOCKED_TUNNEL_TOTAL = "webhook_delivery_blocked_tunnel_total";

/**
 * The name that is never allowed, whatever it resolves to.
 *
 * On GCE it answers `169.254.169.254`, so the address check would normally
 * catch it — but a resolver that answered otherwise, or a `/etc/hosts` entry,
 * would not, and the name is only ever used to reach the metadata service.
 */
const METADATA_NAME = "metadata.google.internal";

/**
 * The refusal messages, verbatim from the reference.
 *
 * They are deliberately vague about *why* a host was refused: an operator
 * reads the same message whether their URL named the metadata service, a
 * private range or loopback, and a caller registering a webhook learns
 * nothing about the network they are pointing at. The distinction the
 * connector guard draws between "metadata" and "internal" is a *log* concern
 * there; here the string crosses a trust boundary.
 */
const NOT_ALLOWED = "webhook url host is not allowed";
const UNRESOLVED = "webhook url host could not be resolved";
const BAD_SCHEME = "webhook url must use http or https";
const NO_HOST = "webhook url must include a host";

/**
 * `scheme://authority`, as `urlparse` splits it.
 *
 * Applied to the raw string before WHATWG parsing, because the two disagree in
 * ways that matter here. WHATWG strips leading whitespace, so `" http://x/"`
 * is a URL to it and is not one to the reference; and its special-scheme path
 * turns `http:///hook` into a request to a host named `hook`, pulling the
 * first path segment up into the empty authority, where the reference reads an
 * empty netloc and refuses. Both are refused here: an empty authority names no
 * host, and a URL that names nothing must not be delivered to *somewhere*.
 *
 * Case-insensitive because `urlparse` lowercases the scheme, so `HTTPS://…` is
 * a webhook URL the reference accepts.
 */
const SCHEME_AUTHORITY = /^(https?):\/\/([^/?#]*)/i;

/** How a webhook URL is validated. */
export interface ValidateWebhookUrlOptions {
  /**
   * Whether a loopback destination is permitted.
   *
   * Defaults to refusing, matching the reference's
   * `allow_loopback_destinations: bool = False`. An embedder who has not
   * considered egress gets the closed posture. This is the *effective*
   * permission — see {@link effectiveAllowLoopbackDestinations}, and do not
   * recompute it per call site.
   */
  allowLoopbackDestinations?: boolean | undefined;
  /** How a name is turned into addresses. The platform's unless a test says otherwise. */
  resolve?: Resolver | undefined;
  /** How long resolution may take. {@link WEBHOOK_DNS_TIMEOUT_S} unless given. */
  timeoutS?: number | undefined;
}

/** How a delivery-time destination is checked. */
export interface WebhookDeliveryOptions extends ValidateWebhookUrlOptions {
  /**
   * Whether the session this webhook belongs to holds a live tunnel share.
   *
   * Asked of the caller rather than read from a registry because the answer
   * must be "is a share live *now*": shares are issued at runtime and expire,
   * and an expired share must not keep the guard closed. At configuration-load
   * time the fact is neither true nor false yet, which is why this cannot be
   * folded into {@link effectiveAllowLoopbackDestinations}.
   */
  tunnelShared?: boolean | undefined;
  /** Where a refusal is counted, so an operator can see it happening. */
  onMetric?: ((name: string, value: number) => void) | undefined;
}

/** The `[webhooks]` and `[server]` sections, as merged. */
export interface WebhookEgressConfig {
  webhooks: Readonly<Record<string, unknown>>;
  server: Readonly<Record<string, unknown>>;
}

/** A destination that passed, and whether it is local to the server. */
interface WebhookTarget {
  url: string;
  loopback: boolean;
}

const LOOPBACK_BIND_HOSTS: ReadonlySet<string> = new Set(LOOPBACK_HOSTS);

/**
 * Whether a bind address can be reached off-box.
 *
 * Ported from `_is_loopback_host` in `server/app/auth.py`: exact membership
 * after trimming and lowercasing, over the same three spellings. Deliberately
 * *not* generalised to "anything in `127.0.0.0/8`" — the established idiom in
 * this codebase is this set, it gates `require_jwt_in_production`,
 * `auth.mode="header"` and `security.mode="dev"` as well, and a broader answer
 * here would silently widen those too.
 */
export function isLoopbackHost(host: string): boolean {
  return LOOPBACK_BIND_HOSTS.has(host.trim().toLowerCase());
}

/**
 * Whether this server may deliver webhooks to loopback.
 *
 * ```
 * effective_allow_loopback =
 *     config.webhooks.allow_loopback_destinations OR is_loopback_host(config.server.host)
 * ```
 *
 * The bind term is what makes the default configuration usable. The default
 * bind is `127.0.0.1`, so without it a stock server listens only on loopback
 * *and refuses loopback webhook destinations* — a guard protecting nothing (no
 * remote caller can reach the listener) at the cost of breaking every
 * single-box deployment. Deriving permissiveness from the bind address is the
 * established idiom here; see `_is_loopback_host` in `server/app/auth.py`.
 *
 * Compute this once, where the server is built from configuration, and pass
 * the answer down. Recomputing it per call site is how one path ends up
 * reading the key and another reading the bind.
 */
export function effectiveAllowLoopbackDestinations(config: WebhookEgressConfig): boolean {
  // `=== true` rather than a truthiness test: a document that wrote a string
  // there is `serverconfig`'s to refuse, and it does — but if one ever slipped
  // past, `"false"` must not read as permission on the way through.
  return config.webhooks.allow_loopback_destinations === true || isLoopbackHost(String(config.server.host));
}

/**
 * Validate a webhook delivery URL.
 *
 * @returns The URL unchanged, as the reference does, so a caller can assign
 *   the validated value back. Normalising it here would change what is POSTed.
 * @throws {WebhookUrlError} If the scheme is not http(s), the URL names no
 *   host, the host is refused, or it cannot be resolved.
 */
export async function validateWebhookUrl(url: string, options: ValidateWebhookUrlOptions = {}): Promise<string> {
  return (await inspectWebhookUrl(url, options)).url;
}

/**
 * Whether a webhook may be delivered to this URL *right now*.
 *
 * Answers rather than throwing: delivery runs in a background task, where a
 * refusal is a counter and a log line and there is nobody to catch an
 * exception.
 *
 * The extra rule over {@link validateWebhookUrl} is §4 of the contract: a
 * loopback destination is refused while the session holds an active tunnel
 * share, *even where the effective permission allows loopback*. Tunnel sharing
 * exposes a loopback-bound server through a relay, so "bound to loopback"
 * stops implying "only local callers exist" — and the share is issued long
 * after the configuration was loaded, so this cannot be decided any earlier.
 */
export async function webhookDeliveryAllowed(url: string, options: WebhookDeliveryOptions = {}): Promise<boolean> {
  let target: WebhookTarget;
  try {
    target = await inspectWebhookUrl(url, options);
  } catch {
    // Broad, as the reference is: a destination that cannot be *evaluated* is
    // one this must not deliver to, whatever went wrong evaluating it.
    return refuse(options, WEBHOOK_DELIVERY_BLOCKED_TOTAL);
  }
  // Destination safety first, share second — the order Go and C# settled on. A
  // destination the configuration refuses outright can never deliver, so it
  // belongs on the counter that eventually retires the webhook. The share guard
  // is for destinations that would otherwise be fine.
  if (target.loopback && options.tunnelShared === true) {
    return refuse(options, WEBHOOK_DELIVERY_BLOCKED_TUNNEL_TOTAL);
  }
  return true;
}

/** Count a refused delivery, if anyone is counting, and refuse it. */
function refuse(options: WebhookDeliveryOptions, metric: string): false {
  options.onMetric?.(metric, 1);
  return false;
}

/**
 * Decide a URL, and say whether the destination it names is loopback.
 *
 * The loopback answer is carried out rather than swallowed because delivery
 * needs it even in the case where it was allowed: §4 refuses a *permitted*
 * loopback destination once a tunnel share is live.
 */
async function inspectWebhookUrl(url: string, options: ValidateWebhookUrlOptions): Promise<WebhookTarget> {
  const allowLoopback = options.allowLoopbackDestinations === true;
  const host = webhookHost(url);
  if (host === METADATA_NAME) {
    throw new WebhookUrlError(NOT_ALLOWED);
  }
  // `*.localhost` is reserved for loopback (RFC 6761) and resolvers answer
  // 127.0.0.1 for it, so a check that only knew the bare name would be walked
  // straight past by `api.localhost`. Decided by rule and never resolved: a
  // resolver answering something public for it must not re-open the case.
  if (host === "localhost" || host.endsWith(".localhost")) {
    if (!allowLoopback) {
      throw new WebhookUrlError(NOT_ALLOWED);
    }
    return { url, loopback: true };
  }
  const literal = ipAddress(host);
  // A literal is not looked up: there is nothing to resolve, and a lookup
  // would open a rebinding window between the check and the delivery.
  const addresses = literal === undefined ? await resolveWebhookHost(host, options) : [host];

  let loopback = false;
  for (const text of addresses) {
    const address = ipAddress(text);
    if (address === undefined) {
      // A resolver that answered with something that is not an address. The
      // reference reaches `ipaddress.ip_address`'s `ValueError` here and turns
      // it into the same refusal.
      throw new WebhookUrlError(NOT_ALLOWED);
    }
    const kind = classifyEgressAddress(address);
    if (kind === "loopback") {
      if (!allowLoopback) {
        throw new WebhookUrlError(NOT_ALLOWED);
      }
      // One loopback answer among others is enough to make the destination
      // local, for the same reason one bad answer refuses the whole name: the
      // request goes to whichever address is picked.
      loopback = true;
    } else if (kind !== "public") {
      // Metadata and every private / link-local / multicast / unspecified /
      // reserved range, with no key that re-opens them.
      throw new WebhookUrlError(NOT_ALLOWED);
    }
  }
  return { url, loopback };
}

/**
 * The host a webhook URL names, normalised.
 *
 * @throws {WebhookUrlError} If the scheme is not http(s), or the URL carries
 *   no usable host.
 */
function webhookHost(url: string): string {
  const split = SCHEME_AUTHORITY.exec(url);
  if (split === null) {
    // Either the scheme is not http(s) — `file:`, `ftp:`, `ws:` — or the
    // string is not a URL at all. Both are the reference's empty-or-wrong
    // scheme branch.
    throw new WebhookUrlError(BAD_SCHEME);
  }
  if (split[2] === "") {
    throw new WebhookUrlError(NO_HOST);
  }
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    // The authority is present but unusable: a bracket form that is not an
    // address, a space or a null byte inside the host. The reference gets here
    // too — `parsed.hostname` raises on exactly these — it just lets the raise
    // escape as a 500 rather than answering the caller.
    throw new WebhookUrlError(NO_HOST);
  }
  // `parsed.hostname` rather than the captured authority: it drops the
  // userinfo, so `http://hook.example.org@169.254.169.254/` is read as the
  // metadata service it actually reaches rather than the innocuous name in
  // front of the `@`. WHATWG lowercases and punycodes on the way; the trailing
  // dot it keeps is the same name to a resolver, so it goes, as the
  // reference's `.rstrip(".")` drops it.
  return cleanEgressHost(parsed.hostname).replace(/\.+$/, "").toLowerCase();
}

/**
 * Resolve a webhook host, under a deadline, failing closed.
 *
 * @throws {WebhookUrlError} If resolution fails, times out, or answers with
 *   nothing. An empty answer must be refused explicitly: the caller's loop
 *   over an empty list never runs, so the host would otherwise be allowed by
 *   nothing having objected.
 */
async function resolveWebhookHost(host: string, options: ValidateWebhookUrlOptions): Promise<string[]> {
  const resolve = options.resolve ?? resolveThroughPlatform;
  const timeoutS = options.timeoutS ?? WEBHOOK_DNS_TIMEOUT_S;
  let addresses: string[];
  try {
    addresses = await withDeadline(resolve(host), timeoutS, host);
  } catch {
    throw new WebhookUrlError(UNRESOLVED);
  }
  if (addresses.length === 0) {
    throw new WebhookUrlError(UNRESOLVED);
  }
  return addresses;
}

/**
 * Await *work*, or give up.
 *
 * The reference bounds this with `socket.setdefaulttimeout`, which has no
 * equivalent for a promise: a resolver that never settles would otherwise hang
 * the caller for as long as it liked, which is a denial of service a hostile
 * resolver gets for free. The timer is always cleared, so a resolution that
 * beat the deadline does not hold the event loop open behind it.
 */
async function withDeadline(work: Promise<string[]>, timeoutS: number, host: string): Promise<string[]> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_settle, reject) => {
    timer = setTimeout(() => reject(new Error(`resolving webhook host '${host}' timed out`)), timeoutS * 1000);
  });
  try {
    return await Promise.race([work, deadline]);
  } finally {
    clearTimeout(timer);
  }
}
