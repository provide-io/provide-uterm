//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Group creation: who may be swept into a fan-out group, and on whose word.
 *
 * Admission is where the blast radius is decided, so it is held on its own
 * against the recorded reference behaviour; the rest of the surface lives in
 * `routes.test.ts`.
 */

import { describe, expect, it } from "vitest";
import { FakeController, golden, harness, PRINCIPAL, request, withoutId } from "../testing/fanout-routes-harness.ts";
import type { FanOutGroup } from "./index.ts";

describe("creating a group", () => {
  it("fills in every default", async () => {
    const { routes, controller } = harness();
    const response = await routes.createGroup(request(PRINCIPAL, {}));
    expect({ ...response, body: withoutId(response.body) }).toStrictEqual(golden.create_defaults.response);
    const group = [...controller.groups.values()][0] as FanOutGroup;
    expect({
      created_by: group.createdBy,
      divergence_threshold: group.divergenceThreshold,
      error_pattern: group.errorPattern ?? null,
      grants: group.grants,
      max_response_ms: group.maxResponseMs,
      mode: group.mode,
      name: group.name,
      quiesce_ms: group.quiesceMs,
      stop_on_first_error: group.stopOnFirstError,
      worker_ids: group.workerIds,
    }).toStrictEqual(golden.create_defaults.group);
  });

  it("carries every field through when they are given", async () => {
    const { routes, controller } = harness();
    const response = await routes.createGroup(
      request(PRINCIPAL, {
        worker_ids: ["w1", "w2"],
        name: "prod fleet",
        mode: "sequential",
        stop_on_first_error: true,
        error_pattern: "ERROR",
        quiesce_ms: 100,
        max_response_ms: 2000,
        divergence_threshold: 0.5,
      }),
    );
    expect({ ...response, body: withoutId(response.body) }).toStrictEqual(golden.create_full.response);
    const group = [...controller.groups.values()][0] as FanOutGroup;
    expect({
      created_by: group.createdBy,
      divergence_threshold: group.divergenceThreshold,
      error_pattern: group.errorPattern ?? null,
      grants: group.grants,
      max_response_ms: group.maxResponseMs,
      mode: group.mode,
      name: group.name,
      quiesce_ms: group.quiesceMs,
      stop_on_first_error: group.stopOnFirstError,
      worker_ids: group.workerIds,
    }).toStrictEqual(golden.create_full.group);
  });

  it("refuses a controller that cannot judge access at all", async () => {
    // Missing one authorizer, the controller cannot judge access; admitting
    // members on the strength of the checks that remain is exactly the quiet
    // failure this route exists to prevent. The members here are readable, so
    // the unwired controller is the only reason to refuse.
    const { routes, controller, readChecks } = harness({ authorizationReady: false });
    const response = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["w1"] }));
    expect(response).toStrictEqual(golden.create_authorization_unavailable);
    // Refused before any member was looked at, and nothing was created.
    expect(readChecks).toStrictEqual([]);
    expect(controller.groups.size).toBe(0);
  });

  it("refuses a session the caller cannot read", async () => {
    // Otherwise a group is a way to reach sessions the caller was never
    // allowed to see.
    const { routes, controller } = harness();
    const response = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["w1", "secret"] }));
    expect(response).toStrictEqual(golden.create_forbidden);
    expect(controller.groups.size).toBe(0);
  });

  it("names the session it refused", async () => {
    expect(golden.create_forbidden.body.error).toContain("secret");
  });

  it("rejects a session it has never heard of by default", async () => {
    const { routes, controller } = harness();
    const response = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["never-registered"] }));
    expect(response).toStrictEqual({ status: 400, body: { error: "unknown fan-out session: never-registered" } });
    expect(controller.groups.size).toBe(0);
  });

  it("allows dormant members only when explicitly configured", async () => {
    // The gate is the controller's own, not the routes': the corpus records a
    // strict controller refusing an unknown member with a 400 and no group.
    const { routes: strict } = harness();
    const refused = await strict.createGroup(request(PRINCIPAL, { worker_ids: ["never-registered"] }));
    expect(refused.status).toBe(golden.create_unknown_session.status);
    expect((refused.body as Record<string, unknown>).session_count).toBe(
      golden.create_unknown_session.body.session_count,
    );

    const { routes, controller } = harness({ allowUnknownMembers: true });
    const response = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["never-registered"] }));
    expect(response.status).toBe(200);
    expect((response.body as Record<string, unknown>).session_count).toBe(1);
    expect(controller.groups.size).toBe(1);
  });

  it("still refuses a forbidden member in dormant mode", async () => {
    // The opt-in admits unknown members only; a known-but-unreadable session
    // is refused exactly as before.
    const { routes, controller } = harness({ allowUnknownMembers: true });
    const response = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["never-registered", "secret"] }));
    expect(response).toStrictEqual({ status: 403, body: { error: "forbidden: no read access to session secret" } });
    expect(controller.groups.size).toBe(0);
  });

  it("answers about the first member it refuses, not the first kind", async () => {
    // Members are judged in the order the caller gave them, so a forbidden
    // member ahead of an unknown one is what the caller hears about. Sorting
    // the refusals by kind instead would let the order of the request decide
    // whether a session the caller may not read is even mentioned.
    const { routes } = harness();
    const forbiddenFirst = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["secret", "never-registered"] }));
    const unknownFirst = await routes.createGroup(request(PRINCIPAL, { worker_ids: ["never-registered", "secret"] }));
    expect(forbiddenFirst).toStrictEqual(golden.create_forbidden);
    expect(unknownFirst).toStrictEqual({ status: 400, body: { error: "unknown fan-out session: never-registered" } });
  });

  it("decides read access from the routes' own authorizer, on the registry's definition", async () => {
    // One resolution per member, and access checked against that same
    // definition: a controller wired to a wider view than the server's
    // registry cannot widen what a group is allowed to reach.
    const { routes, readChecks } = harness();
    await routes.createGroup(request(PRINCIPAL, { worker_ids: ["secret"] }));
    expect(readChecks).toStrictEqual([{ workerId: "secret" }]);
  });

  it("passes a controller refusal back as a 400", async () => {
    const controller = new FakeController();
    controller.createError = new Error("group too large: 99 > 50");
    const { routes } = harness({ controller });
    expect(await routes.createGroup(request(PRINCIPAL, { worker_ids: ["w1"] }))).toStrictEqual(golden.create_rejected);
  });

  it("lets a failure that is not a refusal through", async () => {
    // A bug in the controller is not a client error, and dressing it as one
    // would send the caller looking at their own request.
    const controller = new FakeController();
    controller.createError = new TypeError("controller is broken");
    const { routes } = harness({ controller });
    await expect(routes.createGroup(request(PRINCIPAL, { worker_ids: ["w1"] }))).rejects.toThrow(TypeError);
  });

  it("gives the group a fresh identifier", async () => {
    const { routes, controller } = harness();
    await routes.createGroup(request(PRINCIPAL, {}));
    await routes.createGroup(request(PRINCIPAL, {}));
    expect(controller.groups.size).toBe(2);
    const [first, second] = [...controller.groups.keys()];
    expect(first).not.toBe(second);
  });

  it("stamps the creator and the time", async () => {
    const { routes, controller } = harness();
    await routes.createGroup(request(PRINCIPAL, {}));
    const group = [...controller.groups.values()][0] as FanOutGroup;
    expect(group.createdBy).toBe(PRINCIPAL);
    expect(group.createdAt).toBe(1000.0);
  });
});
