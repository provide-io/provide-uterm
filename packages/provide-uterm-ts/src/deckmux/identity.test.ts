//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  identityAsPrincipal,
  PresenceStore,
  parseIdentityFrame,
  presenceFromIdentity,
  presenceToWire,
} from "./index.ts";

interface ParseCase {
  name: string;
  frame: Record<string, unknown>;
  secret: string | null;
  accepted: boolean;
  subject: string | null;
  claims: Record<string, unknown> | null;
  fingerprint: string | null;
}

interface IdentityGolden {
  secret: string;
  canonical_example: string;
  python_boolean_version: boolean;
  unsorted_claims_signature: string;
  unsigned: ParseCase[];
  signed: ParseCase[];
  presence: Array<{
    name: string;
    subject: string;
    claims: Record<string, unknown>;
    role_argument: string;
    presence: Record<string, unknown>;
    principal_subject_id: string;
    principal_display_name: string;
  }>;
  taken_colors: Array<{ name: string; taken: string[]; color: string }>;
}

const golden = loadGolden<IdentityGolden>("deckmux_identity_golden.json");

/** Parse one recorded case the way the corpus did. */
function parse(record: ParseCase) {
  return parseIdentityFrame(record.frame, record.secret ?? undefined);
}

/** The recorded frame for a named case. */
function frameFor(cases: ParseCase[], name: string): Record<string, unknown> {
  return (cases.find((entry) => entry.name === name) as ParseCase).frame;
}

describe("reading an identity frame", () => {
  it.each(golden.unsigned)("$name", (record) => {
    const identity = parse(record);
    expect(identity !== undefined).toBe(record.accepted);
    if (identity !== undefined) {
      expect(identity.subject).toBe(record.subject);
      expect(identity.claims).toStrictEqual(record.claims);
      expect(identity.fingerprint).toBe(record.fingerprint);
    }
  });

  it("refuses a frame that is not an identity", () => {
    // A presence update carrying a subject must not become a login.
    expect(golden.unsigned.find((entry) => entry.name === "the wrong message type")?.accepted).toBe(false);
    expect(golden.unsigned.find((entry) => entry.name === "no type at all")?.accepted).toBe(false);
  });

  it("ignores a version it does not understand", () => {
    // Ignored rather than raised: a newer proxy must not break an older hub.
    expect(golden.unsigned.find((entry) => entry.name === "an unknown version")?.accepted).toBe(false);
    expect(golden.unsigned.find((entry) => entry.name === "no version")?.accepted).toBe(false);
    expect(golden.unsigned.find((entry) => entry.name === "a version as a string")?.accepted).toBe(false);
    expect(golden.unsigned.find((entry) => entry.name === "a non-integral version")?.accepted).toBe(false);
  });

  it("refuses a boolean version, as the Go port does", () => {
    // Recorded, deliberately not matched: Python's `True in {1}` is true, so
    // the reference reads `"version": true` as version 1. Go's
    // identityVersion accepts only the int and float forms, so the ports
    // already disagree and this one follows Go.
    expect(golden.python_boolean_version).toBe(true);
    expect(parseIdentityFrame({ ...frameFor(golden.unsigned, "a well-formed frame"), version: true })).toBeUndefined();
  });

  it("refuses a frame with no usable subject", () => {
    // The subject is the participant's identity; without one there is nobody
    // to admit.
    for (const name of ["no subject", "an empty subject", "a subject that is not a string", "a null subject"]) {
      expect(golden.unsigned.find((entry) => entry.name === name)?.accepted).toBe(false);
    }
  });

  it("downgrades unusable claims rather than losing the subject", () => {
    // Better to admit somebody with no extra metadata than to drop the
    // identity along with the metadata.
    for (const name of ["claims that are not a mapping", "null claims", "no claims"]) {
      const record = golden.unsigned.find((entry) => entry.name === name) as ParseCase;
      expect(record.accepted).toBe(true);
      expect(record.claims).toStrictEqual({});
    }
  });

  it("downgrades an unusable fingerprint to nothing", () => {
    for (const name of ["no fingerprint", "a fingerprint that is not a string", "a null fingerprint"]) {
      const record = golden.unsigned.find((entry) => entry.name === name) as ParseCase;
      expect(record.accepted).toBe(true);
      expect(record.fingerprint).toBe("");
    }
  });

  it("copies the claims rather than holding the frame's own mapping", () => {
    // The frame is somebody else's object; a later mutation of it must not
    // reach into an admitted participant.
    const frame = { type: "identity", version: 1, subject: "sre:alice", claims: { role: "viewer" } };
    const identity = parseIdentityFrame(frame);
    (frame.claims as Record<string, unknown>).role = "admin";
    expect(identity?.claims).toStrictEqual({ role: "viewer" });
  });
});

describe("verifying a signed identity frame", () => {
  it.each(golden.signed)("$name", (record) => {
    expect(parse(record) !== undefined).toBe(record.accepted);
  });

  it("signs a canonical string, not the frame", () => {
    // Assemble it differently in any port and every frame is rejected.
    expect(golden.canonical_example).toBe('1:sre:alice:SHA256:abc:ssh:{"display_name":"Alice","role":"operator"}');
  });

  it("refuses a frame with no usable signature", () => {
    for (const name of ["no signature", "an empty signature", "a signature that is not a string", "a null signature"]) {
      expect(golden.signed.find((entry) => entry.name === name)?.accepted).toBe(false);
    }
  });

  it("refuses a signature from a different secret", () => {
    expect(golden.signed.find((entry) => entry.name === "a signature from the wrong secret")?.accepted).toBe(false);
    expect(golden.signed.find((entry) => entry.name === "the right signature, the wrong secret")?.accepted).toBe(false);
  });

  it("covers every field a caller would act on", () => {
    // Each of these is in the canonical string, so tampering after signing
    // has to break the check. A field left out is a field an attacker can
    // rewrite — the claims carry the role.
    for (const name of [
      "the subject tampered with",
      "the fingerprint tampered with",
      "the transport tampered with",
      "the claims tampered with",
      "the version tampered with",
    ]) {
      expect(golden.signed.find((entry) => entry.name === name)?.accepted).toBe(false);
    }
  });

  it("serialises the claims in a fixed order", () => {
    // Two mappings with the same pairs sign the same, so a proxy and a hub
    // need not agree on insertion order. The frame is rebuilt here rather
    // than read from the corpus, because the corpus file is itself key-sorted
    // and cannot carry the order that makes the point.
    const outOfOrder = {
      type: "identity",
      version: 1,
      subject: "sre:alice",
      claims: { role: "operator", display_name: "Alice" },
      fingerprint: "SHA256:abc",
      transport: "ssh",
      signature: golden.unsorted_claims_signature,
    };
    expect(Object.keys(outOfOrder.claims)).toStrictEqual(["role", "display_name"]);
    expect(parseIdentityFrame(outOfOrder, golden.secret)).toBeDefined();
    expect(golden.signed.find((entry) => entry.name === "claims in a different order")?.accepted).toBe(true);
  });

  it("refuses a signature of the wrong length", () => {
    // A comparison that reads a length mismatch as a match accepts anything.
    expect(golden.signed.find((entry) => entry.name === "a signature that is too short")?.accepted).toBe(false);
    expect(golden.signed.find((entry) => entry.name === "a signature that is too long")?.accepted).toBe(false);
  });

  it("treats an absent transport as empty rather than as missing", () => {
    expect(golden.signed.find((entry) => entry.name === "no transport in the frame")?.accepted).toBe(true);
    expect(golden.signed.find((entry) => entry.name === "a transport that is not a string")?.accepted).toBe(false);
  });

  it("takes an empty secret as no secret at all", () => {
    // Falsy, so verification is skipped rather than failing closed. Pinned
    // because a config that reads a missing environment variable into an
    // empty string lands exactly here, and the deployment that thought it was
    // verifying signatures is not.
    const record = golden.signed.find((entry) => entry.name === "an empty secret skips the check");
    expect(record?.accepted).toBe(true);
    expect(parseIdentityFrame({ ...frameFor(golden.signed, "no signature"), signature: "nonsense" }, "")).toBeDefined();
  });

  it("accepts what a correctly signing gateway sends", () => {
    expect(golden.signed.find((entry) => entry.name === "a correctly signed frame")?.accepted).toBe(true);
    expect(parse(golden.signed.find((entry) => entry.name === "a correctly signed frame") as ParseCase)).toBeDefined();
  });
});

describe("building a participant from an identity", () => {
  it.each(golden.presence)("$name", (record) => {
    const identity = parseIdentityFrame({
      type: "identity",
      version: 1,
      subject: record.subject,
      claims: record.claims,
    });
    const presence = presenceFromIdentity(identity as never, "conn-7", { role: record.role_argument });
    expect(presenceToWire(presence)).toStrictEqual(record.presence);
  });

  it("prefers the display name claim, then display, then the subject", () => {
    expect(golden.presence.find((entry) => entry.name === "both, the first wins")?.presence.name).toBe("First");
    expect(golden.presence.find((entry) => entry.name === "a display claim")?.presence.name).toBe("Alice B");
    expect(golden.presence.find((entry) => entry.name === "no display claim at all")?.presence.name).toBe("alice");
  });

  it("splits the realm at the first colon", () => {
    // A subject may carry more than one, and everything after the first is
    // the name — including the rest of the colons.
    expect(golden.presence.find((entry) => entry.name === "a subject with two colons")?.presence.name).toBe(
      "team:alice",
    );
  });

  it("falls back to a generated name when nothing else is usable", () => {
    // A participant with no name cannot be rendered, so there is always one.
    for (const name of [
      "a subject that is only a realm",
      "a subject that is only a colon",
      "a subject ending in a colon",
      "a whitespace subject",
    ]) {
      expect(golden.presence.find((entry) => entry.name === name)?.presence.name).toBe("Drift Panther");
    }
  });

  it("ignores a claim of the wrong type", () => {
    // A number where a name belongs falls through to the next candidate
    // rather than being rendered as one.
    expect(golden.presence.find((entry) => entry.name === "a display claim that is not a string")?.presence.name).toBe(
      "alice",
    );
    expect(golden.presence.find((entry) => entry.name === "a role claim that is not a string")?.presence.role).toBe(
      "viewer",
    );
  });

  it("ignores a blank claim", () => {
    expect(golden.presence.find((entry) => entry.name === "a blank display claim falls through")?.presence.name).toBe(
      "alice",
    );
  });

  it("trims a claimed name", () => {
    expect(golden.presence.find((entry) => entry.name === "a display claim with padding")?.presence.name).toBe("Alice");
  });

  it("lets a claimed role beat the caller's", () => {
    expect(golden.presence.find((entry) => entry.name === "a role claim beats the argument")?.presence.role).toBe(
      "admin",
    );
    expect(golden.presence.find((entry) => entry.name === "no role claim uses the argument")?.presence.role).toBe(
      "operator",
    );
    expect(golden.presence.find((entry) => entry.name === "no role anywhere")?.presence.role).toBe("");
  });

  it("keys the participant on the subject, not the connection", () => {
    // Repeated connections from one user collapse to one entry rather than
    // filling the participant list with duplicates of them.
    for (const record of golden.presence) {
      expect(record.presence.user_id).toBe(record.subject);
    }
  });

  it.each(golden.taken_colors)("$name", (record) => {
    const identity = parseIdentityFrame({
      type: "identity",
      version: 1,
      subject: "sre:alice",
      claims: record.name === "a claimed colour ignores what is taken" ? { color: "#123456" } : {},
    });
    const presence = presenceFromIdentity(identity as never, "conn-7", { takenColors: new Set(record.taken) });
    expect(presence.color).toBe(record.color);
  });

  it("walks past a colour already in use", () => {
    const natural = golden.taken_colors.find((entry) => entry.name === "nothing taken");
    const walked = golden.taken_colors.find((entry) => entry.name === "its natural colour taken");
    expect(walked?.color).not.toBe(natural?.color);
  });

  it("lets a claimed colour stand even when it is taken", () => {
    // The claim is what the deployment asked for; the walk only applies to
    // the generated fallback.
    expect(golden.taken_colors.find((entry) => entry.name === "a claimed colour ignores what is taken")?.color).toBe(
      "#123456",
    );
  });

  it("seats them as active rather than as long idle", () => {
    // The reference stamps the current time. Left at zero the participant is
    // idle by decades, and the first prune sweep removes somebody who just
    // connected.
    const identity = parseIdentityFrame({ type: "identity", version: 1, subject: "sre:alice", claims: {} });
    const store = new PresenceStore({ now: () => 1_000 });
    const presence = presenceFromIdentity(identity as never, "conn-7", { now: () => 1_000 });
    expect(presence.lastActivityAt).toBe(1_000);
    expect(store.isIdle(presence, 60)).toBe(false);
  });

  it("stamps the wall clock when no clock is given", () => {
    const identity = parseIdentityFrame({ type: "identity", version: 1, subject: "sre:alice", claims: {} });
    const before = Date.now() / 1000;
    const presence = presenceFromIdentity(identity as never, "conn-7");
    expect(presence.lastActivityAt).toBeGreaterThanOrEqual(before);
  });

  it("defaults the role and the taken colours", () => {
    const identity = parseIdentityFrame({ type: "identity", version: 1, subject: "sre:alice", claims: {} });
    expect(presenceFromIdentity(identity as never, "conn-7").role).toBe("");
    expect(presenceFromIdentity(identity as never, "conn-7").color).toBe(
      golden.taken_colors.find((entry) => entry.name === "nothing taken")?.color,
    );
  });
});

describe("adapting an identity to a principal", () => {
  it.each(golden.presence)("$name", (record) => {
    const identity = parseIdentityFrame({
      type: "identity",
      version: 1,
      subject: record.subject,
      claims: record.claims,
    });
    const principal = identityAsPrincipal(identity as never);
    expect(principal.subjectId).toBe(record.principal_subject_id);
    expect(principal.displayName).toBe(record.principal_display_name);
  });

  it("names an SSH user the way the hub names everybody else", () => {
    // The hub reads subjectId and displayName off whatever it is handed, so
    // an SSH-authenticated user takes the same code path as an HTTP one.
    const record = golden.presence.find((entry) => entry.name === "a display name claim");
    expect(record?.principal_subject_id).toBe("sre:alice");
    expect(record?.principal_display_name).toBe("Alice A");
  });

  it("falls back to the subject itself rather than to a generated name", () => {
    // Unlike a participant, a principal is an identity — a made-up name here
    // would be a made-up subject in a log line.
    expect(
      golden.presence.find((entry) => entry.name === "a subject that is only a realm")?.principal_display_name,
    ).toBe("sre:");
    expect(golden.presence.find((entry) => entry.name === "a whitespace subject")?.principal_display_name).toBe("   ");
  });

  it("carries the identity it was built from", () => {
    const identity = parseIdentityFrame({ type: "identity", version: 1, subject: "sre:alice", claims: {} });
    expect(identityAsPrincipal(identity as never).identity).toBe(identity);
  });
});
