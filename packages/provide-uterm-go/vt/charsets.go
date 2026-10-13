//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

// charsetMaps maps charset designator codes to their translation tables,
// mirroring pyte.charsets.MAPS. The tables themselves live in the
// generated charset_tables.go.
var charsetMaps = map[string]*charsetMap{
	"B": &lat1Map,  // Latin-1.
	"0": &vt100Map, // VT100 special graphics.
	"U": &ibmpcMap, // IBM codepage 437.
	"V": &vax42Map, // VAX42.
}

// charsetCodes maps each translation table back to its designator.
var charsetCodes = func() map[*charsetMap]string {
	codes := make(map[*charsetMap]string, len(charsetMaps))
	for code, table := range charsetMaps {
		codes[table] = code
	}
	return codes
}()

// charsetCode is the designator a translation table was selected by. Every
// table a screen holds comes from charsetMaps, so it always has one.
func charsetCode(m *charsetMap) string { return charsetCodes[m] }
