//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  applyProfileUpdate,
  type ConnectionProfile,
  findProfile,
  MUTABLE_PROFILE_FIELDS,
  removeProfile,
  visibleProfiles,
} from "./index.ts";

interface ProfilesGolden {
  mutable_fields: string[];
  profiles: ConnectionProfile[];
  listings: Array<{ owner: string | null; visible: string[] }>;
  updates: Array<{ name: string; updates: Record<string, unknown>; result: ConnectionProfile }>;
  updated_at: number;
}

const golden = loadGolden<ProfilesGolden>("profiles_golden.json");

/** The first recorded profile, which the update cases are applied to. */
const subject = golden.profiles[0] as ConnectionProfile;

describe("which profiles a caller sees", () => {
  it.each(golden.listings)("owner $owner", (record) => {
    expect(visibleProfiles(golden.profiles, record.owner ?? undefined).map((p) => p.profile_id)).toEqual(
      record.visible,
    );
  });

  it("shows a caller their own and the shared ones", () => {
    const seen = visibleProfiles(golden.profiles, "alice").map((p) => p.profile_id);
    expect(seen).toContain("p1");
    expect(seen).toContain("p2");
    expect(seen).toContain("p4");
  });

  it("never shows another person's private target", () => {
    expect(visibleProfiles(golden.profiles, "alice").map((p) => p.profile_id)).not.toContain("p3");
    expect(visibleProfiles(golden.profiles, "carol").map((p) => p.profile_id)).toEqual(["p2", "p4"]);
  });

  it("shows everything to a caller with no owner at all", () => {
    // The administrative view, and why a caller's identity has to reach this
    // rather than being assumed.
    expect(visibleProfiles(golden.profiles).map((p) => p.profile_id)).toEqual(golden.profiles.map((p) => p.profile_id));
  });

  it("treats an empty owner as somebody, not as nobody", () => {
    // A caller identified as the empty string owns nothing; reading it as
    // "no owner given" would show them every private profile there is.
    expect(visibleProfiles(golden.profiles, "").map((p) => p.profile_id)).toEqual(["p2", "p4"]);
  });

  it("does not hand back the list it was given", () => {
    const listed = visibleProfiles(golden.profiles);
    expect(listed).not.toBe(golden.profiles);
  });
});

describe("what an update may change", () => {
  it.each(golden.updates)("$name", (record) => {
    expect(applyProfileUpdate(subject, record.updates, golden.updated_at)).toEqual(record.result);
  });

  it("names the fields the reference names", () => {
    expect([...MUTABLE_PROFILE_FIELDS].sort()).toEqual(golden.mutable_fields);
  });

  it("refuses to move a profile to another owner", () => {
    // The point of the allow-list: a client that could rewrite an owner could
    // hand itself somebody else's saved target by editing a profile rather
    // than by asking for it.
    expect(applyProfileUpdate(subject, { owner: "mallory" }, golden.updated_at).owner).toBe(subject.owner);
  });

  it("refuses to change what identifies a profile", () => {
    const updated = applyProfileUpdate(
      subject,
      { profile_id: "stolen", connector_type: "shell", created_at: 0 },
      golden.updated_at,
    );
    expect(updated.profile_id).toBe(subject.profile_id);
    expect(updated.connector_type).toBe(subject.connector_type);
    expect(updated.created_at).toBe(subject.created_at);
  });

  it("keeps a permitted change alongside a forbidden one", () => {
    // Dropped rather than refused: a round-tripped profile carries every
    // field, so refusing the whole request would make the ordinary case fail.
    const updated = applyProfileUpdate(subject, { name: "renamed", owner: "mallory" }, golden.updated_at);
    expect(updated.name).toBe("renamed");
    expect(updated.owner).toBe(subject.owner);
  });

  it("ignores a field nobody defined", () => {
    expect(applyProfileUpdate(subject, { nonsense: 1 }, golden.updated_at)).toEqual(
      applyProfileUpdate(subject, {}, golden.updated_at),
    );
  });

  it("stamps the time itself rather than taking the caller's", () => {
    // So it records when the change happened, not what a client claimed.
    expect(applyProfileUpdate(subject, { updated_at: 0 }, golden.updated_at).updated_at).toBe(golden.updated_at);
    expect(applyProfileUpdate(subject, {}, 42).updated_at).toBe(42);
  });

  it("does not change the profile it was given", () => {
    const before = JSON.stringify(subject);
    applyProfileUpdate(subject, { name: "renamed" }, golden.updated_at);
    expect(JSON.stringify(subject)).toBe(before);
  });
});

describe("finding and removing a profile", () => {
  it("finds one by its identifier", () => {
    expect(findProfile(golden.profiles, "p3")?.owner).toBe("bob");
    expect(findProfile(golden.profiles, "nope")).toBeUndefined();
  });

  it("removes one and says it did", () => {
    const { profiles, removed } = removeProfile(golden.profiles, "p1");
    expect(removed).toBe(true);
    expect(profiles.map((p) => p.profile_id)).toEqual(["p2", "p3", "p4"]);
  });

  it("says so when there was nothing to remove", () => {
    // A caller can tell a deletion from a request to delete something that
    // never existed.
    const { profiles, removed } = removeProfile(golden.profiles, "nope");
    expect(removed).toBe(false);
    expect(profiles).toHaveLength(golden.profiles.length);
  });

  it("does not change the list it was given", () => {
    removeProfile(golden.profiles, "p1");
    expect(golden.profiles).toHaveLength(4);
  });
});
