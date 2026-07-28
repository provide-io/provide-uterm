//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The interactive shell.
 *
 * What it writes to a terminal is here. Its Python evaluation sandbox is
 * deliberately not ported — restricting Python builtins has no counterpart on
 * this runtime, and inventing an evaluation sandbox rather than porting one
 * would be a new security surface wearing a port's clothes.
 */

export * from "./output.ts";
