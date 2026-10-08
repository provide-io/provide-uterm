//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package recording

import (
	"bytes"
	"encoding/json"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/ctrlmsg"
)

// PyJSONSize returns len(json.dumps(v)) as CPython writes it by default:
// ", " between items, ": " after each key, non-ASCII as \uXXXX escapes, floats
// by repr. The reference measures recordings this way, both the byte quota a
// SessionLogger enforces and the size_bytes an in-memory store reports, so a
// budget runs out at the same entry here as there.
//
// v is first put through Go's JSON encoding (numbers kept exact), so any
// value a store can write is measured as the JSON it becomes. Key order does
// not change the length, so the sorted canonical form is measured and the
// default separators' spaces are added: one per item separator, one per key.
// A value JSON cannot carry is an error, as json.dumps raises on one.
func PyJSONSize(v any) (int, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return 0, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var norm any
	// Neither can fail: raw is JSON Go just wrote, and decoding it yields only
	// the types the canonical encoder takes.
	_ = dec.Decode(&norm)
	canonical, _ := ctrlmsg.CanonicalJSON(norm)
	return len(canonical) + separatorSpaces(norm), nil
}

// separatorSpaces counts the spaces CPython's default separators add over the
// compact form.
func separatorSpaces(v any) int {
	switch x := v.(type) {
	case map[string]any:
		n := 0
		if len(x) > 0 {
			n = 2*len(x) - 1
		}
		for _, e := range x {
			n += separatorSpaces(e)
		}
		return n
	case []any:
		n := 0
		if len(x) > 0 {
			n = len(x) - 1
		}
		for _, e := range x {
			n += separatorSpaces(e)
		}
		return n
	default:
		return 0
	}
}
