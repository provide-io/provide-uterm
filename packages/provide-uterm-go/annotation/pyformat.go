//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import "strings"

// pyFormatErrKind distinguishes the CPython str.format failure modes that the
// detector cares about. The Python code catches (KeyError, IndexError) and
// falls back to a non-leaking description; a malformed template (single '{' or
// '}') raises ValueError, which the detector does NOT catch.
type pyFormatErrKind int

const (
	// keyError mirrors CPython raising KeyError for an unknown named field.
	keyError pyFormatErrKind = iota
	// indexError mirrors CPython raising IndexError for a positional field
	// ("{}" or "{0}") when no positional arguments are supplied.
	indexError
	// valueError mirrors CPython raising ValueError for an unbalanced brace.
	valueError
)

// pyFormatError is returned by pyFormat when substitution fails. Kind selects
// whether the detector treats it as a caught (KeyError/IndexError) fallback or
// an uncaught (ValueError) error.
type pyFormatError struct {
	Kind pyFormatErrKind
	Msg  string
}

func (e *pyFormatError) Error() string { return e.Msg }

// caught reports whether CPython's "except (KeyError, IndexError)" would have
// swallowed this error (triggering the safe fallback description).
func (e *pyFormatError) caught() bool {
	return e.Kind == keyError || e.Kind == indexError
}

// pyFormat emulates the subset of Python str.format used by detection-rule
// description templates: named replacement fields ("{match}", "{event_type}"),
// doubled-brace escapes ("{{" -> "{", "}}" -> "}"), and the error modes for
// unknown-named (KeyError), positional (IndexError), and unbalanced-brace
// (ValueError) fields. Conversion ("!r") and format-spec (":>10") suffixes are
// parsed and ignored, matching Python's willingness to accept them.
func pyFormat(tmpl string, kwargs map[string]string) (string, error) {
	var b strings.Builder
	rs := []rune(tmpl)
	n := len(rs)
	for i := 0; i < n; {
		c := rs[i]
		switch c {
		case '{':
			if i+1 < n && rs[i+1] == '{' {
				b.WriteRune('{')
				i += 2
				continue
			}
			j := i + 1
			for j < n && rs[j] != '}' {
				j++
			}
			if j >= n {
				return "", &pyFormatError{Kind: valueError, Msg: "Single '{' encountered in format string"}
			}
			field := string(rs[i+1 : j])
			i = j + 1
			val, err := resolveField(field, kwargs)
			if err != nil {
				return "", err
			}
			b.WriteString(val)
		case '}':
			if i+1 < n && rs[i+1] == '}' {
				b.WriteRune('}')
				i += 2
				continue
			}
			return "", &pyFormatError{Kind: valueError, Msg: "Single '}' encountered in format string"}
		default:
			b.WriteRune(c)
			i++
		}
	}
	return b.String(), nil
}

// resolveField resolves a single replacement field body (the text between the
// braces) against kwargs. It strips the "!conversion" and ":format_spec"
// suffixes and the ".attr"/"[index]" access chain, then looks up the base arg
// name. An empty or all-digit arg name is positional (IndexError, as no
// positional args are supplied); any other name missing from kwargs is a
// KeyError.
func resolveField(field string, kwargs map[string]string) (string, error) {
	name := field
	if idx := strings.IndexAny(name, "!:"); idx >= 0 {
		name = name[:idx]
	}
	argName := name
	if idx := strings.IndexAny(argName, ".["); idx >= 0 {
		argName = argName[:idx]
	}
	if argName == "" || isAllDigits(argName) {
		return "", &pyFormatError{Kind: indexError, Msg: "Replacement index out of range for positional args tuple"}
	}
	val, ok := kwargs[argName]
	if !ok {
		return "", &pyFormatError{Kind: keyError, Msg: "'" + argName + "'"}
	}
	return val, nil
}

// isAllDigits reports whether s is non-empty and consists solely of ASCII
// digits (the CPython test for a positional field index).
func isAllDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}
