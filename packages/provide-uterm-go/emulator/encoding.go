//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package emulator

// Encoding is the codec an emulator decodes the bytes it is fed with: the
// receive_encoding of the Python TerminalEmulator. The zero value is CP437,
// which is the reference's default.
type Encoding int

const (
	// CP437 maps every byte to a character: what a BBS draws its interface
	// in, and what the telnet, SSH and websocket connectors decode, as in the
	// reference.
	CP437 Encoding = iota
	// UTF8 is decoded across reads, so a character split between two arrives
	// whole, and invalid bytes become one U+FFFD per maximal subpart. A UTF-8
	// session also honours DEC special graphics, as the reference's does. It
	// is what a local program writes: the pty_capture and shell connectors.
	UTF8
)
