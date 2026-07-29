//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// referencePattern matches a step field that is entirely a reference to what an
// earlier step recorded: ${<step id>.<dotted path>}.
//
// The grammar is deliberately the smallest thing that works — one step id, one
// dotted path, no expressions, no defaults, no nesting — and the anchors are
// the whole rule that "a${x.y}b" is not a reference and is sent as written.
//
// The pattern is a raw string literal on purpose: written as an interpreted
// literal its backslashes would have to be doubled, and doubling them once too
// often is how a resolver comes to match a literal backslash and silently
// resolve nothing. TestReferenceResolvesAnEarlierAnswer is the guard: it
// asserts a reference produced a value, not merely that the code path ran.
var referencePattern = regexp.MustCompile(`^\$\{([a-z0-9_]+)\.([A-Za-z0-9_.]+)\}$`)

// resolveStep replaces every reference in a step with what the step it names
// recorded. It runs immediately before the request is built, which is the only
// moment the value exists: the harness cannot do this, because the driver
// performs the request and so holds the answer before anyone else could see it.
//
// An unresolvable reference is returned as an error and is a run error, never a
// step observation — it is a malformed scenario, and recording it as a field
// would let the harness compare it as though the server had done something.
func resolveStep(step *Step, seen map[string]StepFields) error {
	for _, field := range step.referenceable() {
		text, err := resolveText(*field, seen)
		if err != nil {
			return err
		}
		*field = text
	}
	return resolveBody(step, seen)
}

// referenceable is every string field a scenario may write a reference into.
// The step's own id and action are not among them: the scenario schema pins
// both to a shape no reference can take, and a step that renamed itself from an
// earlier answer could not be matched to its expectations.
func (s *Step) referenceable() []*string {
	return []*string{
		&s.Auth,
		&s.Path,
		&s.SessionID,
		&s.WorkerID,
		&s.HijackID,
		&s.Owner,
		&s.Keys,
		&s.InputMode,
	}
}

// resolveText resolves one string field, returning it unchanged when it is not
// a reference.
func resolveText(text string, seen map[string]StepFields) (string, error) {
	match := referencePattern.FindStringSubmatch(text)
	if match == nil {
		return text, nil
	}
	value, err := lookupReference(text, match[1], match[2], seen)
	if err != nil {
		return "", err
	}
	return asText(value), nil
}

// resolveBody resolves an http_post body that is written as a reference. The
// value is substituted as JSON rather than as text, so a step can post back an
// object an earlier step was given.
func resolveBody(step *Step, seen map[string]StepFields) error {
	var text string
	if len(step.Body) == 0 || json.Unmarshal(step.Body, &text) != nil {
		return nil
	}
	match := referencePattern.FindStringSubmatch(text)
	if match == nil {
		return nil
	}
	value, err := lookupReference(text, match[1], match[2], seen)
	if err != nil {
		return err
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		// Only reachable if a recorded body held something json.Marshal
		// refuses, which nothing decoded from a response can.
		return fmt.Errorf("reference %s cannot be sent as a body: %w", text, err)
	}
	step.Body = encoded
	return nil
}

// lookupReference reads one reference out of what has been recorded so far.
func lookupReference(ref, stepID, path string, seen map[string]StepFields) (any, error) {
	fields, ok := seen[stepID]
	if !ok {
		return nil, fmt.Errorf("reference %s names step %q, which has not run", ref, stepID)
	}
	segments := strings.Split(path, ".")
	node, ok := fields.lookup(segments[0])
	if !ok {
		return nil, fmt.Errorf("reference %s is not there", ref)
	}
	value, ok := dig(node, segments[1:])
	if !ok {
		return nil, fmt.Errorf("reference %s is not there", ref)
	}
	return value, nil
}

// lookup reads one of the four field names a step records. A field that is
// recorded as null is found and is null: absent and null are different answers,
// and only the first is a malformed reference.
func (f StepFields) lookup(name string) (any, bool) {
	switch name {
	case "status":
		if f.Status == nil {
			return nil, true
		}
		return *f.Status, true
	case "ok":
		return f.OK, true
	case "body":
		return f.Body, true
	case "error":
		if f.Error == nil {
			return nil, true
		}
		return *f.Error, true
	default:
		return nil, false
	}
}

// dig walks a dotted path through a recorded value: objects by key, arrays by
// numeric index. Anything else — a path into a scalar, an index past the end —
// is not there.
func dig(node any, segments []string) (any, bool) {
	for _, segment := range segments {
		switch held := node.(type) {
		case map[string]any:
			value, ok := held[segment]
			if !ok {
				return nil, false
			}
			node = value
		case []any:
			index, err := strconv.Atoi(segment)
			if err != nil || index < 0 || index >= len(held) {
				return nil, false
			}
			node = held[index]
		default:
			return nil, false
		}
	}
	return node, true
}

// asText renders a resolved value for a field that was written as a string. A
// string is itself; anything else is its JSON form, so an id that arrived as a
// number still reaches the wire as the digits the server sent.
func asText(value any) string {
	if text, ok := value.(string); ok {
		return text
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		// Unreachable: every recorded value came out of a JSON response.
		return fmt.Sprint(value)
	}
	return string(encoded)
}
