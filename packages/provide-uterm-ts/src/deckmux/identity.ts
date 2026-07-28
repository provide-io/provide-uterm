//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The bridge from an authenticated SSH connection to a DeckMux participant.
 *
 * Port of the Python module `provide.uterm.deckmux._identity`.
 *
 * The gateway sends an `identity` frame as the first message on a connection
 * whose public key it accepted, and this turns that frame into somebody the
 * hub can seat. Everything in the frame comes from another process, so most
 * of what is here is about what it refuses.
 *
 * ## Trust boundary
 *
 * No trust decision is made here. Whether an identity frame is honoured at
 * all is the caller's: a hub behind a trusted proxy on the same host should
 * accept it, and a hub reachable from anywhere else should either ignore it
 * or check the fingerprint against its own registry first. Passing an
 * expected secret makes the frame self-authenticating; passing none takes it
 * at its word.
 */

import { createHmac, timingSafeEqual } from "node:crypto";
import type { ResolvedIdentity } from "../auth/index.ts";
import { pyJsonDumps } from "../pycompat/index.ts";
import { generateColor, generateInitials, generateName } from "./names.ts";
import type { UserPresence } from "./presence.ts";

/** The frame versions this understands. */
const SUPPORTED_VERSIONS: ReadonlySet<number> = new Set([1]);

/** A principal built from an identity frame. */
export interface IdentityPrincipal {
  /** The identity's subject, as the hub's own principals spell it. */
  subjectId: string;
  /** What to show for them. */
  displayName: string;
  /** What it was built from. */
  identity: ResolvedIdentity;
}

/**
 * A plain object, as JSON produces.
 *
 * The null check changes no answer on its own — spreading null yields an
 * empty object, which is what an unusable claims value becomes anyway — but
 * null is not a mapping and saying so here keeps that from being a
 * coincidence.
 */
function isMapping(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A non-empty trimmed string, or nothing. */
function stringOrNothing(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed;
}

/**
 * The part of a subject after its realm, as a display name.
 *
 * Split at the *first* colon, so a subject carrying more than one keeps the
 * rest of them in the name. The no-colon branch reaches the same answer as
 * the slice would on its own — `indexOf` returning -1 makes it a slice from
 * zero — and is kept because a subject with no realm is a different thing
 * from one whose realm is empty.
 */
function nameFromSubject(subject: string): string | undefined {
  if (!subject.includes(":")) {
    return stringOrNothing(subject);
  }
  return stringOrNothing(subject.slice(subject.indexOf(":") + 1));
}

/** The name a set of claims asks for, if it asks for one. */
function claimedName(claims: Record<string, unknown>, subject: string): string | undefined {
  return stringOrNothing(claims.display_name) ?? stringOrNothing(claims.display) ?? nameFromSubject(subject);
}

/** The string a signature covers. */
function canonical(
  version: number,
  subject: string,
  fingerprint: string,
  transport: string,
  claims: Record<string, unknown>,
): string {
  // Key-sorted and separator-free, so a proxy and a hub need not agree on
  // insertion order — and so that assembling it differently in any port
  // rejects every frame the others accept.
  const claimsJson = pyJsonDumps(claims, { sortKeys: true, separators: [",", ":"] });
  return `${version}:${subject}:${fingerprint}:${transport}:${claimsJson}`;
}

/** Whether two hex digests match, without leaking where they first differ. */
function digestsMatch(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, "utf-8");
  const rightBytes = Buffer.from(right, "utf-8");
  // A length mismatch cannot go through `timingSafeEqual`, which throws on
  // unequal lengths; it is also not a secret, since the digest length is
  // fixed and public.
  return leftBytes.length === rightBytes.length && timingSafeEqual(leftBytes, rightBytes);
}

/**
 * Read an identity out of a control frame, or refuse it.
 *
 * Returns nothing when the frame is not an `identity` message, when its
 * version is not understood, or when it carries no usable subject. An
 * unknown version is ignored rather than raised, so a newer proxy does not
 * break an older hub.
 *
 * Unusable claims are downgraded to none rather than rejected: better to seat
 * somebody with no extra metadata than to lose the subject along with it. The
 * fingerprint is treated the same way.
 *
 * When `expectedSecret` is given the frame must also carry a signature over
 * {@link canonical}. An **empty** secret counts as no secret at all and skips
 * the check — a deployment that reads a missing environment variable into an
 * empty string is not verifying anything, which is worth knowing.
 */
export function parseIdentityFrame(
  frame: Record<string, unknown>,
  expectedSecret?: string | undefined,
): ResolvedIdentity | undefined {
  if (frame.type !== "identity") {
    return undefined;
  }
  // Membership does all the deciding here: nothing but the number 1 is in
  // that set, so a string, a float, a boolean and an absent field are all
  // refused by it alone. The `typeof` is what lets the version be used as a
  // number afterwards — and it is also where this port parts company with
  // the reference, which reads `true` as 1 because Python's `True == 1`. Go's
  // identityVersion refuses a bool, so the ports already disagree and this
  // follows Go.
  const version = frame.version;
  if (typeof version !== "number" || !SUPPORTED_VERSIONS.has(version)) {
    return undefined;
  }
  const subject = frame.subject;
  if (typeof subject !== "string" || subject === "") {
    return undefined;
  }
  // Copied, not held: the frame is somebody else's object, and a later
  // mutation of it must not reach into a seated participant.
  const claims: Record<string, unknown> = isMapping(frame.claims) ? { ...frame.claims } : {};
  const fingerprint = typeof frame.fingerprint === "string" ? frame.fingerprint : "";

  if (expectedSecret !== undefined && expectedSecret !== "") {
    const signature = frame.signature;
    // The emptiness check is the reference's. It changes no answer, since an
    // empty string is the wrong length for a digest and fails the comparison
    // anyway, but a frame with no signature is refused for not having one
    // rather than for failing to match.
    if (typeof signature !== "string" || signature === "") {
      return undefined;
    }
    const transport = typeof frame.transport === "string" ? frame.transport : "";
    const expected = createHmac("sha256", expectedSecret)
      .update(canonical(version, subject, fingerprint, transport, claims), "utf-8")
      .digest("hex");
    if (!digestsMatch(signature, expected)) {
      return undefined;
    }
  }

  return { subject, claims, fingerprint };
}

/** Options for {@link presenceFromIdentity}. */
export interface PresenceFromIdentityOptions {
  /** Colours other participants already hold. */
  takenColors?: ReadonlySet<string>;
  /** The role to use when the claims do not name one. */
  role?: string;
  /** Wall clock in seconds. */
  now?: () => number;
}

/**
 * Turn an identity into a participant.
 *
 * Names resolve as `display_name`, then `display`, then the part of the
 * subject after its realm, then a name generated from the connection id —
 * there is always one, because a participant with no name cannot be
 * rendered. The colour is the claimed one, or a generated one that avoids
 * what is already taken.
 *
 * The connection id is only used for those fallbacks. The participant is
 * keyed on the subject, so repeated connections from one person collapse to a
 * single entry rather than filling the list with copies of them.
 */
export function presenceFromIdentity(
  identity: ResolvedIdentity,
  connectionId: string,
  options: PresenceFromIdentityOptions = {},
): UserPresence {
  // The reference guards this with `or {}` because its dataclass can hold
  // None. Here the type says otherwise, so there is nothing to guard.
  const claims = identity.claims;
  const name = claimedName(claims, identity.subject) ?? generateName(connectionId);
  const color = stringOrNothing(claims.color) ?? generateColor(connectionId, options.takenColors ?? new Set());
  return {
    userId: identity.subject,
    name,
    color,
    role: stringOrNothing(claims.role) ?? options.role ?? "",
    initials: generateInitials(name),
    scrollLine: 0,
    scrollRange: [0, 0],
    totalLines: 0,
    selection: undefined,
    pin: undefined,
    typing: false,
    queuedKeys: "",
    cols: 0,
    rows: 0,
    // Stamped now, as the reference does. Left at zero the participant is
    // idle by decades and the first prune sweep removes somebody who has just
    // this moment connected.
    lastActivityAt: (options.now ?? (() => Date.now() / 1000))(),
    isOwner: false,
  };
}

/**
 * Adapt an identity to the shape the hub already consumes.
 *
 * The hub reads `subjectId` and `displayName` off whatever it is handed, so
 * an SSH-authenticated user takes the same path as any other principal and
 * the hub needs no branch for them.
 *
 * The display name falls back to the subject itself rather than to a
 * generated one: a principal is an identity, and a made-up name here would be
 * a made-up subject in a log line.
 */
export function identityAsPrincipal(identity: ResolvedIdentity): IdentityPrincipal {
  const claims = identity.claims;
  return {
    subjectId: identity.subject,
    displayName: claimedName(claims, identity.subject) ?? identity.subject,
    identity,
  };
}
