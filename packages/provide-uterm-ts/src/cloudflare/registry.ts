//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Fleet-wide session registry backed by Cloudflare KV.
 *
 * Port of the Python module `provide.uterm.cloudflare.state.registry`.
 *
 * Every Durable Object writes its own status here, and the Default Worker
 * reads the lot back to answer "what sessions exist". The store is eventually
 * consistent, so a listing can be a minute or so behind for the global
 * network and under a second within one colo.
 *
 * Every call degrades rather than raising. This is a network hop: a status
 * write that could not land must not take the session down with it, and one
 * unreadable entry must not cost the whole listing.
 */

/** The key-value store a Worker is bound to. */
export interface SessionRegistryKV {
  get(key: string): Promise<string | null | undefined>;
  put(key: string, value: string): Promise<void>;
  delete(key: string): Promise<void>;
  list(options: { prefix: string }): Promise<unknown>;
}

/** A Worker environment, which may or may not have a registry bound. */
export interface RegistryEnv {
  SESSION_REGISTRY?: SessionRegistryKV;
}

/** What a session says about itself beyond its status. */
export interface SessionMeta {
  display_name?: string;
  created_at?: number;
  connector_type?: string;
  tags?: string[];
  owner?: string | null;
  visibility?: string;
}

/** Options for {@link updateKvSession}. */
export interface UpdateKvSessionOptions {
  connected: boolean | null;
  hijacked?: boolean;
  input_mode?: string;
  recording_enabled?: boolean;
  recording_available?: boolean;
  meta?: SessionMeta;
  remove_offline?: boolean;
}

/** How often the alarm rewrites a live entry. */
export const KV_REFRESH_S = 60;

/** What every session document's key begins with. */
const KV_PREFIX = "session:";

/**
 * Fields that must never leave in a fleet listing.
 *
 * Token material and invite secrets live in the same document, because a
 * Durable Object needs them to bootstrap a tunnel. Handing them out in a list
 * response would be a long-lived credential loose during the invite window.
 */
const LIST_REDACT_KEYS: ReadonlySet<string> = new Set([
  "share_invite_token",
  "control_invite_token",
  "share_token",
  "control_token",
  "worker_token",
  "worker_token_hash",
  "share_token_hash",
  "control_token_hash",
  "share_invite_hash",
  "control_invite_hash",
]);

/** A plain object, as JSON produces. */
function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * A value the caller actually supplied.
 *
 * The reference tests these with `or`, so a zero is falsy there too — it
 * reaches the same answer here because every numeric fallback is itself zero.
 */
function supplied<T>(value: T | undefined | null | ""): T | undefined {
  return value === undefined || value === null || value === "" ? undefined : (value as T);
}

/** One session's key. */
function sessionKey(workerId: string): string {
  return `${KV_PREFIX}${workerId}`;
}

/** Read and parse an entry, or nothing at all. */
async function readEntry(kv: SessionRegistryKV, key: string): Promise<Record<string, unknown>> {
  let raw: string | null | undefined;
  try {
    raw = await kv.get(key);
  } catch {
    // Unreadable is treated as absent: the status is still worth writing, and
    // losing it as well as the merge would take the session out of the fleet
    // list entirely.
    return {};
  }
  if (raw === null || raw === undefined) {
    return {};
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    return isObject(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * Write, or remove, this session's entry.
 *
 * A read-modify-write, not an overwrite. The tunnel API keeps credential
 * hashes, a revoked flag, expiry and one-time invites in this same key, and
 * this runs every sixty seconds from the alarm — a blind write nulled tunnel,
 * share and control auth about a minute after every worker reconnect. The
 * status fields go *over* what is there, so create, revoke and rotate stay
 * authoritative for the credentials while this stays authoritative for the
 * status.
 *
 * Does nothing when no registry is bound, which is how a deployment without
 * one runs at all.
 */
export async function updateKvSession(
  env: RegistryEnv,
  workerId: string,
  options: UpdateKvSessionOptions,
): Promise<void> {
  // Every call below is wrapped, so an absent binding would be swallowed
  // rather than raise — this returns before doing the work at all.
  const kv = env.SESSION_REGISTRY;
  if (kv === undefined) {
    return;
  }
  const key = sessionKey(workerId);

  if (options.connected === false && (options.remove_offline ?? true)) {
    // A stopped session leaves the fleet list rather than lingering in it.
    try {
      await kv.delete(key);
    } catch {
      // Nothing to do about it; the safety-net expiry will catch the entry.
    }
    return;
  }

  const existing = await readEntry(kv, key);
  // No connection state means "leave it as it was" — a heartbeat that does
  // not know must not report the session as gone.
  const connected = options.connected ?? Boolean(existing.connected);
  const meta = options.meta ?? {};

  const status: Record<string, unknown> = {
    session_id: workerId,
    display_name: supplied(meta.display_name) ?? workerId,
    created_at: supplied(meta.created_at) ?? 0.0,
    connector_type: supplied(meta.connector_type) ?? "unknown",
    lifecycle_state: connected ? "running" : "stopped",
    input_mode: options.input_mode ?? "hijack",
    connected,
    auto_start: false,
    tags: supplied(meta.tags) ?? [],
    recording_enabled: options.recording_enabled ?? true,
    recording_available: options.recording_available ?? false,
    owner: meta.owner ?? null,
    visibility: supplied(meta.visibility) ?? "public",
    last_error: null,
    hijacked: options.hijacked ?? false,
  };

  try {
    await kv.put(key, JSON.stringify({ ...existing, ...status }));
  } catch {
    // A network hop that failed. The session keeps running.
  }
}

/**
 * Read one session's entry.
 *
 * Unredacted: a Durable Object reads its own entry to bootstrap a tunnel and
 * needs the credential material a listing must not hand out.
 */
export async function getKvSession(env: RegistryEnv, workerId: string): Promise<Record<string, unknown> | undefined> {
  const kv = env.SESSION_REGISTRY;
  if (kv === undefined) {
    return undefined;
  }
  try {
    const raw = await kv.get(sessionKey(workerId));
    // An empty value is absent rather than a parse failure to swallow. Both
    // end in nothing, but only one of them is a store that lost the entry.
    if (raw === null || raw === undefined || raw === "") {
      return undefined;
    }
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return undefined;
  }
}

/** Remove one session's entry. */
export async function deleteKvSession(env: RegistryEnv, workerId: string): Promise<void> {
  const kv = env.SESSION_REGISTRY;
  if (kv === undefined) {
    return;
  }
  try {
    await kv.delete(sessionKey(workerId));
  } catch {
    // As above: the expiry will catch it.
  }
}

/**
 * The key names in a listing, whatever shape it arrived in.
 *
 * The shape checks reach the same answer as letting the caller's `try` catch
 * a malformed listing, but they say that a listing which is not one yields no
 * sessions rather than being an error to swallow.
 */
function listedKeys(result: unknown): string[] {
  if (!isObject(result)) {
    return [];
  }
  const keys = result.keys;
  if (!Array.isArray(keys)) {
    return [];
  }
  const names: string[] = [];
  for (const entry of keys) {
    // Read as a property of an object rather than off anything: a listing
    // entry that is not one has no name, which the string test below would
    // also conclude.
    const name = isObject(entry) ? entry.name : undefined;
    // An empty name is skipped here rather than fetched and found missing —
    // the same listing either way, one fewer round trip.
    if (typeof name === "string" && name !== "") {
      names.push(name);
    }
  }
  return names;
}

/**
 * Every session's entry, with credential material stripped.
 *
 * One unreadable entry is stepped over rather than costing the listing.
 */
export async function listKvSessions(env: RegistryEnv): Promise<Array<Record<string, unknown>>> {
  const kv = env.SESSION_REGISTRY;
  if (kv === undefined) {
    return [];
  }
  let names: string[];
  try {
    names = listedKeys(await kv.list({ prefix: KV_PREFIX }));
  } catch {
    return [];
  }

  const sessions: Array<Record<string, unknown>> = [];
  for (const name of names) {
    try {
      const raw = await kv.get(name);
      if (raw === null || raw === undefined || raw === "") {
        continue;
      }
      const entry: unknown = JSON.parse(raw);
      if (isObject(entry)) {
        sessions.push(Object.fromEntries(Object.entries(entry).filter(([key]) => !LIST_REDACT_KEYS.has(key))));
      }
    } catch {
      // A corrupt document costs itself, not the listing.
    }
  }
  return sessions;
}
