//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * API key management.
 *
 * Port of the Python module `provide.uterm.server.api_keys`.
 *
 * The raw key is returned once and never stored — only its digest — so a
 * reader of the store cannot use what they find. The tenant id is a bounded
 * ASCII slug shared verbatim with the Go and C# ports, so the same tenant
 * validates identically on every surface.
 */

import { createHash, randomBytes } from "node:crypto";
import { digestsMatch } from "./digests.ts";

/**
 * A tenant id: an alphanumeric first character and up to 127 more of
 * `[A-Za-z0-9_.-]`.
 */
const TENANT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

/** What a caller is told when a tenant id will not do. */
export const INVALID_TENANT_MESSAGE = "tenant_id is required and must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$";

/** How many bytes of entropy a key carries. */
const KEY_BYTES = 32;

/** How much of the digest names the key. */
const KEY_ID_CHARS = 16;

/** One key record. The raw key is never part of it. */
export interface ApiKey {
  /** The first characters of the digest, used to refer to the key. */
  keyId: string;
  /** The digest of the full key. */
  keyHash: string;
  /** A human-readable label. */
  name: string;
  /** The owning tenant, or empty for a flat key. */
  tenantId: string;
  /** What the key is allowed to do. */
  scopes: ReadonlySet<string>;
  /** When it was minted. */
  createdAt: number;
  /** When it stops working, if ever. */
  expiresAt?: number | undefined;
  /** When it was last accepted. */
  lastUsedAt?: number | undefined;
  /** Whether it has been withdrawn. */
  revoked: boolean;
}

/** Options for {@link ApiKeyStore}. */
export interface ApiKeyStoreOptions {
  /** Wall clock in seconds. */
  now?: () => number;
  /** Where a raw key comes from. Injected so a test need not be random. */
  newKey?: () => string;
}

/**
 * The trimmed tenant id when it is one, or nothing.
 *
 * Empty or whitespace-only is absent rather than invalid; anything else has
 * to match the shared pattern.
 */
export function canonicalTenantId(tenantId: string | undefined): string | undefined {
  const text = (tenantId ?? "").trim();
  if (text === "") {
    return undefined;
  }
  return TENANT_PATTERN.test(text) ? text : undefined;
}

/** The digest of a raw key. */
function hashKey(rawKey: string): string {
  return createHash("sha256").update(rawKey, "utf8").digest("hex");
}

/** An in-memory key registry. */
export class ApiKeyStore {
  readonly #keys = new Map<string, ApiKey>();
  readonly #now: () => number;
  readonly #newKey: () => string;

  constructor(options: ApiKeyStoreOptions = {}) {
    this.#now = options.now ?? (() => Date.now() / 1000);
    this.#newKey = options.newKey ?? (() => randomBytes(KEY_BYTES).toString("base64url"));
  }

  /**
   * Mint a key.
   *
   * @returns The raw key and its record. The raw key is returned exactly
   *   once — it is not stored, so it cannot be produced again.
   */
  create(name: string, options: { scopes?: Iterable<string>; expiresInS?: number } = {}): [string, ApiKey] {
    const rawKey = this.#newKey();
    const keyHash = hashKey(rawKey);
    const record: ApiKey = {
      keyId: keyHash.slice(0, KEY_ID_CHARS),
      keyHash,
      name,
      tenantId: "",
      scopes: new Set(options.scopes ?? []),
      createdAt: this.#now(),
      // Only a non-zero lifetime sets an expiry, as in the reference: zero is
      // "no expiry", not "expires immediately".
      expiresAt: options.expiresInS ? this.#now() + options.expiresInS : undefined,
      lastUsedAt: undefined,
      revoked: false,
    };
    this.#keys.set(record.keyId, record);
    return [rawKey, record];
  }

  /**
   * Mint a key bound to a tenant.
   *
   * @throws {Error} When the tenant id is empty or malformed — a key with a
   *   tenant nobody can name belongs to nobody, and every tenant-scoped
   *   lookup would miss it.
   */
  createForTenant(
    tenantId: string,
    name: string,
    options: { scopes?: Iterable<string>; expiresInS?: number } = {},
  ): [string, ApiKey] {
    const tenant = canonicalTenantId(tenantId);
    if (tenant === undefined) {
      throw new Error(INVALID_TENANT_MESSAGE);
    }
    const [rawKey, record] = this.create(name, options);
    record.tenantId = tenant;
    return [rawKey, record];
  }

  /** The record for a raw key, if it is live. */
  validate(rawKey: string): ApiKey | undefined {
    const keyHash = hashKey(rawKey);
    for (const record of this.#keys.values()) {
      if (record.revoked) {
        continue;
      }
      // Expiry that is not enforced is a note in a database.
      if (record.expiresAt !== undefined && this.#now() > record.expiresAt) {
        continue;
      }
      if (digestsMatch(record.keyHash, keyHash)) {
        record.lastUsedAt = this.#now();
        return record;
      }
    }
    return undefined;
  }

  /** Withdraw a key. Reports whether there was one. */
  revoke(keyId: string): boolean {
    const record = this.#keys.get(keyId);
    if (record === undefined) {
      return false;
    }
    record.revoked = true;
    return true;
  }

  /**
   * Every key, revoked ones included.
   *
   * The listing is a record: a revoked key that vanished would take its own
   * audit trail with it.
   */
  listKeys(): ApiKey[] {
    return [...this.#keys.values()];
  }

  /** The live keys one tenant owns. Nothing for a tenant that is not one. */
  listKeysForTenant(tenantId: string): ApiKey[] {
    const tenant = canonicalTenantId(tenantId);
    if (tenant === undefined) {
      return [];
    }
    return [...this.#keys.values()].filter((record) => !record.revoked && record.tenantId === tenant);
  }

  /**
   * Withdraw a key, but only for the tenant that owns it.
   *
   * The whole point of the tenant field: one tenant must not be able to
   * revoke another's key by guessing its id.
   */
  revokeForTenant(keyId: string, tenantId: string): boolean {
    const tenant = canonicalTenantId(tenantId);
    if (tenant === undefined) {
      return false;
    }
    const record = this.#keys.get(keyId);
    if (record === undefined || record.tenantId !== tenant) {
      return false;
    }
    record.revoked = true;
    return true;
  }
}
