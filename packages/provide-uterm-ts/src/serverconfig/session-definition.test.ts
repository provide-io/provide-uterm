//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { afterEach, describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  BUILTIN_CONNECTOR_TYPES,
  type ConfigError,
  coerceSection,
  setRegisteredConnectorTypes,
  validateSection,
  validateServerConfig,
} from "./index.ts";

interface Case {
  name: string;
  kwargs: Record<string, unknown>;
  errors?: ConfigError[];
  accepted?: Record<string, unknown>;
}

interface SessionGolden {
  builtin_connector_types: string[];
  cases: Case[];
  registered_cases: Case[];
}

const golden = loadGolden<SessionGolden>("sessiondef_golden.json");

/** The type the corpus registered before recording the registry cases. */
const RECORDED_TYPES = ["recorded-fake", "aaa-recorded"];

/** The one an entry names when it wants the registered connector. */
const RECORDED_TYPE = "recorded-fake";

afterEach(() => {
  setRegisteredConnectorTypes([]);
});

/** One entry, checked and coerced, with `created_at` dropped as the corpus drops it. */
function accept(kwargs: Record<string, unknown>): Record<string, unknown> {
  const values = coerceSection("SessionDefinition", kwargs);
  delete values.created_at;
  return values;
}

describe("what a session entry accepts", () => {
  it.each(golden.cases)("$name", (record) => {
    expect(validateSection("SessionDefinition", record.kwargs)).toEqual(record.errors ?? []);
  });

  it.each(
    golden.cases.filter((record) => record.accepted !== undefined),
  )("$name — as the reference held it", (record) => {
    const coerced = accept(record.kwargs);
    for (const field of ["session_id", "display_name", "connector_type", "connector_config"]) {
      if (coerced[field] !== undefined) {
        expect({ [field]: coerced[field] }).toEqual({ [field]: record.accepted?.[field] });
      }
    }
  });

  it("has the built-in connector types the server has", () => {
    expect([...BUILTIN_CONNECTOR_TYPES].sort()).toEqual(golden.builtin_connector_types);
  });
});

describe("the identifier", () => {
  it("is required, and saying nothing is itself a mistake", () => {
    expect(validateSection("SessionDefinition", {})).toEqual([
      { type: "missing", loc: ["session_id"], msg: "Field required" },
    ]);
  });

  it("is stripped before it is used", () => {
    expect(accept({ session_id: "  shell  " }).session_id).toBe("shell");
  });

  it("refuses one that is only spaces", () => {
    const errors = validateSection("SessionDefinition", { session_id: "   " });
    expect(errors[0]?.msg).toBe("Value error, session_id is required for each [[sessions]] entry");
  });

  it("accepts a name that is not written in English", () => {
    // CPython's `\w` is Unicode-aware; a JavaScript `\w` is ASCII-only, so a
    // port reading the pattern literally would refuse identifiers the
    // reference accepts.
    for (const sessionId of ["café", "端末", "сессия", "shell٣"]) {
      expect(validateSection("SessionDefinition", { session_id: sessionId })).toEqual([]);
    }
  });

  it("refuses a combining accent, which is not a letter", () => {
    // The same word as `café` above, spelled with a combining mark. Both
    // runtimes refuse it, and a port that reached for a looser class would
    // not.
    expect(validateSection("SessionDefinition", { session_id: "café" })).toHaveLength(1);
    expect(validateSection("SessionDefinition", { session_id: "shell🐚" })).toHaveLength(1);
  });

  it("refuses the separators a route would read", () => {
    for (const sessionId of ["my shell", "my.shell", "a/b"]) {
      const errors = validateSection("SessionDefinition", { session_id: sessionId });
      expect(errors[0]?.msg).toBe(`Value error, session_id must match ^[\\w\\-]+$, got: '${sessionId}'`);
    }
  });
});

describe("a name nobody defined", () => {
  it("becomes a connector setting rather than a refusal", () => {
    // The one place in this schema where an unrecognised key is kept. Every
    // other section forbids extras, so a typo there is a startup failure.
    expect(accept({ session_id: "s", host: "h.example" }).connector_config).toEqual({ host: "h.example" });
  });

  it("means a mistyped field silently configures nothing", () => {
    // Recorded rather than corrected: `recordign_enabled` becomes a connector
    // setting nothing reads, and recording stays off.
    const coerced = accept({ session_id: "s", recordign_enabled: true });
    expect(coerced.connector_config).toEqual({ recordign_enabled: true });
    expect(coerced.recording_enabled).toBeUndefined();
  });

  it("wins over a setting already written out", () => {
    expect(accept({ session_id: "s", connector_config: { host: "given" }, host: "loose" }).connector_config).toEqual({
      host: "loose",
    });
  });

  it("joins the settings already written out", () => {
    expect(accept({ session_id: "s", connector_config: { host: "h" }, port: 22 }).connector_config).toEqual({
      host: "h",
      port: 22,
    });
  });
});

describe("the rest of an entry", () => {
  it("displays an entry by its identifier when it names nothing", () => {
    for (const kwargs of [{ session_id: "shell" }, { session_id: "shell", display_name: "" }]) {
      expect(accept(kwargs).display_name).toBe("shell");
    }
  });

  it("keeps a display name of its own", () => {
    expect(accept({ session_id: "shell", display_name: "Provide Shell" }).display_name).toBe("Provide Shell");
  });

  it("falls back to the default connector when the type is empty", () => {
    // So a commented-out line leaves a working entry behind rather than one
    // that refuses to start.
    expect(accept({ session_id: "s", connector_type: "  " }).connector_type).toBe("shell");
  });

  it("names the entry in a complaint about its input mode", () => {
    expect(validateSection("SessionDefinition", { session_id: "s", input_mode: "readonly" })).toEqual([
      { type: "value_error", loc: ["input_mode"], msg: "Value error, invalid input_mode for s: readonly" },
    ]);
  });

  it("says <unknown> when there is no identifier to name it by", () => {
    expect(validateSection("SessionDefinition", { input_mode: "readonly" }).map((error) => error.msg)).toEqual([
      "Field required",
      "Value error, invalid input_mode for <unknown>: readonly",
    ]);
  });

  it("writes a value the way the reference writes it", () => {
    // `str()` for the input mode and `repr()` for the visibility, which is the
    // difference between `readonly` and `'everyone'` in these two messages.
    expect(validateSection("SessionDefinition", { session_id: "s", input_mode: null })[0]?.msg).toBe(
      "Value error, invalid input_mode for s: None",
    );
    expect(validateSection("SessionDefinition", { session_id: "s", visibility: "everyone" })[0]?.msg).toBe(
      "Value error, invalid visibility for s: 'everyone'",
    );
  });

  it("refuses a creation time that is not a time at all", () => {
    // A recorded divergence: the reference parses this with Pydantic's own
    // date-time grammar, and only the shape is checked here.
    expect(validateSection("SessionDefinition", { session_id: "s", created_at: [] })[0]?.type).toBe("datetime_type");
    expect(validateSection("SessionDefinition", { session_id: "s", created_at: "2020-01-01T00:00:00Z" })).toEqual([]);
    expect(validateSection("SessionDefinition", { session_id: "s", created_at: 5 })).toEqual([]);
  });
});

describe("the connector type", () => {
  it.each(golden.registered_cases)("$name, with a type registered", (record) => {
    setRegisteredConnectorTypes(RECORDED_TYPES);
    expect(validateSection("SessionDefinition", record.kwargs)).toEqual(record.errors ?? []);
  });

  it("is not checked at all while nothing is registered", () => {
    // During startup the registry is empty, and refusing every type before the
    // connectors load would leave the server unable to start on its own
    // config.
    expect(validateSection("SessionDefinition", { session_id: "s", connector_type: "carrier-pigeon" })).toEqual([]);
  });

  it("is checked once something is", () => {
    setRegisteredConnectorTypes(RECORDED_TYPES);
    expect(validateSection("SessionDefinition", { session_id: "s", connector_type: "carrier-pigeon" })).toHaveLength(1);
    expect(validateSection("SessionDefinition", { session_id: "s", connector_type: RECORDED_TYPE })).toEqual([]);
  });

  it("offers the types it knows in a fixed order", () => {
    // Sorted rather than in the order they registered, so two servers with the
    // same connectors read the same complaint.
    setRegisteredConnectorTypes(["zzz-late", "aaa-early"]);
    expect(validateSection("SessionDefinition", { session_id: "s", connector_type: "nope" })[0]?.msg).toContain(
      "['aaa-early', 'shell', 'ssh', 'telnet', 'ushell', 'websocket', 'zzz-late']",
    );
  });

  it("keeps the built-ins whatever else is registered", () => {
    setRegisteredConnectorTypes(RECORDED_TYPES);
    for (const type of BUILTIN_CONNECTOR_TYPES) {
      expect(validateSection("SessionDefinition", { session_id: "s", connector_type: type })).toEqual([]);
    }
  });
});

describe("a session definition inside a document", () => {
  it("is checked where it sits", () => {
    expect(validateServerConfig({ sessions: [{ session_id: "ok" }, { session_id: "not ok" }] })[0]?.loc).toEqual([
      "sessions",
      1,
      "session_id",
    ]);
  });

  it("accepts the entries a working config has", () => {
    expect(validateServerConfig({ sessions: [{ session_id: "provide-shell", connector_type: "shell" }] })).toEqual([]);
  });
});
