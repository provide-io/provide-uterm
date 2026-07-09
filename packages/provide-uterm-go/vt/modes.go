//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

// Terminal mode switches, mirroring pyte.modes. Private (DEC) modes are
// shifted left by 5 to distinguish them from the ANSI modes they would
// otherwise collide with, exactly like pyte stores them.
const (
	// LNM is Line Feed/New Line Mode: when set, LF/VT/FF also perform a
	// carriage return.
	LNM = 20

	// IRM is Insert/Replace Mode: when set, drawn characters push existing
	// characters to the right instead of overwriting them.
	IRM = 4

	// DECTCEM is Text Cursor Enable Mode: cursor visibility.
	DECTCEM = 25 << 5

	// DECSCNM is Screen Mode: screen-wide reverse video.
	DECSCNM = 5 << 5

	// DECOM is Origin Mode: cursor addressing relative to the scroll region.
	DECOM = 6 << 5

	// DECAWM is Auto Wrap Mode: wrap the cursor at the right margin.
	DECAWM = 7 << 5

	// DECCOLM is Column Mode: 80/132 columns per line.
	DECCOLM = 3 << 5
)
