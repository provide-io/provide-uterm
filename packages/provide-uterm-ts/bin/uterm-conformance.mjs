#!/usr/bin/env node
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// The TypeScript driver for the live cross-language conformance harness
// (`conformance/live/PROTOCOL.md`). Node >= 22.18 strips the types, so this
// runs from source with no build step:
//
//   node bin/uterm-conformance.mjs client --base-url URL --token TOKEN --scenario FILE
//
// Everything it does lives in `src/conformance/`, which is held at 100%
// coverage; this file only hands over the arguments and the exit code.

import { runCli } from "../src/conformance/cli.ts";

process.exitCode = await runCli(process.argv.slice(2));
