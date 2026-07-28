//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * VT/ANSI terminal emulation.
 *
 * A fresh implementation of the observable behaviour of pyte, which the
 * Python reference depends on for terminal emulation. pyte is LGPL and none
 * of it is copied: the behaviour and the colour tables were recorded from a
 * running pyte by `testdata/gen_vt_golden.py`, and the corpus asserts them.
 *
 * Counterpart of the Go package `vt`.
 */

export { type Char, defaultChar, FG_BG_256, isDefaultChar } from "./char.ts";
export { type Cursor, Screen } from "./screen.ts";
export { Stream } from "./stream.ts";
