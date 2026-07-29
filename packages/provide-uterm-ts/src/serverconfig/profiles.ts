//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Saved connection targets, owned by whoever made them.
 *
 * Port of `provide.uterm.server.profiles`. The storage is the caller's — the
 * reference writes a JSON file, and what matters here is what may be read,
 * written and changed, which is the same wherever it lives.
 */

/** A connector a profile can name. */
export const PROFILE_CONNECTOR_TYPES = ["ssh", "telnet", "websocket", "ushell", "shell"] as const;

/** One of {@link PROFILE_CONNECTOR_TYPES}. */
export type ProfileConnectorType = (typeof PROFILE_CONNECTOR_TYPES)[number];

/** Who may see a profile. Distinct from a *session's* visibility, which has three values. */
export const PROFILE_VISIBILITIES = ["private", "shared"] as const;

/** One of {@link PROFILE_VISIBILITIES}. */
export type ProfileVisibility = (typeof PROFILE_VISIBILITIES)[number];

/** A saved connection target. */
export interface ConnectionProfile {
  profile_id: string;
  owner: string;
  name: string;
  connector_type: ProfileConnectorType;
  host: string | null;
  port: number | null;
  username: string | null;
  tags: string[];
  input_mode: "open" | "hijack";
  recording_enabled: boolean;
  visibility: ProfileVisibility;
  created_at: number;
  updated_at: number;
}

/**
 * The fields an update may change.
 *
 * A closed set, and what it leaves out is the point: the owner, the
 * identifier, the connector type and the creation time cannot be changed. A
 * client that could rewrite an owner could hand itself somebody else's saved
 * target by editing a profile rather than by asking for it.
 */
const MUTABLE_FIELD_NAMES = [
  "name",
  "host",
  "port",
  "username",
  "tags",
  "input_mode",
  "recording_enabled",
  "visibility",
] as const satisfies readonly (keyof ConnectionProfile)[];

/** A field {@link MUTABLE_PROFILE_FIELDS} permits. */
export type MutableProfileField = (typeof MUTABLE_FIELD_NAMES)[number];

/** Read-only, so a caller cannot widen the allow-list by adding to it. */
export const MUTABLE_PROFILE_FIELDS: ReadonlySet<MutableProfileField> = new Set(MUTABLE_FIELD_NAMES);

/** Whether a name an update used is one of them. */
function isMutableField(field: string): field is MutableProfileField {
  return (MUTABLE_PROFILE_FIELDS as ReadonlySet<string>).has(field);
}

/**
 * Write one permitted field.
 *
 * Separate so the key and the value are tied together by the type of the
 * field being written, rather than both being widened to `unknown` and the
 * result asserted back.
 */
function assignField<F extends MutableProfileField>(profile: ConnectionProfile, field: F, value: unknown): void {
  profile[field] = value as ConnectionProfile[F];
}

/**
 * Which profiles a caller may see.
 *
 * Their own and the shared ones. Asking with no owner at all returns
 * everything, which is the administrative view — and is why a caller's
 * identity has to reach this rather than being assumed.
 */
export function visibleProfiles(profiles: readonly ConnectionProfile[], owner?: string): ConnectionProfile[] {
  if (owner === undefined) {
    return [...profiles];
  }
  return profiles.filter((profile) => profile.owner === owner || profile.visibility === "shared");
}

/**
 * Apply an update, keeping only the fields that may change.
 *
 * Everything else in the update is dropped rather than refused: a client
 * sending a field it may not set has not necessarily attacked anything — a
 * round-tripped profile carries all of them — and refusing the whole request
 * would make the ordinary case fail.
 *
 * The update time is stamped here rather than taken from the caller, so it
 * records when the change happened rather than what a client claimed.
 */
export function applyProfileUpdate(
  profile: ConnectionProfile,
  updates: Readonly<Record<string, unknown>>,
  now: number,
): ConnectionProfile {
  const updated: ConnectionProfile = { ...profile };
  for (const [field, value] of Object.entries(updates)) {
    if (isMutableField(field)) {
      assignField(updated, field, value);
    }
  }
  updated.updated_at = now;
  return updated;
}

/** Find one profile by its identifier. */
export function findProfile(profiles: readonly ConnectionProfile[], profileId: string): ConnectionProfile | undefined {
  return profiles.find((profile) => profile.profile_id === profileId);
}

/**
 * Remove one profile.
 *
 * Reports whether it was there, so a caller can tell a deletion from a
 * request to delete something that never existed.
 */
export function removeProfile(
  profiles: readonly ConnectionProfile[],
  profileId: string,
): { profiles: ConnectionProfile[]; removed: boolean } {
  const remaining = profiles.filter((profile) => profile.profile_id !== profileId);
  return { profiles: remaining, removed: remaining.length !== profiles.length };
}
