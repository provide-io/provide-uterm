//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The recording read routes, answered as the reference answers them.
 *
 * Every expected answer is the reference server's own, from
 * `serverrecording_golden.json`: a running reference configured with the same
 * two sessions, serving the same fixture recording with the same
 * modification time, asked every probe below. This server is bootstrapped the
 * same way and asked the same probes.
 */

import { mkdirSync, mkdtempSync, rmSync, symlinkSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { InMemoryRecordingStore } from "../recording/index.ts";
import { encodeJwt } from "../serverauth/index.ts";
import { loadGolden } from "../testing/golden.ts";
import { createServerApp } from "./app.ts";
import { bootstrapServer } from "./bootstrap.ts";
import { SessionHub } from "./session-hub.ts";
import { SessionRegistry } from "./session-registry.ts";
import { sessionDefinitionFrom } from "./session-status.ts";

interface Probe {
  id: string;
  method: string;
  path: string;
  auth: string;
  request_headers: Record<string, string>;
  status: number;
  headers: Record<string, string>;
  body: unknown;
}

interface RecordingGolden {
  volatile: string;
  fixture: string[];
  fixture_mtime_ns: number;
  boundary: string;
  probes: Probe[];
}

const golden = loadGolden<RecordingGolden>("serverrecording_golden.json");

/** The paths of a body that differ between runs: the temporary directory. */
const VOLATILE_PATHS: Readonly<Record<string, readonly string[]>> = { meta: ["path"], meta_viewer: ["path"] };

const SESSIONS = [
  { session_id: "recorded", connector_type: "shell", auto_start: false, recording_enabled: true },
  { session_id: "unrecorded", connector_type: "shell", auto_start: false, recording_enabled: false },
];

let scratch: string;
let recordings: string;

beforeAll(() => {
  scratch = mkdtempSync(join(tmpdir(), "uterm-recording-routes-"));
  recordings = join(scratch, "recordings");
  const fixture = join(recordings, "recorded.jsonl");
  mkdirSync(recordings, { mode: 0o700 });
  writeFileSync(fixture, golden.fixture.map((line) => `${line}\n`).join(""));
  const seconds = golden.fixture_mtime_ns / 1e9;
  utimesSync(fixture, seconds, seconds);
});

afterAll(() => {
  rmSync(scratch, { recursive: true, force: true });
});

/** A server configured as the reference was, with its tokens. */
function server() {
  const bootstrapped = bootstrapServer({
    authMode: "dev_token",
    document: { recording: { directory: recordings }, sessions: SESSIONS },
  });
  const auth = bootstrapped.auth;
  const mint = (roles: string[], scope?: string) => {
    const now = Math.floor(Date.now() / 1000);
    return encodeJwt(
      {
        sub: "golden-probe",
        iss: auth.jwt_issuer,
        aud: auth.jwt_audience,
        iat: now,
        exp: now + 3600,
        roles,
        ...(scope === undefined ? {} : { scope }),
      },
      auth.jwt_public_key_pem as string,
    );
  };
  const tokens: Record<string, string> = {
    token: bootstrapped.token,
    viewer: mint(["viewer"]),
    no_recording: mint(["admin"], "session.read"),
  };
  return { ...bootstrapped, tokens };
}

/** The download's validators, which If-Range probes quote back. */
const whole = golden.probes.find((probe) => probe.id === "download") as Probe;

/** A probe as a request. */
function request(probe: Probe, tokens: Record<string, string>): Request {
  const headers = new Headers();
  for (const [name, value] of Object.entries(probe.request_headers)) {
    headers.set(name, value.startsWith("<") ? (whole.headers[value.slice(1, -1)] as string) : value);
  }
  if (probe.auth !== "none") {
    headers.set("Authorization", `Bearer ${tokens[probe.auth]}`);
  }
  return new Request(`http://127.0.0.1:0${probe.path}`, {
    method: probe.method,
    headers,
    ...(probe.method === "GET" ? {} : { body: "{}" }),
  });
}

/** A copy of `body` with each declared path replaced. */
function mask(body: unknown, paths: readonly string[]): unknown {
  const copy = structuredClone(body) as Record<string, unknown>;
  for (const path of paths) {
    if (copy !== null && typeof copy === "object" && path in copy) {
      copy[path] = golden.volatile;
    }
  }
  return copy;
}

/** Whether a probe's body is a JSON document rather than a file or plain text. */
function isJson(probe: Probe): boolean {
  return probe.headers["content-type"] === "application/json" && !("content-disposition" in probe.headers);
}

/**
 * The headers a probe is compared on.
 *
 * Every kept header, except a JSON body's length: the reference writes an
 * integral float as `1.0` and JavaScript as `1`, so the same entries can be a
 * byte or two apart. A file's length is compared — its bytes are the fixture's.
 */
function comparedNames(probe: Probe): string[] {
  return Object.keys(probe.headers).filter((name) => !(name === "content-length" && isJson(probe)));
}

/** A response's values for those headers, with a multipart boundary replaced. */
function headersOf(response: Response, names: readonly string[]): Record<string, string> {
  const kept: Record<string, string> = {};
  for (const name of names) {
    const value = response.headers.get(name);
    if (value !== null) {
      kept[name] = value.replace(/boundary=[0-9a-f]{26}/, `boundary=${golden.boundary}`);
    }
  }
  return kept;
}

describe("the reference's own answers, probe by probe", () => {
  it("covers every probe the reference recorded", () => {
    expect(golden.probes.length).toBe(69);
  });

  for (const probe of golden.probes) {
    it(`${probe.id}: ${probe.method} ${probe.path.slice(0, 120)} as ${probe.auth}`, async () => {
      const { app, tokens } = server();
      const response = await app.handle(request(probe, tokens));
      expect(response.status).toBe(probe.status);
      const names = comparedNames(probe);
      expect(headersOf(response, names)).toStrictEqual(
        Object.fromEntries(names.map((name) => [name, probe.headers[name]])),
      );
      const text = await response.text();
      const body = isJson(probe) ? mask(JSON.parse(text), VOLATILE_PATHS[probe.id] ?? []) : text;
      const boundary = /boundary=([0-9a-f]{26})/.exec(response.headers.get("content-type") ?? "")?.[1];
      expect(boundary === undefined ? body : (body as string).replaceAll(boundary, golden.boundary)).toStrictEqual(
        probe.body,
      );
    });
  }
});

describe("what the reference's probes cannot reach", () => {
  it("flushes what a running session has buffered before answering", async () => {
    const { app, tokens, runtimes } = server();
    let flushed = 0;
    const original = runtimes.flushRecording.bind(runtimes);
    runtimes.flushRecording = async (sessionId: string) => {
      flushed += 1;
      await original(sessionId);
    };
    for (const path of ["/recording", "/recording/entries"]) {
      await app.handle(request({ ...whole, path: `/api/sessions/recorded${path}`, request_headers: {} }, tokens));
    }
    expect(flushed).toBe(2);
  });

  it("will not serve a recording that resolves outside the recording directory", async () => {
    const outside = mkdtempSync(join(tmpdir(), "uterm-outside-"));
    try {
      writeFileSync(join(outside, "target.jsonl"), "{}\n");
      const elsewhere = mkdtempSync(join(tmpdir(), "uterm-elsewhere-"));
      symlinkSync(join(outside, "target.jsonl"), join(elsewhere, "recorded.jsonl"));
      const bootstrapped = bootstrapServer({
        authMode: "dev_token",
        document: { recording: { directory: elsewhere }, sessions: SESSIONS },
      });
      const response = await bootstrapped.app.handle(
        request({ ...whole, request_headers: {} }, { token: bootstrapped.token }),
      );
      expect(response.status).toBe(404);
      expect(await response.json()).toStrictEqual({ detail: "recording not available" });
      rmSync(elsewhere, { recursive: true, force: true });
    } finally {
      rmSync(outside, { recursive: true, force: true });
    }
  });

  it("will not serve a recording when the recording directory cannot be resolved", async () => {
    // A store that hands back a real file while the configured directory is
    // gone: nothing can be shown to lie inside it, so nothing is served.
    const registry = new SessionRegistry([sessionDefinitionFrom(SESSIONS[0] as Record<string, unknown>, "x")], false);
    const { tokens, auth } = server();
    const store = new InMemoryRecordingStore();
    store.getPath = async () => join(recordings, "recorded.jsonl");
    const app = createServerApp({
      registry,
      auth,
      hub: new SessionHub(),
      connectors: { setMode: async () => {} },
      recordings: { recordingStore: store, recordingDirectory: join(scratch, "gone"), flushRecording: async () => {} },
      version: "0.0.0",
      controlPlaneBackend: "memory",
      startupTime: 1,
    });
    const download = await app.handle(request({ ...whole, request_headers: {} }, tokens));
    expect(download.status).toBe(404);
  });

  it("answers from the no-op store when an app is built without recordings", async () => {
    const registry = new SessionRegistry([sessionDefinitionFrom(SESSIONS[0] as Record<string, unknown>, "x")], false);
    const { tokens, auth } = server();
    const app = createServerApp({
      registry,
      auth,
      hub: new SessionHub(),
      connectors: { setMode: async () => {} },
      version: "0.0.0",
      controlPlaneBackend: "memory",
      startupTime: 1,
    });
    const meta = await app.handle(request({ ...whole, path: "/api/sessions/recorded/recording" }, tokens));
    expect(await meta.json()).toStrictEqual({ session_id: "recorded", exists: false, size_bytes: 0, enabled: true });
    const download = await app.handle(request({ ...whole, request_headers: {} }, tokens));
    expect(download.status).toBe(404);
  });
});
