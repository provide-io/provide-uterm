//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ctrlmsg

import (
	"fmt"
	"strings"
	"sync"
)

// LinkPattern is an immutable descriptor for one server-driven clickable text
// pattern, ported from the Python control_channel_patterns.LinkPattern
// dataclass. Construct one with NewLinkPattern so the action is validated and
// the "g" flags default is applied.
type LinkPattern struct {
	// Pattern is the JavaScript regex source string.
	Pattern string
	// Action is what happens on click: "cmd", "url", "key" or "focus".
	Action string
	// ID is the optional stable identifier used by the registry (nil == none).
	ID *string
	// Flags are the regex flags forwarded to new RegExp; defaults to "g".
	Flags string
	// Group is the clickable capture group (0 == whole match).
	Group int
	// Payload is the click payload template.
	Payload string
	// Hover is the hover-tooltip template.
	Hover string
	// Class is the CSS class applied to highlighted ranges; serialised as the
	// wire key "class".
	Class string
}

// LinkPatternOption configures NewLinkPattern.
type LinkPatternOption func(*LinkPattern)

// WithID sets the stable identifier used for registry replace/unregister.
func WithID(id string) LinkPatternOption {
	return func(p *LinkPattern) { p.ID = &id }
}

// WithFlags overrides the regex flags (default "g").
func WithFlags(flags string) LinkPatternOption {
	return func(p *LinkPattern) { p.Flags = flags }
}

// WithGroup sets the clickable capture group.
func WithGroup(group int) LinkPatternOption {
	return func(p *LinkPattern) { p.Group = group }
}

// WithPayload sets the click payload template.
func WithPayload(payload string) LinkPatternOption {
	return func(p *LinkPattern) { p.Payload = payload }
}

// WithHover sets the hover-tooltip template.
func WithHover(hover string) LinkPatternOption {
	return func(p *LinkPattern) { p.Hover = hover }
}

// WithClass sets the CSS class applied to highlighted ranges.
func WithClass(class string) LinkPatternOption {
	return func(p *LinkPattern) { p.Class = class }
}

// NewLinkPattern builds a LinkPattern for the given regex source and action,
// applying the "g" flags default and validating the action. It mirrors the
// Python dataclass __post_init__ validation; an unknown action is an error.
//
// The Python error lists the valid actions as the sorted repr
// ['cmd', 'focus', 'key', 'url']; this Go port emits the same content with Go
// slice formatting: `invalid action "explode"; must be one of [cmd focus key url]`.
func NewLinkPattern(pattern, action string, opts ...LinkPatternOption) (LinkPattern, error) {
	p := LinkPattern{Pattern: pattern, Action: action, Flags: "g"}
	for _, opt := range opts {
		opt(&p)
	}
	if !isValidLinkAction(p.Action) {
		return LinkPattern{}, fmt.Errorf(
			"invalid action %q; must be one of [%s]", p.Action, strings.Join(validLinkActions, " "),
		)
	}
	return p, nil
}

// ToFrameEntry serialises the pattern to its wire-format map. Only non-default
// optional fields are included: "flags" is omitted when "g", "group" when 0,
// and "id"/"payload"/"hover"/"class" when empty. Class is emitted under the key
// "class".
func (p LinkPattern) ToFrameEntry() map[string]any {
	entry := map[string]any{
		"pattern": p.Pattern,
		"action":  p.Action,
	}
	if p.ID != nil {
		entry["id"] = *p.ID
	}
	if p.Flags != "g" {
		entry["flags"] = p.Flags
	}
	if p.Group != 0 {
		entry["group"] = p.Group
	}
	if p.Payload != "" {
		entry["payload"] = p.Payload
	}
	if p.Hover != "" {
		entry["hover"] = p.Hover
	}
	if p.Class != "" {
		entry["class"] = p.Class
	}
	return entry
}

// LinkPatternRegistry tracks the active link-pattern set for one owner
// (session, worker, etc.), ported from the Python LinkPatternRegistry.
//
// Patterns are kept in insertion order. Registering a pattern whose ID already
// exists replaces the earlier one in place (its slot position is preserved).
// Patterns without an ID are appended and cannot be removed individually.
//
// Unlike the Python original — which relies on the GIL for atomicity — this
// port guards its state with a sync.Mutex, so it is safe for concurrent use.
type LinkPatternRegistry struct {
	mu      sync.Mutex
	order   []any // key insertion order: string ID or int counter sentinel
	items   map[any]LinkPattern
	counter int
}

// NewLinkPatternRegistry returns an empty registry ready for use. The zero
// value is also usable; this constructor is provided for clarity.
func NewLinkPatternRegistry() *LinkPatternRegistry {
	return &LinkPatternRegistry{}
}

func (r *LinkPatternRegistry) ensureInit() {
	if r.items == nil {
		r.items = make(map[any]LinkPattern)
	}
}

// Register adds pattern to the active set. A pattern with an ID that already
// exists replaces the earlier entry in place; an ID-less pattern is appended
// under a private counter key.
func (r *LinkPatternRegistry) Register(pattern LinkPattern) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.ensureInit()

	var key any
	if pattern.ID != nil {
		key = *pattern.ID
	} else {
		key = r.counter
		r.counter++
	}
	if _, exists := r.items[key]; !exists {
		r.order = append(r.order, key)
	}
	r.items[key] = pattern
}

// Unregister removes the pattern registered under patternID, returning true if
// one was found and removed. ID-less patterns (keyed by an int counter) never
// match a string ID.
func (r *LinkPatternRegistry) Unregister(patternID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.ensureInit()

	if _, exists := r.items[patternID]; !exists {
		return false
	}
	delete(r.items, patternID)
	for i, k := range r.order {
		if k == patternID {
			r.order = append(r.order[:i], r.order[i+1:]...)
			break
		}
	}
	return true
}

// Clear removes all patterns and resets the ID-less counter.
func (r *LinkPatternRegistry) Clear() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.items = make(map[any]LinkPattern)
	r.order = nil
	r.counter = 0
}

// GetAll returns all active patterns in insertion order.
func (r *LinkPatternRegistry) GetAll() []LinkPattern {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]LinkPattern, 0, len(r.order))
	for _, k := range r.order {
		out = append(out, r.items[k])
	}
	return out
}

// SyncPayload returns a fresh {"type": "link_patterns", "patterns": [...]} map
// ready for controlchannel.EncodeControlFrame. It is non-destructive.
func (r *LinkPatternRegistry) SyncPayload() map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	patterns := make([]any, 0, len(r.order))
	for _, k := range r.order {
		patterns = append(patterns, r.items[k].ToFrameEntry())
	}
	return map[string]any{"type": "link_patterns", "patterns": patterns}
}
