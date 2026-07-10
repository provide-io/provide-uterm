//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package interop holds the live Go↔Python runtime interop proof.
//
// Unlike the conformance package (which compares Python-authored input→output
// vectors against the Go port entirely OFFLINE), this package starts a REAL
// Python `uterm server` subprocess and drives it from the Go client over the
// actual wire — REST plus the inline DLE/STX WebSocket control channel. It is
// the single test that proves the two implementations interoperate at runtime,
// not merely that they agree byte-for-byte on frozen vectors.
//
// The test skips gracefully (never fails) when uv or the Python server
// dependencies are unavailable, mirroring the conformance suite's skip
// pattern. Because it needs a Python toolchain it is NOT part of the Go
// quality-gate; it is wired into its own `make interop-test` target.
package interop
