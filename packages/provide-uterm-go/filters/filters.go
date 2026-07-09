//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package filters provides character-level input filters for BBS/telnet
// terminal sessions. Port of provide.uterm.filters.
//
// These helpers consume and discard protocol-level byte sequences (telnet IAC
// commands, ANSI escape sequences) from a byte-at-a-time reader. They are
// intended for interactive sessions where arrow keys, function keys, and
// telnet negotiation bytes must be silently discarded rather than leaking
// into command input:
//
//	b, err := reader.ReadByte()
//	if b == filters.IAC { _ = filters.ConsumeIAC(reader); continue }
//	if b == filters.ESC { _ = filters.ConsumeEscape(reader); continue }
package filters

import (
	"errors"
	"io"
)

// Telnet IAC constants (RFC 854).
const (
	IAC  byte = 255
	WILL byte = 251
	WONT byte = 252
	DO   byte = 253
	DONT byte = 254
	SB   byte = 250
	SE   byte = 240

	// ESC is the ANSI escape byte.
	ESC byte = 0x1B
)

// readByte returns (b, false, nil) normally, (0, true, nil) at EOF (mirroring
// Python's empty-read early return), and a non-nil error otherwise.
func readByte(r io.ByteReader) (byte, bool, error) {
	b, err := r.ReadByte()
	if errors.Is(err, io.EOF) {
		return 0, true, nil
	}
	if err != nil {
		return 0, false, err
	}
	return b, false, nil
}

// ConsumeIAC consumes and discards a telnet IAC command sequence. Call it
// after the IAC byte (0xFF) has been read. Handles two-byte commands
// (WILL/WONT/DO/DONT + option byte), sub-negotiation (SB ... IAC SE), and
// escaped IAC IAC.
func ConsumeIAC(r io.ByteReader) error {
	cmd, eof, err := readByte(r)
	if err != nil || eof {
		return err
	}
	switch cmd {
	case WILL, WONT, DO, DONT:
		_, _, err = readByte(r) // option byte
		return err
	case SB:
		for {
			sb, eof, err := readByte(r)
			if err != nil || eof {
				return err
			}
			if sb == IAC {
				se, eof, err := readByte(r)
				if err != nil || eof || se == SE {
					return err
				}
			}
		}
	}
	// IAC IAC or other — already consumed.
	return nil
}

// ConsumeEscape consumes and discards an ANSI escape sequence. Call it after
// the ESC byte (0x1B) has been read. Handles CSI sequences (ESC '[' ...
// final-byte), SS3 sequences (ESC 'O' key), and two-char ESC+letter combos.
func ConsumeEscape(r io.ByteReader) error {
	b, eof, err := readByte(r)
	if err != nil || eof {
		return err
	}
	switch b {
	case 0x5B: // '[' — CSI
		for {
			c, eof, err := readByte(r)
			if err != nil || eof {
				return err
			}
			if c >= 0x40 && c <= 0x7E {
				return nil // final byte
			}
		}
	case 0x4F: // 'O' — SS3
		_, _, err = readByte(r)
		return err
	}
	// Otherwise ESC + single char — already consumed.
	return nil
}
