//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The TypeScript port of provide-uterm.
 *
 * Each subpackage is exported under its own name, so `provide-uterm-ts`
 * resolves as well as `provide-uterm-ts/hub` does. Namespaces rather than a
 * flat re-export, because several subpackages name the same concept — a
 * `Screen`, a `Cursor`, an `InputMode` — and flattening them would either
 * collide or silently pick one.
 *
 * The `testing` helpers are deliberately absent: they are for this package's
 * own tests, not part of its surface.
 */

export * as ansi from "./ansi/index.ts";
export * as auth from "./auth/index.ts";
export * as bridge from "./bridge/index.ts";
export * as channels from "./channels/index.ts";
export * as colors from "./colors/index.ts";
export * as controlChannel from "./control-channel/index.ts";
export * as ctrlmsg from "./ctrlmsg/index.ts";
export * as defaults from "./defaults/index.ts";
export * as detection from "./detection/index.ts";
export * as emulator from "./emulator/index.ts";
export * as fanout from "./fanout/index.ts";
export * as fileIo from "./file-io/index.ts";
export * as filters from "./filters/index.ts";
export * as frames from "./frames/index.ts";
export * as gateway from "./gateway/index.ts";
export * as hub from "./hub/index.ts";
export * as lineEditor from "./line-editor/index.ts";
export * as pycompat from "./pycompat/index.ts";
export * as ratelimit from "./ratelimit/index.ts";
export * as recording from "./recording/index.ts";
export * as redaction from "./redaction/index.ts";
export * as render from "./render/index.ts";
export * as sanitizer from "./sanitizer/index.ts";
export * as screen from "./screen/index.ts";
export * as session from "./session/index.ts";
export * as sessionLogger from "./session-logger/index.ts";
export * as telemetry from "./telemetry/index.ts";
export * as transports from "./transports/index.ts";
export * as vt from "./vt/index.ts";
