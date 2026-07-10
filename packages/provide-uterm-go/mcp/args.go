//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"encoding/json"
	"math"
	"strconv"

	mcpgo "github.com/mark3labs/mcp-go/mcp"
)

// Optional-argument helpers. Python tool parameters default to None and are
// only forwarded when supplied; these helpers reproduce the "present vs unset"
// distinction the mcp-go typed getters (which collapse absent to a default)
// cannot express on their own.

// optString returns a pointer to a string argument when it is present and a
// string, else nil (the Python "param is None" case).
func optString(req mcpgo.CallToolRequest, key string) *string {
	if v, ok := req.GetArguments()[key]; ok {
		if s, isStr := v.(string); isStr {
			return &s
		}
	}
	return nil
}

// deref returns the pointed-to string, or "" when p is nil.
func deref(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// optInt returns a pointer to an int argument when it is present and numeric
// (JSON number, int, or numeric string), else nil.
func optInt(req mcpgo.CallToolRequest, key string) *int {
	v, ok := req.GetArguments()[key]
	if !ok {
		return nil
	}
	switch n := v.(type) {
	case int:
		return &n
	case int64:
		i := int(n)
		return &i
	case float64:
		i := int(n)
		return &i
	case json.Number:
		if f, err := n.Float64(); err == nil {
			i := int(math.Trunc(f))
			return &i
		}
	case string:
		if i, err := strconv.Atoi(n); err == nil {
			return &i
		}
	}
	return nil
}
