//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { InMemoryGraphicalTargetRegistry, makeGraphicalTarget } from "../graphical/index.ts";
import { loadGolden, must } from "../testing/golden.ts";
import {
  CAP_GRAPHICAL_ATTACH,
  type GraphicalSession,
  GUI_BUTTON_MASKS,
  GUI_KEY_SYMS,
  type GuiDeps,
  type GuiRequest,
  type GuiWorkerState,
  guiAttach,
  guiClick,
  guiDrag,
  guiKey,
  guiScreenshot,
  guiType,
  intField,
  MemoryGraphicalSession,
  monoToWall,
  type RgbaImage,
  strField,
} from "./index.ts";

interface Case {
  name: string;
  path: string;
  tenant?: string | null;
  allow?: boolean;
  body?: unknown;
  bad_json?: boolean;
  has_lease?: boolean;
  acquired_by?: string | null;
  attached?: boolean;
  known_worker?: boolean;
  drawn?: Array<[number, number]>;
  subject?: string;
  status: number;
  response: unknown;
  calls: Array<[string, ...number[]] | [string, number, boolean]>;
  console: string | null;
  lit: number[];
}

interface RoutesGolden {
  mono_to_wall: { wall_now: number; mono_now: number; samples: Array<{ mono: number; wall: number }> };
  capability: string;
  key_syms: Record<string, number>;
  button_masks: Record<string, number>;
  console: [number, number];
  paths: Record<string, string>;
  int_fields: Array<{ raw: unknown; value: number }>;
  int_field_absent: number;
  int_field_default: number;
  int_field_default_used: number;
  str_fields: Array<{ raw: unknown; value: string }>;
  str_field_absent: string;
  str_field_default: string;
  str_field_default_used: string;
  cases: Array<Case & { body: unknown }>;
}

const golden = loadGolden<RoutesGolden>("guiroutes_golden.json");

const WORKER = "gui-worker";
const HIJACK = "00000000-0000-0000-0000-000000000000";

/** A console that draws as the stub does and remembers what it was told. */
class RecordingSession implements GraphicalSession {
  readonly calls: unknown[][] = [];
  readonly #inner: MemoryGraphicalSession;

  constructor(width: number, height: number) {
    this.#inner = new MemoryGraphicalSession(width, height);
  }

  screenshot(): RgbaImage {
    return this.#inner.screenshot();
  }

  injectPointer(x: number, y: number, buttonMask: number): void {
    this.calls.push(["pointer", x, y, buttonMask]);
    this.#inner.injectPointer(x, y, buttonMask);
  }

  injectKey(keySym: number, down: boolean): void {
    this.calls.push(["key", keySym, down]);
    this.#inner.injectKey(keySym, down);
  }
}

/** The seeded targets the corpus was recorded against. */
function seededTargets(): InMemoryGraphicalTargetRegistry {
  const registry = new InMemoryGraphicalTargetRegistry();
  const [width, height] = golden.console;
  registry.addStatic(makeGraphicalTarget({ targetId: "gt-mem", tenantId: "acme", protocol: "memory", width, height }));
  registry.addStatic(
    makeGraphicalTarget({ targetId: "gt-rfb", tenantId: "acme", protocol: "rfb", endpoint: "1.2.3.4:5900" }),
  );
  // litevirt is the corpus's "a protocol this system does not speak" case. It
  // used to be rfb, until the reference grew an RFB client; litevirt is Go's
  // console protocol, which neither the reference nor this port implements.
  registry.addStatic(
    makeGraphicalTarget({ targetId: "gt-litevirt", tenantId: "acme", protocol: "litevirt", endpoint: "1.2.3.4:5900" }),
  );
  registry.addStatic(
    makeGraphicalTarget({ targetId: "gt-other", tenantId: "other", protocol: "memory", width: 2, height: 2 }),
  );
  return registry;
}

/** A worker registry as much as these handlers use one. */
function workerRegistry(): {
  get(id: string): GuiWorkerState | undefined;
  put(id: string, state: GuiWorkerState): void;
} {
  const workers = new Map<string, GuiWorkerState>();
  return {
    get: (id) => workers.get(id),
    put: (id, state) => {
      workers.set(id, state);
    },
  };
}

/** Everything a case needs, wired the way the corpus was recorded. */
function depsFor(record: Case): { deps: GuiDeps; console: RecordingSession | null } {
  const registry = workerRegistry();
  let console: RecordingSession | null = null;
  if (record.attached === true || record.known_worker === true) {
    const state: GuiWorkerState = { graphicalSession: null };
    if (record.attached === true) {
      const [width, height] = golden.console;
      console = new RecordingSession(width, height);
      for (const [x, y] of record.drawn ?? []) {
        console.injectPointer(x, y, 1);
      }
      console.calls.length = 0;
      state.graphicalSession = console;
    }
    registry.put(WORKER, state);
  }

  const lease =
    record.has_lease === false
      ? null
      : { leaseExpiresAt: 1000, acquiredBy: record.acquired_by === undefined ? null : record.acquired_by };

  return {
    deps: {
      hub: { registry, getRestSession: async () => lease },
      authz: { hasCapability: async () => record.allow !== false },
      targets: seededTargets(),
      clock: () => ({ wall: golden.mono_to_wall.wall_now, monotonic: golden.mono_to_wall.mono_now }),
    },
    console,
  };
}

/** The request the corpus recorded, as this port takes one. */
function requestFor(record: Case): { principal: { tenantId: unknown; subjectId: unknown }; body: unknown } {
  return {
    principal: {
      tenantId: record.tenant === undefined ? "acme" : record.tenant,
      subjectId: record.subject ?? "u1",
    },
    // An unreadable body and an absent one are the same thing to these
    // handlers, so both arrive as nothing.
    body: record.bad_json === true ? undefined : record.body,
  };
}

describe("what a body may say", () => {
  it.each(golden.int_fields)("an integer field holding $raw", (record) => {
    expect(intField({ k: record.raw }, "k")).toBe(record.value);
  });

  it.each(golden.str_fields)("a text field holding $raw", (record) => {
    expect(strField({ k: record.raw }, "k")).toBe(record.value);
  });

  it("falls back where the field is absent or unusable", () => {
    expect(intField({}, "k")).toBe(golden.int_field_absent);
    expect(intField({}, "k", 5)).toBe(golden.int_field_default);
    expect(intField({ k: "no" }, "k", 5)).toBe(golden.int_field_default_used);
    expect(strField({}, "k")).toBe(golden.str_field_absent);
    expect(strField({}, "k", "d")).toBe(golden.str_field_default);
    expect(strField({ k: 7 }, "k", "d")).toBe(golden.str_field_default_used);
  });

  it("takes a whole number written as text, and nothing else", () => {
    // A client that sent "12" meant twelve; one that sent 12.5 or `true` meant
    // something this field cannot hold, and guessing would put the pointer
    // somewhere nobody asked for.
    expect(intField({ k: " 12 " }, "k")).toBe(12);
    expect(intField({ k: "-12" }, "k")).toBe(-12);
    for (const raw of [12.5, "12.5", true, false, null, [1], { a: 1 }, "twelve", ""]) {
      expect(intField({ k: raw }, "k", 99)).toBe(99);
    }
  });

  it("takes a zero and a number with one in it", () => {
    // "0" and "10" are coordinates a client sends constantly; a reader that
    // took only the digits 1-9 would silently drop both to the default.
    expect(intField({ k: "0" }, "k", 5)).toBe(0);
    expect(intField({ k: "10" }, "k", 5)).toBe(10);
    expect(intField({ k: "100" }, "k", 5)).toBe(100);
    expect(intField({ k: "-10" }, "k", 5)).toBe(-10);
    expect(intField({ k: 0 }, "k", 5)).toBe(0);
  });

  it("takes a boolean as no text at all", () => {
    // Python's `bool` is an `int`, so the reference tests for it first; here
    // it simply is not a string.
    expect(strField({ k: true }, "k", "d")).toBe("d");
  });
});

describe("the tables the input routes read", () => {
  it("names the capability attaching needs", () => {
    expect(CAP_GRAPHICAL_ATTACH).toBe(golden.capability);
  });

  it("knows the keysyms the reference knows", () => {
    expect(GUI_KEY_SYMS).toEqual(golden.key_syms);
  });

  it("knows the buttons the reference knows", () => {
    expect(GUI_BUTTON_MASKS).toEqual(golden.button_masks);
  });
});

describe("turning a lease expiry into a time a client can read", () => {
  it.each(golden.mono_to_wall.samples)("a lease ending at $mono", (record) => {
    expect(
      monoToWall(record.mono, () => ({
        wall: golden.mono_to_wall.wall_now,
        monotonic: golden.mono_to_wall.mono_now,
      })),
    ).toBe(record.wall);
  });

  it("uses the clock it is given, so the same instant reads the same", () => {
    const clock = () => ({ wall: 1000, monotonic: 10 });
    expect(monoToWall(10, clock)).toBe(1000);
    expect(monoToWall(70, clock)).toBe(1060);
    expect(monoToWall(0, clock)).toBe(990);
  });
});

/** Run one recorded case against the handler its path names. */
async function runCase(record: Case, deps: GuiDeps) {
  const request = requestFor(record);
  switch (record.path) {
    case golden.paths.attach:
      return guiAttach(deps, request, WORKER);
    case golden.paths.screenshot:
      return guiScreenshot(deps, WORKER, HIJACK);
    case golden.paths.click:
      return guiClick(deps, request, WORKER, HIJACK);
    case golden.paths.type:
      return guiType(deps, request, WORKER, HIJACK);
    case golden.paths.key:
      return guiKey(deps, request, WORKER, HIJACK);
    default:
      return guiDrag(deps, request, WORKER, HIJACK);
  }
}

describe("the gui routes", () => {
  it.each(golden.cases)("$name", async (record) => {
    const { deps, console } = depsFor(record as unknown as Case);
    const answer = await runCase(record as unknown as Case, deps);

    expect(answer.status).toBe(record.status);

    const expected = record.response as Record<string, unknown>;
    const actual = { ...(answer.body as Record<string, unknown>) };
    if ("lease_expires_at" in actual) {
      // Read off a clock rather than stored, so the corpus says only that
      // there was one; the conversion itself is pinned above.
      expect(typeof actual.lease_expires_at).toBe("number");
      actual.lease_expires_at = "<a time>";
    }
    expect(actual).toEqual(expected);
    expect(Object.keys(actual)).toEqual(Object.keys(expected));

    if (console !== null) {
      expect(console.calls).toEqual(record.calls);
      const shot = console.screenshot();
      expect(Buffer.from(shot.pixels).toString("base64")).toBe(record.console);
    }
  });

  it("refuses a caller without the capability before it reads anything", async () => {
    // Otherwise the refusal itself says whether a target exists.
    const { deps } = depsFor({ allow: false } as Case);
    const answer = await guiAttach(deps, { principal: { tenantId: "acme", subjectId: "u1" } }, WORKER);
    expect(answer.status).toBe(403);
    expect(deps.hub.registry.get(WORKER)).toBeUndefined();
  });

  it("says a target it may not see is not there, not that it is forbidden", async () => {
    // A tenant that can tell the two apart can enumerate another tenant's
    // consoles by name.
    const { deps } = depsFor({} as Case);
    const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: { target_id: "gt-other" } };
    const answer = await guiAttach(deps, request, WORKER);
    expect(answer.status).toBe(404);
    const absent = await guiAttach(
      deps,
      { principal: { tenantId: "acme", subjectId: "u1" }, body: { target_id: "no-such" } },
      WORKER,
    );
    expect(answer.body).toEqual(absent.body);
  });

  it("attaches a console the size the target says", async () => {
    const { deps } = depsFor({} as Case);
    const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: { target_id: "gt-mem" } };
    expect((await guiAttach(deps, request, WORKER)).status).toBe(200);
    const attached = deps.hub.registry.get(WORKER)?.graphicalSession as GraphicalSession;
    const shot = attached.screenshot();
    expect([shot.width, shot.height]).toEqual(golden.console);
  });

  it("never attaches a console with no pixels", async () => {
    // A target declaring a zero side cannot be created — validation refuses
    // one — so this only ever arrives from a store somebody wrote behind the
    // registry's back. It is still a size the framebuffer would refuse.
    const flat = makeGraphicalTarget({ targetId: "gt-flat", tenantId: "acme", protocol: "memory" });
    flat.width = 0;
    flat.height = -5;
    const { deps } = depsFor({} as Case);
    deps.targets = { get: () => flat };
    const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: { target_id: "gt-flat" } };
    expect((await guiAttach(deps, request, WORKER)).status).toBe(200);
    const shot = (
      must(deps.hub.registry.get(WORKER), "the worker state").graphicalSession as GraphicalSession
    ).screenshot();
    expect([shot.width, shot.height]).toEqual([1, 1]);
  });

  it("reads a protocol however a store spelled it", async () => {
    // The registry normalises on the way in, so this only ever arrives from a
    // store written behind its back — and refusing to attach because somebody
    // wrote "MEMORY" would be a console nobody can reach.
    for (const protocol of ["MEMORY", "  memory  ", "  MeMoRy "]) {
      const stored = makeGraphicalTarget({ targetId: "gt-odd", tenantId: "acme", protocol });
      const { deps } = depsFor({} as Case);
      deps.targets = { get: () => stored };
      const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: { target_id: "gt-odd" } };
      expect((await guiAttach(deps, request, WORKER)).status).toBe(200);
    }
  });

  it("names a protocol it cannot speak as the store spelled it, in lower case", async () => {
    // litevirt, not rfb: this port speaks rfb now. litevirt is Go's console
    // protocol and the one no other implementation wires, which is what makes
    // it the stable subject for this case.
    const stored = makeGraphicalTarget({ targetId: "gt-odd", tenantId: "acme", protocol: " LITEVIRT " });
    const { deps } = depsFor({} as Case);
    deps.targets = { get: () => stored };
    const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: { target_id: "gt-odd" } };
    const answer = await guiAttach(deps, request, WORKER);
    expect(answer.status).toBe(501);
    expect(answer.body).toEqual({ error: "graphical protocol not supported: litevirt" });
  });

  it("keeps a worker's other state when it attaches a console", async () => {
    const { deps } = depsFor({ known_worker: true } as Case);
    const before = deps.hub.registry.get(WORKER) as GuiWorkerState;
    const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: { target_id: "gt-mem" } };
    await guiAttach(deps, request, WORKER);
    expect(deps.hub.registry.get(WORKER)).toBe(before);
  });

  it("binds injection to whoever took the lease", async () => {
    // Holding an unguessable string is not the same as being the one who
    // asked for it.
    const { deps } = depsFor({ attached: true, acquired_by: "ada" } as Case);
    const mine = { principal: { tenantId: "acme", subjectId: "ada" }, body: { x: 1, y: 1 } };
    const theirs = { principal: { tenantId: "acme", subjectId: "mallory" }, body: { x: 1, y: 1 } };
    expect((await guiClick(deps, mine, WORKER, HIJACK)).status).toBe(200);
    expect((await guiClick(deps, theirs, WORKER, HIJACK)).status).toBe(403);
  });

  it("lets anybody holding an unclaimed lease through", async () => {
    // Unchanged behaviour for a lease taken before principals were recorded:
    // possession of the hijack id is the whole capability model there.
    const { deps } = depsFor({ attached: true, acquired_by: null } as Case);
    const request = { principal: { tenantId: "acme", subjectId: "anybody" }, body: { x: 1, y: 1 } };
    expect((await guiClick(deps, request, WORKER, HIJACK)).status).toBe(200);
  });

  it("checks the lease before it checks the console", async () => {
    // So an expired lease reads the same whether or not a console was ever
    // attached.
    const { deps } = depsFor({ has_lease: false } as Case);
    const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: {} };
    for (const answer of [
      await guiClick(deps, request, WORKER, HIJACK),
      await guiType(deps, request, WORKER, HIJACK),
      await guiKey(deps, request, WORKER, HIJACK),
      await guiDrag(deps, request, WORKER, HIJACK),
      await guiScreenshot(deps, WORKER, HIJACK),
    ]) {
      expect(answer.status).toBe(404);
      expect(answer.body).toEqual({ error: "Invalid or expired hijack session." });
    }
  });

  it("presses and releases, rather than leaving a button held", async () => {
    // A button left down drags everything the pointer touches afterwards.
    const { deps, console } = depsFor({ attached: true } as Case);
    const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: { x: 1, y: 2 } };
    await guiClick(deps, request, WORKER, HIJACK);
    expect(console?.calls).toEqual([
      ["pointer", 1, 2, 1],
      ["pointer", 1, 2, 0],
    ]);
  });

  it("types a character at a time, down then up", async () => {
    const { deps, console } = depsFor({ attached: true } as Case);
    const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: { text: "ab" } };
    await guiType(deps, request, WORKER, HIJACK);
    expect(console?.calls).toEqual([
      ["key", 97, true],
      ["key", 97, false],
      ["key", 98, true],
      ["key", 98, false],
    ]);
  });

  it("counts a character past the basic plane once", async () => {
    // Split into two, a single emoji would arrive as two keys that are not
    // the one that was typed.
    const { deps, console } = depsFor({ attached: true } as Case);
    const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: { text: "\u{1f600}" } };
    await guiType(deps, request, WORKER, HIJACK);
    expect(console?.calls).toEqual([
      ["key", 128512, true],
      ["key", 128512, false],
    ]);
  });

  it("sends a key nobody named as nothing", async () => {
    const { deps, console } = depsFor({ attached: true } as Case);
    const request = { principal: { tenantId: "acme", subjectId: "u1" }, body: { key_name: "F13" } };
    await guiKey(deps, request, WORKER, HIJACK);
    expect(console?.calls).toEqual([
      ["key", 0, true],
      ["key", 0, false],
    ]);
  });

  it("drags with the button held between the two points", async () => {
    const { deps, console } = depsFor({ attached: true } as Case);
    const request = {
      principal: { tenantId: "acme", subjectId: "u1" },
      body: { start_x: 1, start_y: 1, end_x: 3, end_y: 2 },
    };
    await guiDrag(deps, request, WORKER, HIJACK);
    expect(console?.calls).toEqual([
      ["pointer", 1, 1, 1],
      ["pointer", 3, 2, 1],
      ["pointer", 3, 2, 0],
    ]);
  });

  it("reads the clock itself when it is given none", async () => {
    // The injected one is for the corpus; a running server has only its own.
    const { deps } = depsFor({ attached: true } as Case);
    delete deps.clock;
    const before = Date.now() / 1000;
    const answer = await guiScreenshot(deps, WORKER, HIJACK);
    const expiry = (answer.body as Record<string, number>).lease_expires_at;
    // The lease in this fixture ends 1000 seconds onto a forward-only clock
    // that started long before now, so the instant is in the past.
    expect(Number.isFinite(expiry)).toBe(true);
    expect(expiry).toBeLessThanOrEqual(before + 1000);
  });

  it("takes a caller with no principal as nobody, not as the owner", async () => {
    // A request that lost its principal must not inherit somebody's lease.
    const { deps } = depsFor({ attached: true, acquired_by: "ada" } as Case);
    for (const request of [
      { principal: null, body: { x: 1, y: 1 } },
      { principal: {}, body: { x: 1, y: 1 } },
      { principal: { subjectId: null }, body: { x: 1, y: 1 } },
    ]) {
      const answer = await guiClick(deps, request, WORKER, HIJACK);
      expect(answer.status).toBe(403);
    }
  });

  it("takes a caller with no tenant as no scope at all", async () => {
    const { deps } = depsFor({} as Case);
    for (const principal of [null, {}, { tenantId: null }, { tenantId: 7 }]) {
      const answer = await guiAttach(deps, { principal, body: { target_id: "gt-mem" } }, WORKER);
      expect(answer.status).toBe(403);
      expect(answer.body).toEqual({ error: "graphical target access denied" });
    }
  });

  it("sends a screenshot a decoder can open", async () => {
    const { deps } = depsFor({ attached: true } as Case);
    const answer = await guiScreenshot(deps, WORKER, HIJACK);
    const body = answer.body as Record<string, unknown>;
    const png = Buffer.from(body.screenshot as string, "base64");
    expect([...png.subarray(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    expect(body.worker_id).toBe(WORKER);
    expect(body.hijack_id).toBe(HIJACK);
  });
});

describe("attaching an rfb console", () => {
  /** A duplex replaying a handshake, so no socket is involved. */
  function scriptedStream(script: Buffer): {
    on(event: string, listener: (...args: never[]) => void): unknown;
    write(chunk: Uint8Array): unknown;
    destroy(): unknown;
    destroyed: boolean;
  } {
    let onData: ((chunk: Buffer) => void) | null = null;
    let pending = script;
    return {
      destroyed: false,
      on(event: string, listener: (...args: never[]) => void) {
        if (event === "data") {
          onData = listener as unknown as (chunk: Buffer) => void;
          const script_ = pending;
          pending = Buffer.alloc(0);
          if (script_.length > 0) {
            onData(script_);
          }
        }
        return this;
      },
      write() {
        return true;
      },
      destroy() {
        this.destroyed = true;
        return undefined;
      },
    };
  }

  function handshake(width: number, height: number): Buffer {
    const serverInit = Buffer.alloc(24);
    serverInit.writeUInt16BE(width, 0);
    serverInit.writeUInt16BE(height, 2);
    return Buffer.concat([
      Buffer.from("RFB 003.008\n", "ascii"),
      Buffer.from([1, 1]),
      Buffer.from([0, 0, 0, 0]),
      serverInit,
    ]);
  }

  function rfbDeps(endpoint: string): { deps: GuiDeps; request: GuiRequest } {
    const stored = makeGraphicalTarget({ targetId: "gt-rfb", tenantId: "acme", protocol: "rfb", endpoint });
    const { deps } = depsFor({} as Case);
    deps.targets = { get: () => stored };
    return { deps, request: { principal: { tenantId: "acme", subjectId: "u1" }, body: { target_id: "gt-rfb" } } };
  }

  it("attaches a console that answers the handshake", async () => {
    const { deps, request } = rfbDeps("127.0.0.1:5900");
    deps.dialRfb = () => scriptedStream(handshake(8, 4)) as never;
    const answer = await guiAttach(deps, request, WORKER);
    expect(answer.status ?? 200).toBe(200);
  });

  it("answers 502 when the console is not listening", async () => {
    const { deps, request } = rfbDeps("127.0.0.1:5900");
    deps.dialRfb = () => scriptedStream(Buffer.from("HTTP/1.1 200", "ascii")) as never;
    const answer = await guiAttach(deps, request, WORKER);
    expect(answer.status).toBe(502);
    expect(String((answer.body as { error: string }).error)).toContain("rfb connect failed:");
  });

  it("refuses an endpoint the registry cannot parse", async () => {
    const { deps, request } = rfbDeps("");
    const answer = await guiAttach(deps, request, WORKER);
    expect(answer.status).toBe(403);
    expect(String((answer.body as { error: string }).error)).toContain("invalid endpoint:");
  });

  it("refuses a cloud-metadata console before dialling it", async () => {
    // The guard runs first, so a tenant naming 169.254.169.254 never reaches a
    // dial. blockPrivate stays off; metadata is refused either way.
    const { deps, request } = rfbDeps("169.254.169.254:5900");
    let dialled = false;
    deps.dialRfb = () => {
      dialled = true;
      return scriptedStream(handshake(8, 4)) as never;
    };
    const answer = await guiAttach(deps, request, WORKER);
    expect(answer.status).toBe(403);
    expect(dialled).toBe(false);
  });

  it("dials for real when the deployment injects nothing", async () => {
    // Exercises the production path: no dialRfb, so it uses node:net. Port 1
    // has no listener, so the answer is the gateway failure, not a hang.
    const { deps, request } = rfbDeps("127.0.0.1:1");
    const answer = await guiAttach(deps, request, WORKER);
    expect(answer.status).toBe(502);
  });

  it("reports a thrown non-Error without assuming it has a message", async () => {
    const { deps, request } = rfbDeps("127.0.0.1:5900");
    deps.dialRfb = () => {
      // Not everything thrown is an Error; the refusal still has to read.
      throw "upstream said no";
    };
    const answer = await guiAttach(deps, request, WORKER);
    expect(answer.status).toBe(502);
    expect((answer.body as { error: string }).error).toBe("rfb connect failed: upstream said no");
  });
});
