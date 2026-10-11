//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import (
	"regexp"
	"strings"
)

// SecretMarker replaces every credential-shaped substring of a match before
// the match is embedded in a description.
const SecretMarker = "[SECRET]"

// SecretMaskPatterns are the masks, in the order they are applied. Port of
// SECRET_MASKS in provide.uterm.annotation._masking: the strings are identical
// (annotation_golden.json pins them). THE RULE: a template that names {match}
// receives MaskSecrets(fullMatch) cut to 80 code points -- masked FIRST, so the
// cut cannot leave the head of a credential behind. Each mask has three groups,
// kept prefix / secret / kept suffix, and every non-overlapping match, left to
// right, becomes prefix + SecretMarker + suffix. The patterns use ASCII-only
// classes, no \s or (?i), and no lookaround, so RE2, CPython, ECMAScript and
// .NET agree on every input. Detection and MatchKey are unaffected.
var SecretMaskPatterns = []string{
	`([Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn][ \t]*:[ \t]*)([^'"\r\n]+)()`,
	`((?:[Bb][Ee][Aa][Rr][Ee][Rr]|[Bb][Aa][Ss][Ii][Cc])[ \t\n\r\f\v]+)([^ \t\n\r\f\v'"]+)()`,
	`((?:[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Pp][Aa][Ss][Ss][Ww][Dd]|[Ss][Ee][Cc][Rr][Ee][Tt]` +
		`|[Tt][Oo][Kk][Ee][Nn]|[Aa][Pp][Ii][_-]?[Kk][Ee][Yy])['"]?[ \t]*[=:][ \t]*['"]?)` +
		`([^ \t\n\r\f\v'"]+)()`,
	`((?:^|[ \t])(?:-[uU][ \t]*|--(?:user|proxy-user|password|http-password|ftp-password|proxy-password)` +
		`(?:[ \t]+|=))['"]?)([^ \t\n\r\f\v'"]+)()`,
	`(://)([^/ \t\n\r\f\v@:'"]*:[^/ \t\n\r\f\v@'"]*)(@)`,
	`()(AKIA[0-9A-Z]{12,})()`,
	`()(gh[psourx]_[A-Za-z0-9_]{8,})()`,
	`()(eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*)()`,
	`(-----BEGIN[ \t]+[A-Z ]*PRIVATE KEY-----)([^-]+)()`,
}

var secretMasks = compileMasks(SecretMaskPatterns)

func compileMasks(patterns []string) []*regexp.Regexp {
	out := make([]*regexp.Regexp, len(patterns))
	for i, p := range patterns {
		out[i] = mustCompile(p)
	}
	return out
}

// MaskSecrets returns text with every credential-shaped substring replaced by
// SecretMarker.
func MaskSecrets(text string) string {
	for _, mask := range secretMasks {
		text = maskOne(mask, text)
	}
	return text
}

// maskOne replaces group 2 of every non-overlapping match of mask in text.
func maskOne(mask *regexp.Regexp, text string) string {
	locs := mask.FindAllStringSubmatchIndex(text, -1)
	if locs == nil {
		return text
	}
	var b strings.Builder
	last := 0
	for _, loc := range locs {
		// loc[4], loc[5] bound the secret group; the prefix and suffix around
		// it are kept as they are.
		b.WriteString(text[last:loc[4]])
		b.WriteString(SecretMarker)
		last = loc[5]
	}
	b.WriteString(text[last:])
	return b.String()
}
