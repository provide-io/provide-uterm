//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The `/gui/` REST surface: how somebody reaches a remote screen.
 *
 * Port of `provide.uterm.server.bridge.routes.rest_gui`, path-compatible with
 * the C# canonical (`Server/UtermServer.Gui.cs`) and the Go port. Six
 * handlers — attach, screenshot, click, type, key, drag — and what they refuse
 * matters as much as what they do:
 *
 * * **Attaching** needs the capability, a target the caller's tenant may see,
 *   and a protocol this system speaks. The order is the contract: a caller
 *   that could tell "forbidden" from "not found" could enumerate another
 *   tenant's consoles by name, so a target it may not see is simply not
 *   there.
 * * **Injecting** is bound to the principal who took the lease, not to
 *   whoever holds the hijack id. Possession of an unguessable string is not
 *   the same as being the one who asked for it. A lease nobody claimed keeps
 *   the older model, where possession *was* the capability.
 *
 * As with the sibling hijack routes, there is no per-request authentication
 * here: session-level gating is applied by the dependency that protects the
 * hub router. Mount these behind it.
 */

import {
  type GraphicalTarget,
  type GraphicalTargetScope,
  PROTOCOL_MEMORY,
  scopeForTenant,
} from "../graphical/index.ts";
import { encodeRgbaPng, type GraphicalSession, MemoryGraphicalSession } from "./session.ts";

/** The capability attaching a console needs. */
export const CAP_GRAPHICAL_ATTACH = "graphical.session.attach";

/** Key name to X11 keysym, as the C# and Go ports spell it. */
export const GUI_KEY_SYMS: Readonly<Record<string, number>> = {
  Enter: 0xff0d,
  Tab: 0xff09,
  Esc: 0xff1b,
  Backspace: 0xff08,
  Up: 0xff52,
  Down: 0xff54,
  Left: 0xff51,
  Right: 0xff53,
};

/** Button name to the RFB bitmask that stands for it. */
export const GUI_BUTTON_MASKS: Readonly<Record<string, number>> = { left: 1, middle: 2, right: 4 };

/** What a handler answers with. */
export interface RouteResponse {
  status: number;
  body: unknown;
}

/** As much of a worker's state as these handlers touch. */
export interface GuiWorkerState {
  graphicalSession: GraphicalSession | null;
}

/** The hijack lease behind a request. */
export interface GuiLease {
  /** When the lease runs out, on a clock that only counts forward. */
  leaseExpiresAt: number;
  /** Who took it, or nothing for a lease taken before that was recorded. */
  acquiredBy: string | null;
}

/** The hub surface these handlers drive. */
export interface GuiHub {
  registry: {
    get(workerId: string): GuiWorkerState | undefined;
    put(workerId: string, state: GuiWorkerState): void;
  };
  getRestSession(workerId: string, hijackId: string): Promise<GuiLease | null>;
}

/** Two clocks read at the same instant. */
export interface Clocks {
  /** Seconds since the epoch. */
  wall: number;
  /** Seconds on a clock that only counts forward from an arbitrary zero. */
  monotonic: number;
}

/** Everything a handler reaches outside the request. */
export interface GuiDeps {
  hub: GuiHub;
  authz: { hasCapability(principal: unknown, capability: string): Promise<boolean> };
  targets: { get(scope: GraphicalTargetScope, targetId: string): GraphicalTarget | null };
  clock?: () => Clocks;
}

/** A request, as much of one as these handlers read. */
export interface GuiRequest {
  principal: { tenantId?: unknown; subjectId?: unknown } | null;
  /** The parsed body. Anything unreadable arrives as nothing. */
  body?: unknown;
}

/** A lease expiry as a wall-clock instant a client can read. */
export function monoToWall(monotonic: number, clock: () => Clocks): number {
  const now = clock();
  return now.wall + (monotonic - now.monotonic);
}

/** A body as an object; anything else — a list, a number, nothing — is empty. */
function bodyObject(body: unknown): Record<string, unknown> {
  return typeof body === "object" && body !== null && !Array.isArray(body) ? (body as Record<string, unknown>) : {};
}

/**
 * An integer body field, tolerating one written as text.
 *
 * A client that sent `"12"` meant twelve. One that sent `12.5`, `true` or a
 * list meant something this field cannot hold, and guessing would put the
 * pointer somewhere nobody asked for — so the default stands.
 */
export function intField(body: Record<string, unknown>, key: string, fallback = 0): number {
  const raw = key in body ? body[key] : fallback;
  // The reference tests for a boolean first, because a Python `bool` *is* an
  // `int` and `true` would otherwise be read as the coordinate 1. Here it is
  // simply not a number, so the same answer falls out of the tests below.
  if (typeof raw === "number") {
    return Number.isInteger(raw) ? raw : fallback;
  }
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    return /^[+-]?[0-9]+$/.test(trimmed) ? Number(trimmed) : fallback;
  }
  return fallback;
}

/** A text body field, or the default where it is anything else. */
export function strField(body: Record<string, unknown>, key: string, fallback = ""): string {
  const raw = key in body ? body[key] : fallback;
  return typeof raw === "string" ? raw : fallback;
}

/** Attach a graphical session to a worker. */
export async function guiAttach(deps: GuiDeps, request: GuiRequest, workerId: string): Promise<RouteResponse> {
  // First, so a refusal says nothing about which targets exist.
  if (!(await deps.authz.hasCapability(request.principal, CAP_GRAPHICAL_ATTACH))) {
    return { status: 403, body: { error: "insufficient privileges" } };
  }

  const body = bodyObject(request.body);
  const targetId = strField(body, "target_id").trim();
  if (targetId === "") {
    return { status: 422, body: { error: "target_id is required for gui attach" } };
  }

  const tenantId = request.principal?.tenantId;
  const scope = scopeForTenant(typeof tenantId === "string" ? tenantId : "");
  if (scope === null) {
    return { status: 403, body: { error: "graphical target access denied" } };
  }

  const target = deps.targets.get(scope, targetId);
  if (target === null) {
    // Not "forbidden": a caller able to tell the two apart could enumerate
    // another tenant's consoles by name.
    return { status: 404, body: { error: "target not found" } };
  }

  const protocol = target.protocol.trim().toLowerCase();
  if (protocol !== PROTOCOL_MEMORY) {
    return { status: 501, body: { error: `graphical protocol not supported: ${protocol}` } };
  }

  // Floored at one pixel: a size the framebuffer would refuse can only come
  // from a store written behind the registry's back, and refusing to serve is
  // worse than serving something small.
  const session = new MemoryGraphicalSession(Math.max(1, target.width), Math.max(1, target.height));
  let state = deps.hub.registry.get(workerId);
  if (state === undefined) {
    state = { graphicalSession: null };
    deps.hub.registry.put(workerId, state);
  }
  state.graphicalSession = session;
  return { status: 200, body: { ok: true, target_id: targetId } };
}

/** The console attached to a worker, or nothing. */
function graphicalSession(deps: GuiDeps, workerId: string): GraphicalSession | null {
  return deps.hub.registry.get(workerId)?.graphicalSession ?? null;
}

/** Capture the console behind an active lease. */
export async function guiScreenshot(deps: GuiDeps, workerId: string, hijackId: string): Promise<RouteResponse> {
  const lease = await deps.hub.getRestSession(workerId, hijackId);
  if (lease === null) {
    return { status: 404, body: { error: "Invalid or expired hijack session." } };
  }
  const gui = graphicalSession(deps, workerId);
  if (gui === null) {
    return { status: 404, body: { error: "No graphical session attached." } };
  }
  const image = gui.screenshot();
  const png = encodeRgbaPng(image.width, image.height, image.pixels);
  return {
    status: 200,
    body: {
      ok: true,
      worker_id: workerId,
      hijack_id: hijackId,
      screenshot: Buffer.from(png).toString("base64"),
      lease_expires_at: monoToWall(lease.leaseExpiresAt, deps.clock ?? systemClock),
    },
  };
}

/** The two clocks, read from this runtime. */
function systemClock(): Clocks {
  return { wall: Date.now() / 1000, monotonic: performance.now() / 1000 };
}

/**
 * The console behind an active lease the caller owns, or the refusal.
 *
 * Injecting is principal-bound: the caller must be the one who took the
 * lease, not merely hold the hijack id. A lease nobody claimed keeps the
 * older model, where possession of an unguessable string *was* the capability.
 */
async function requireSession(
  deps: GuiDeps,
  request: GuiRequest,
  workerId: string,
  hijackId: string,
): Promise<GraphicalSession | RouteResponse> {
  const lease = await deps.hub.getRestSession(workerId, hijackId);
  if (lease === null) {
    return { status: 404, body: { error: "Invalid or expired hijack session." } };
  }
  const requester = principalSubject(request);
  if (lease.acquiredBy !== null && requester !== lease.acquiredBy) {
    return { status: 403, body: { error: "hijack lease not owned by caller" } };
  }
  const gui = graphicalSession(deps, workerId);
  if (gui === null) {
    return { status: 404, body: { error: "No graphical session attached." } };
  }
  return gui;
}

/**
 * Who is asking, or nobody.
 *
 * A principal that never arrived and one carrying no subject are the same
 * thing here — neither may inherit somebody else's lease.
 */
function principalSubject(request: GuiRequest): string | null {
  const subject = request.principal?.subjectId ?? null;
  return subject === null ? null : String(subject);
}

/** Whether a resolution came back as a refusal. */
function isRefusal(value: GraphicalSession | RouteResponse): value is RouteResponse {
  return "status" in value;
}

/** Inject a pointer click. */
export async function guiClick(
  deps: GuiDeps,
  request: GuiRequest,
  workerId: string,
  hijackId: string,
): Promise<RouteResponse> {
  const gui = await requireSession(deps, request, workerId, hijackId);
  if (isRefusal(gui)) {
    return gui;
  }
  const body = bodyObject(request.body);
  const button = strField(body, "button", "left");
  const mask = GUI_BUTTON_MASKS[button];
  if (mask === undefined) {
    return { status: 422, body: { error: "invalid button: must be left, middle, or right" } };
  }
  const x = intField(body, "x");
  const y = intField(body, "y");
  // Down then up: a button left held drags everything the pointer touches
  // afterwards.
  gui.injectPointer(x, y, mask);
  gui.injectPointer(x, y, 0);
  return { status: 200, body: { ok: true } };
}

/** Inject typed text, a character at a time. */
export async function guiType(
  deps: GuiDeps,
  request: GuiRequest,
  workerId: string,
  hijackId: string,
): Promise<RouteResponse> {
  const gui = await requireSession(deps, request, workerId, hijackId);
  if (isRefusal(gui)) {
    return gui;
  }
  // By code point, as the reference iterates: split into two, a single emoji
  // would arrive as two keys that are not the one that was typed.
  for (const character of strField(body(request), "text")) {
    const keySym = character.codePointAt(0) as number;
    gui.injectKey(keySym, true);
    gui.injectKey(keySym, false);
  }
  return { status: 200, body: { ok: true } };
}

/** Inject a named key. */
export async function guiKey(
  deps: GuiDeps,
  request: GuiRequest,
  workerId: string,
  hijackId: string,
): Promise<RouteResponse> {
  const gui = await requireSession(deps, request, workerId, hijackId);
  if (isRefusal(gui)) {
    return gui;
  }
  // A name nobody knows sends nothing rather than a refusal, as the reference
  // does: the key table is a convenience, not the contract.
  const keySym = GUI_KEY_SYMS[strField(body(request), "key_name")] ?? 0;
  gui.injectKey(keySym, true);
  gui.injectKey(keySym, false);
  return { status: 200, body: { ok: true } };
}

/** Inject a pointer drag. */
export async function guiDrag(
  deps: GuiDeps,
  request: GuiRequest,
  workerId: string,
  hijackId: string,
): Promise<RouteResponse> {
  const gui = await requireSession(deps, request, workerId, hijackId);
  if (isRefusal(gui)) {
    return gui;
  }
  const fields = body(request);
  const startX = intField(fields, "start_x");
  const startY = intField(fields, "start_y");
  const endX = intField(fields, "end_x");
  const endY = intField(fields, "end_y");
  // Held between the two points, then released where it lands.
  gui.injectPointer(startX, startY, 1);
  gui.injectPointer(endX, endY, 1);
  gui.injectPointer(endX, endY, 0);
  return { status: 200, body: { ok: true } };
}

/** A request's body as an object. */
function body(request: GuiRequest): Record<string, unknown> {
  return bodyObject(request.body);
}
