//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Graphical targets: what a remote console is, and who may reach it.
 *
 * Port of `provide.uterm.server.graphical_targets`, which is itself a port of
 * the C# canonical (`Provide.Uterm/Server/GraphicalTargets.cs`) and the Go
 * `graphical` package. A graphical target says where a remote console lives
 * and how to authenticate to it, so three things decide whether one tenant
 * can end up looking at another's screen:
 *
 * * **What a definition may say.** An identifier is one safe name, a protocol
 *   is one this system speaks, a size is a size, and a secret reference names
 *   an environment variable or an absolute file — never something arbitrary
 *   that a loader might follow.
 * * **Where the endpoint actually points**, decided by one grammar per
 *   protocol. An endpoint read wrongly is a console somewhere nobody asked
 *   for.
 * * **Who may see it.** A {@link GraphicalTargetScope} is derived from the
 *   authenticated principal and never from client input: exactly one of the
 *   system scope or a single tenant, and a tenant scope permits only its own.
 *
 * {@link publicCopy} strips every secret from anything crossing the REST
 * boundary, so a target's password is never in a listing.
 */

import { bracketsAreValid, netlocOf } from "./cpython-netloc.ts";

/** The in-process protocol, for tests and for a console with no network. */
export const PROTOCOL_MEMORY = "memory";

/** RFB, which is what VNC speaks. */
export const PROTOCOL_RFB = "rfb";

/** The litevirt gRPC control plane. */
export const PROTOCOL_LITEVIRT = "litevirt";

/** Every protocol a target may name. */
export const SUPPORTED_PROTOCOLS: ReadonlySet<string> = new Set([PROTOCOL_MEMORY, PROTOCOL_RFB, PROTOCOL_LITEVIRT]);

/** The coded reasons a registry or a validation refuses. */
export const GRAPHICAL_TARGET_ERROR_CODES = {
  ALREADY_EXISTS: "ALREADY_EXISTS",
  NOT_FOUND: "NOT_FOUND",
  IMMUTABLE: "IMMUTABLE",
  FORBIDDEN: "FORBIDDEN",
  CONFLICT: "CONFLICT",
  INVALID: "INVALID",
  CLOSED: "CLOSED",
  BACKEND: "BACKEND",
} as const;

/** One of {@link GRAPHICAL_TARGET_ERROR_CODES}. */
export type GraphicalTargetErrorCode = (typeof GRAPHICAL_TARGET_ERROR_CODES)[keyof typeof GRAPHICAL_TARGET_ERROR_CODES];

/** A refusal carrying the code a caller is answered with. */
export class GraphicalTargetError extends Error {
  readonly code: GraphicalTargetErrorCode;

  constructor(code: GraphicalTargetErrorCode, message: string) {
    super(message);
    this.name = "GraphicalTargetError";
    this.code = code;
  }
}

/**
 * A safe identifier, for both a target and a tenant.
 *
 * The trailing `\n?` is not decoration: the reference anchors with `$`, which
 * in Python matches before a final newline. Carried over rather than
 * tightened, so the port accepts exactly what the reference accepts — a name
 * one runtime stores and the other rejects is a target that exists on only
 * half of a deployment.
 */
const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\n?$/;

/** An environment variable or an absolute file, and nothing else. */
const SECRET_REF_PATTERN = /^(?:env:[A-Za-z_][A-Za-z0-9_]*|file:\/[^\0]+)\n?$/;

/** The largest console this accepts, in either direction. */
const MAX_DIMENSION = 8192;

/** A single graphical-console definition. */
export interface GraphicalTarget {
  targetId: string;
  tenantId: string;
  displayName: string;
  protocol: string;
  endpoint: string | null;
  secret: string | null;
  width: number;
  height: number;
  isSystem: boolean;
  isStatic: boolean;
  caSecretRef: string | null;
  clientCertSecretRef: string | null;
  clientKeySecretRef: string | null;
  createdBy: string | null;
  createdAt: Date;
  updatedBy: string | null;
  updatedAt: Date | null;
  /**
   * Per-target, protocol-specific parameters — the litevirt `vm_name`, say.
   * Not a secret, so it survives {@link publicCopy}.
   */
  config: Record<string, unknown>;
}

/** A target with the reference's defaults filled in. */
export function makeGraphicalTarget(fields: Partial<GraphicalTarget> = {}): GraphicalTarget {
  return {
    targetId: "",
    tenantId: "",
    displayName: "",
    protocol: PROTOCOL_RFB,
    endpoint: null,
    secret: null,
    width: 640,
    height: 480,
    isSystem: false,
    isStatic: false,
    caSecretRef: null,
    clientCertSecretRef: null,
    clientKeySecretRef: null,
    createdBy: null,
    createdAt: new Date(),
    updatedBy: null,
    updatedAt: null,
    ...fields,
    // Copied rather than kept: two targets sharing one settings map is one
    // target's parameters showing up in another's session.
    config: { ...fields.config },
  };
}

/** A copy whose settings map is its own. */
export function cloneTarget(target: GraphicalTarget): GraphicalTarget {
  return { ...target, config: { ...target.config } };
}

/** A copy with every secret stripped, for anything crossing the REST boundary. */
export function publicCopy(target: GraphicalTarget): GraphicalTarget {
  const copy = cloneTarget(target);
  copy.secret = null;
  copy.caSecretRef = null;
  copy.clientCertSecretRef = null;
  copy.clientKeySecretRef = null;
  return copy;
}

/**
 * A time as Python's `datetime.isoformat` writes an aware UTC one.
 *
 * Byte-for-byte, because the wire shape is what a client parses and what a
 * signature covers: `2026-01-02T03:04:05+00:00`, with six-digit microseconds
 * only when there are any.
 */
export function wireTimestamp(value: Date): string {
  const iso = value.toISOString();
  const seconds = iso.slice(0, 19);
  const milliseconds = iso.slice(20, 23);
  const fraction = milliseconds === "000" ? "" : `.${milliseconds}000`;
  return `${seconds}${fraction}+00:00`;
}

/** The snake_case wire shape, with null optionals omitted. */
export function toWireDict(target: GraphicalTarget): Record<string, unknown> {
  const data: Record<string, unknown> = {
    target_id: target.targetId,
    tenant_id: target.tenantId,
    display_name: target.displayName,
    protocol: target.protocol,
    width: target.width,
    height: target.height,
    is_system: target.isSystem,
    is_static: target.isStatic,
    created_at: wireTimestamp(target.createdAt),
  };
  if (target.endpoint !== null) {
    data.endpoint = target.endpoint;
  }
  if (target.secret !== null) {
    data.secret = target.secret;
  }
  if (target.caSecretRef !== null) {
    data.ca_secret_ref = target.caSecretRef;
  }
  if (target.clientCertSecretRef !== null) {
    data.client_cert_secret_ref = target.clientCertSecretRef;
  }
  if (target.clientKeySecretRef !== null) {
    data.client_key_secret_ref = target.clientKeySecretRef;
  }
  if (target.createdBy !== null) {
    data.created_by = target.createdBy;
  }
  if (target.updatedBy !== null) {
    data.updated_by = target.updatedBy;
  }
  if (target.updatedAt !== null) {
    data.updated_at = wireTimestamp(target.updatedAt);
  }
  // By truth, not by presence: the reference writes the map only when there
  // is something in it, so an empty one is left out.
  if (Object.keys(target.config).length > 0) {
    data.config = { ...target.config };
  }
  return data;
}

/**
 * Normalise the protocol and endpoint in place; throw on any violation.
 *
 * In place, as the reference does: what is stored afterwards is the endpoint
 * as it was read, not as it was typed, so two spellings of one console are
 * one entry rather than two.
 */
export function validateTarget(target: GraphicalTarget): void {
  if (!NAME_PATTERN.test(target.targetId)) {
    throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.INVALID, "target_id must be a safe identifier");
  }

  const protocol = target.protocol.trim().toLowerCase();
  if (!SUPPORTED_PROTOCOLS.has(protocol)) {
    throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.INVALID, "unsupported protocol");
  }
  target.protocol = protocol;

  if (protocol === PROTOCOL_RFB) {
    const [host, port] = parseRfbEndpoint(target.endpoint);
    target.endpoint = `${host}:${port}`;
  } else if (protocol === PROTOCOL_LITEVIRT) {
    const [host, port] = parseLitevirtEndpoint(target.endpoint);
    target.endpoint = `${host}:${port}`;
  }

  if (target.width < 1 || target.width > MAX_DIMENSION) {
    throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.INVALID, "width out of range");
  }
  if (target.height < 1 || target.height > MAX_DIMENSION) {
    throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.INVALID, "height out of range");
  }

  // A blank tenant is no tenant, which is allowed; anything written is held
  // to the same pattern a target id is.
  if (target.tenantId.trim() !== "" && !NAME_PATTERN.test(target.tenantId)) {
    throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.INVALID, "tenant_id is invalid");
  }

  for (const ref of [target.caSecretRef, target.clientCertSecretRef, target.clientKeySecretRef]) {
    if (ref !== null && !SECRET_REF_PATTERN.test(ref)) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.INVALID, "invalid secret reference syntax");
    }
  }
}

/**
 * Host and port from a netloc, exactly as CPython's `SplitResult` reads them.
 *
 * Any credentials in front are dropped, a bracketed address gives up its
 * brackets, and the host is lowercased — except for an IPv6 zone, which names
 * an interface and is not a hostname.
 *
 * Three readings here cannot be told apart from outside, because a netloc
 * whose host comes out empty is refused before the port is consulted: whether
 * a leading `:` yields the port or nothing, whether `[]` keeps the text after
 * the bracket, and whether the closing bracket is dropped before the port is
 * looked for (it holds no colon either way).
 */
function hostAndPortOf(netloc: string): { host: string | null; port: string | null } {
  // A netloc CPython refuses to read at all — a bracket without its partner,
  // brackets out of order, or a bracketed host that is not an address. Every
  // caller turns an unusable netloc into the same refusal it gives a hostless
  // one, so it is reported the same way.
  if (!bracketsAreValid(netloc)) {
    return { host: null, port: null };
  }
  const at = netloc.lastIndexOf("@");
  const hostinfo = at === -1 ? netloc : netloc.slice(at + 1);

  let rawHost: string;
  let rawPort: string;
  const open = hostinfo.indexOf("[");
  if (open === -1) {
    const colon = hostinfo.indexOf(":");
    rawHost = colon === -1 ? hostinfo : hostinfo.slice(0, colon);
    rawPort = colon === -1 ? "" : hostinfo.slice(colon + 1);
  } else {
    const bracketed = hostinfo.slice(open + 1);
    const close = bracketed.indexOf("]");
    rawHost = close === -1 ? bracketed : bracketed.slice(0, close);
    const afterHost = close === -1 ? "" : bracketed.slice(close + 1);
    const colon = afterHost.indexOf(":");
    rawPort = colon === -1 ? "" : afterHost.slice(colon + 1);
  }

  if (rawHost === "") {
    // No port either: every caller refuses a netloc with no host before it
    // ever looks at one, so reporting it would be reporting nothing.
    return { host: null, port: null };
  }
  const zone = rawHost.indexOf("%");
  const host = zone === -1 ? rawHost.toLowerCase() : rawHost.slice(0, zone).toLowerCase() + rawHost.slice(zone);
  return { host, port: rawPort === "" ? null : rawPort };
}

/**
 * The port a netloc names, or a refusal.
 *
 * ASCII digits only and inside the range a port has. CPython raises for
 * anything else and the reference answers every one of those the same way, so
 * there is a single message here too.
 */
function portOf(raw: string | null): number {
  if (raw === null || !/^[0-9]+$/.test(raw)) {
    throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.INVALID, "invalid endpoint port");
  }
  const port = Number(raw);
  if (port < 1 || port > 65535) {
    throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.INVALID, "invalid endpoint port");
  }
  return port;
}

/**
 * Where an rfb endpoint points: `host:port`, `rfb://host:port`, or either
 * behind a `dns:///` prefix.
 */
export function parseRfbEndpoint(rawEndpoint: string | null): [string, number] {
  const raw = rawEndpoint ?? "";
  if (raw.trim() === "") {
    throw new GraphicalTargetError(
      GRAPHICAL_TARGET_ERROR_CODES.INVALID,
      `endpoint is required for protocol ${PROTOCOL_RFB}`,
    );
  }

  let endpoint = raw.trim();
  if (endpoint.toLowerCase().startsWith("dns:///")) {
    endpoint = endpoint.slice("dns:///".length);
  }

  if (!endpoint.toLowerCase().startsWith("rfb://")) {
    if (!endpoint.includes(":")) {
      throw new GraphicalTargetError(
        GRAPHICAL_TARGET_ERROR_CODES.INVALID,
        "invalid endpoint; expected host:port or rfb://host:port",
      );
    }
    endpoint = `rfb://${endpoint}`;
  }

  const { host, port } = hostAndPortOf(netlocOf(endpoint));
  if (host === null) {
    throw new GraphicalTargetError(
      GRAPHICAL_TARGET_ERROR_CODES.INVALID,
      "invalid endpoint; expected host:port or rfb://host:port",
    );
  }
  return [host, portOf(port)];
}

/**
 * Where a litevirt endpoint points: a plain `host:port`, optionally behind a
 * `dns:///` prefix.
 *
 * Unlike rfb it carries no scheme of its own, so a caller who pasted an rfb
 * address here has named a different service and is told so.
 */
export function parseLitevirtEndpoint(rawEndpoint: string | null): [string, number] {
  const raw = rawEndpoint ?? "";
  if (raw.trim() === "") {
    throw new GraphicalTargetError(
      GRAPHICAL_TARGET_ERROR_CODES.INVALID,
      `endpoint is required for protocol ${PROTOCOL_LITEVIRT}`,
    );
  }

  let endpoint = raw.trim();
  if (endpoint.toLowerCase().startsWith("dns:///")) {
    endpoint = endpoint.slice("dns:///".length);
  }

  // Wrapped in a throwaway scheme purely to lean on the same host:port
  // reading the reference leans on `urlparse` for.
  const { host, port } = hostAndPortOf(netlocOf(`grpc://${endpoint}`));
  if (host === null) {
    throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.INVALID, "invalid endpoint; expected host:port");
  }
  return [host, portOf(port)];
}

/**
 * What an authenticated principal may reach.
 *
 * Derived from the principal, NEVER from client input: either a single tenant
 * or the whole system.
 */
export interface GraphicalTargetScope {
  tenantId: string | null;
  isSystem: boolean;
}

/**
 * Whether a scope is one of the two things it may be.
 *
 * A scope that is both would let one tenant reach the system's consoles; one
 * that is neither is a caller who was never authenticated.
 */
export function scopeIsValid(scope: GraphicalTargetScope): boolean {
  return scope.isSystem !== (scope.tenantId !== null);
}

/** Whether a scope may reach a target owned by `tenantId`. */
export function scopePermits(scope: GraphicalTargetScope, tenantId: string | null): boolean {
  if (!scopeIsValid(scope)) {
    return false;
  }
  if (scope.isSystem) {
    return true;
  }
  return tenantId !== null && tenantId === scope.tenantId;
}

/**
 * The scope for a tenant, or nothing when the tenant is blank.
 *
 * Nothing rather than an empty scope: an empty tenant id would otherwise
 * match every target nobody owns.
 */
export function scopeForTenant(tenantId: string): GraphicalTargetScope | null {
  if (tenantId.trim() === "") {
    return null;
  }
  // Untrimmed, deliberately: only the emptiness test trims, so the scope
  // carries the id exactly as it was presented.
  return { tenantId, isSystem: false };
}

/** The scope for seeded and system-owned targets. */
export function systemScope(): GraphicalTargetScope {
  return { tenantId: null, isSystem: true };
}

/**
 * The registry: immutable seeded targets alongside mutable runtime ones.
 *
 * Every read and every write is gated by a scope. The reference guards its
 * maps with a lock; nothing here can interleave, so there is none.
 */
export class InMemoryGraphicalTargetRegistry {
  readonly #static = new Map<string, GraphicalTarget>();
  readonly #runtime = new Map<string, GraphicalTarget>();
  readonly #now: () => Date;
  #closed = false;

  constructor(now: () => Date = () => new Date()) {
    this.#now = now;
  }

  /** Mark closed; every subsequent scoped operation refuses. */
  close(): void {
    this.#closed = true;
  }

  #ensureOpen(scope: GraphicalTargetScope): void {
    if (this.#closed) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.CLOSED, "graphical target registry is closed");
    }
    if (!scopeIsValid(scope)) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.FORBIDDEN, "graphical target tenant scope denied");
    }
  }

  /** The target this scope may reach under that id, seeded first, else null. */
  get(scope: GraphicalTargetScope, targetId: string): GraphicalTarget | null {
    this.#ensureOpen(scope);
    const seeded = this.#static.get(targetId);
    if (seeded !== undefined && scopePermits(scope, seeded.tenantId)) {
      return cloneTarget(seeded);
    }
    const runtime = this.#runtime.get(targetId);
    if (runtime !== undefined && scopePermits(scope, runtime.tenantId)) {
      return cloneTarget(runtime);
    }
    return null;
  }

  /** Runtime and seeded merged (seeded wins), scope-filtered, ordered by id. */
  list(scope: GraphicalTargetScope): GraphicalTarget[] {
    this.#ensureOpen(scope);
    const merged = new Map<string, GraphicalTarget>();
    for (const [targetId, target] of this.#runtime) {
      if (scopePermits(scope, target.tenantId)) {
        merged.set(targetId, cloneTarget(target));
      }
    }
    for (const [targetId, target] of this.#static) {
      if (scopePermits(scope, target.tenantId)) {
        merged.set(targetId, cloneTarget(target));
      }
    }
    return [...merged.keys()].sort().map((targetId) => cloneTarget(merged.get(targetId) as GraphicalTarget));
  }

  /** Insert a new runtime target. */
  create(scope: GraphicalTargetScope, target: GraphicalTarget): GraphicalTarget {
    this.#ensureOpen(scope);
    const clone = cloneTarget(target);
    // Before anything else, so a caller learns nothing about a tenant it
    // cannot reach — not even whether an identifier is taken.
    if (!scopePermits(scope, clone.tenantId)) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.FORBIDDEN, "graphical target tenant scope denied");
    }
    validateTarget(clone);
    if (this.#static.has(clone.targetId) || this.#runtime.has(clone.targetId)) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.ALREADY_EXISTS, "graphical target already exists");
    }
    // Stamped here, not taken from the caller: a target claiming to predate
    // the audit trail is a target that was never approved.
    clone.createdAt = this.#now();
    this.#runtime.set(clone.targetId, clone);
    return cloneTarget(clone);
  }

  /** Replace an existing runtime target. */
  update(scope: GraphicalTargetScope, target: GraphicalTarget): GraphicalTarget {
    this.#ensureOpen(scope);
    const clone = cloneTarget(target);
    if (!scopePermits(scope, clone.tenantId)) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.FORBIDDEN, "graphical target tenant scope denied");
    }
    validateTarget(clone);
    if (this.#static.has(clone.targetId)) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.IMMUTABLE, "static graphical target is immutable");
    }
    const current = this.#runtime.get(clone.targetId);
    if (current === undefined) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.NOT_FOUND, "graphical target not found");
    }
    // Checked again against what is stored: the scope check above only says
    // the caller may write as the tenant it named, not that the target it is
    // replacing belongs to that tenant.
    if (!scopePermits(scope, current.tenantId)) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.FORBIDDEN, "graphical target tenant scope denied");
    }
    // An update may not rewrite who made a target or when.
    clone.createdAt = current.createdAt;
    clone.createdBy = current.createdBy;
    clone.updatedAt = this.#now();
    this.#runtime.set(clone.targetId, clone);
    return cloneTarget(clone);
  }

  /** Remove a runtime target. */
  delete(scope: GraphicalTargetScope, targetId: string): void {
    this.#ensureOpen(scope);
    const seeded = this.#static.get(targetId);
    if (seeded !== undefined) {
      if (!scopePermits(scope, seeded.tenantId)) {
        throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.FORBIDDEN, "graphical target tenant scope denied");
      }
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.IMMUTABLE, "static graphical target is immutable");
    }
    const current = this.#runtime.get(targetId);
    if (current === undefined) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.NOT_FOUND, "graphical target not found");
    }
    if (!scopePermits(scope, current.tenantId)) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.FORBIDDEN, "graphical target tenant scope denied");
    }
    this.#runtime.delete(targetId);
  }

  /**
   * Seed an immutable system target.
   *
   * Not scope-gated and not closed-gated, as the reference is not: seeding is
   * what a deployment does to itself at startup rather than something a
   * caller asks for.
   */
  addStatic(target: GraphicalTarget): void {
    const clone = cloneTarget(target);
    validateTarget(clone);
    clone.isSystem = true;
    if (this.#static.has(clone.targetId)) {
      throw new GraphicalTargetError(GRAPHICAL_TARGET_ERROR_CODES.CONFLICT, "duplicate graphical target_id");
    }
    this.#static.set(clone.targetId, clone);
  }
}
