//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"encoding/json"
	"fmt"
)

// reMultilineIgnorecase mirrors Python's re.MULTILINE | re.IGNORECASE (8 | 2),
// the default flag value on RegexRule and KVExtractRule.
const reMultilineIgnorecase = 10

// RegexRule is a match pattern with a mode. Faithful to the Python model.
type RegexRule struct {
	Pattern   string `json:"pattern"`
	Flags     int    `json:"flags"`
	MatchMode string `json:"match_mode"`
}

// UnmarshalJSON applies the RegexRule defaults (flags, match_mode).
func (r *RegexRule) UnmarshalJSON(data []byte) error {
	type shadow struct {
		Pattern   string `json:"pattern"`
		Flags     *int   `json:"flags"`
		MatchMode string `json:"match_mode"`
	}
	s := shadow{MatchMode: "regex"}
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	r.Pattern = s.Pattern
	r.MatchMode = s.MatchMode
	if r.MatchMode == "" {
		r.MatchMode = "regex"
	}
	if s.Flags != nil {
		r.Flags = *s.Flags
	} else {
		r.Flags = reMultilineIgnorecase
	}
	return nil
}

// ToRegex returns the regex string for this rule, and ok=false when the
// match_mode is unrecognized (mirroring Python's implicit fall-through to
// None).
func (r RegexRule) ToRegex() (string, bool) {
	switch r.MatchMode {
	case "regex":
		return r.Pattern, true
	case "contains":
		return pyRegexEscape(r.Pattern), true
	case "exact":
		return "^" + pyRegexEscape(r.Pattern) + "$", true
	default:
		return "", false
	}
}

// ScreenConstraint constrains where a prompt may appear on screen.
type ScreenConstraint struct {
	ExpectCursorAtEnd bool `json:"expect_cursor_at_end"`
	CursorRowMin      *int `json:"cursor_row_min"`
	CursorRowMax      *int `json:"cursor_row_max"`
	CursorColMin      *int `json:"cursor_col_min"`
	CursorColMax      *int `json:"cursor_col_max"`
}

// UnmarshalJSON applies the ExpectCursorAtEnd default (true).
func (s *ScreenConstraint) UnmarshalJSON(data []byte) error {
	type shadow struct {
		ExpectCursorAtEnd *bool `json:"expect_cursor_at_end"`
		CursorRowMin      *int  `json:"cursor_row_min"`
		CursorRowMax      *int  `json:"cursor_row_max"`
		CursorColMin      *int  `json:"cursor_col_min"`
		CursorColMax      *int  `json:"cursor_col_max"`
	}
	sh := shadow{}
	if err := json.Unmarshal(data, &sh); err != nil {
		return err
	}
	s.ExpectCursorAtEnd = sh.ExpectCursorAtEnd == nil || *sh.ExpectCursorAtEnd
	s.CursorRowMin, s.CursorRowMax = sh.CursorRowMin, sh.CursorRowMax
	s.CursorColMin, s.CursorColMax = sh.CursorColMin, sh.CursorColMax
	return nil
}

func defaultScreenConstraint() ScreenConstraint {
	return ScreenConstraint{ExpectCursorAtEnd: true}
}

// KVExtractRule describes a single key/value extraction.
type KVExtractRule struct {
	Field    string         `json:"field"`
	Regex    string         `json:"regex"`
	Type     string         `json:"type"`
	Flags    int            `json:"flags"`
	Validate map[string]any `json:"validate"`
	Required bool           `json:"required"`
}

// UnmarshalJSON applies KVExtractRule defaults (type, flags) and the
// "validate" alias.
func (k *KVExtractRule) UnmarshalJSON(data []byte) error {
	type shadow struct {
		Field    string         `json:"field"`
		Regex    string         `json:"regex"`
		Type     string         `json:"type"`
		Flags    *int           `json:"flags"`
		Validate map[string]any `json:"validate"`
		Required bool           `json:"required"`
	}
	s := shadow{Type: "string"}
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	k.Field, k.Regex, k.Required, k.Validate = s.Field, s.Regex, s.Required, s.Validate
	k.Type = s.Type
	if k.Type == "" {
		k.Type = "string"
	}
	if s.Flags != nil {
		k.Flags = *s.Flags
	} else {
		k.Flags = reMultilineIgnorecase
	}
	return nil
}

// PromptRule is one detectable prompt.
type PromptRule struct {
	ID            string           `json:"id"`
	Kind          string           `json:"kind"`
	InputType     string           `json:"input_type"`
	Match         RegexRule        `json:"match"`
	Screen        ScreenConstraint `json:"screen"`
	KVExtract     []KVExtractRule  `json:"kv_extract"`
	Notes         *string          `json:"notes"`
	NegativeMatch *RegexRule       `json:"negative_match"`
	DefaultAction *ActionRule      `json:"default_action"`
}

// UnmarshalJSON applies PromptRule defaults (kind, input_type, screen).
func (p *PromptRule) UnmarshalJSON(data []byte) error {
	type shadow struct {
		ID            string            `json:"id"`
		Kind          string            `json:"kind"`
		InputType     string            `json:"input_type"`
		Match         RegexRule         `json:"match"`
		Screen        *ScreenConstraint `json:"screen"`
		KVExtract     []KVExtractRule   `json:"kv_extract"`
		Notes         *string           `json:"notes"`
		NegativeMatch *RegexRule        `json:"negative_match"`
		DefaultAction *ActionRule       `json:"default_action"`
	}
	s := shadow{Kind: "unknown", InputType: "multi_key"}
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	p.ID, p.Match = s.ID, s.Match
	p.KVExtract, p.Notes = s.KVExtract, s.Notes
	p.NegativeMatch, p.DefaultAction = s.NegativeMatch, s.DefaultAction
	p.Kind = orDefault(s.Kind, "unknown")
	p.InputType = orDefault(s.InputType, "multi_key")
	if s.Screen != nil {
		p.Screen = *s.Screen
	} else {
		p.Screen = defaultScreenConstraint()
	}
	return nil
}

// MenuOption is a single selectable option in a menu.
type MenuOption struct {
	Key   string `json:"key"`
	Label string `json:"label"`
}

// MenuRule describes a menu prompt.
type MenuRule struct {
	ID          string       `json:"id"`
	TitleMatch  *RegexRule   `json:"title_match"`
	PromptMatch RegexRule    `json:"prompt_match"`
	Options     []MenuOption `json:"options"`
	Notes       *string      `json:"notes"`
}

// TimingRule holds action timing configuration.
type TimingRule struct {
	MinWaitMs           int  `json:"min_wait_ms"`
	MaxWaitMs           int  `json:"max_wait_ms"`
	RetryMs             int  `json:"retry_ms"`
	RequireStableScreen bool `json:"require_stable_screen"`
}

// UnmarshalJSON applies TimingRule defaults.
func (t *TimingRule) UnmarshalJSON(data []byte) error {
	type shadow struct {
		MinWaitMs           *int  `json:"min_wait_ms"`
		MaxWaitMs           *int  `json:"max_wait_ms"`
		RetryMs             *int  `json:"retry_ms"`
		RequireStableScreen *bool `json:"require_stable_screen"`
	}
	s := shadow{}
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	*t = defaultTimingRule()
	if s.MinWaitMs != nil {
		t.MinWaitMs = *s.MinWaitMs
	}
	if s.MaxWaitMs != nil {
		t.MaxWaitMs = *s.MaxWaitMs
	}
	if s.RetryMs != nil {
		t.RetryMs = *s.RetryMs
	}
	if s.RequireStableScreen != nil {
		t.RequireStableScreen = *s.RequireStableScreen
	}
	return nil
}

func defaultTimingRule() TimingRule {
	return TimingRule{MinWaitMs: 0, MaxWaitMs: 8000, RetryMs: 250, RequireStableScreen: true}
}

// ActionRule is one step in a flow.
type ActionRule struct {
	ID             string      `json:"id"`
	Kind           string      `json:"kind"`
	Keys           *string     `json:"keys"`
	ExpectsPrompt  *string     `json:"expects_prompt"`
	Timing         TimingRule  `json:"timing"`
	GatePrompts    []string    `json:"gate_prompts"`
	BlockIfMatches []RegexRule `json:"block_if_matches"`
}

// UnmarshalJSON applies the ActionRule timing default.
func (a *ActionRule) UnmarshalJSON(data []byte) error {
	type shadow struct {
		ID             string      `json:"id"`
		Kind           string      `json:"kind"`
		Keys           *string     `json:"keys"`
		ExpectsPrompt  *string     `json:"expects_prompt"`
		Timing         *TimingRule `json:"timing"`
		GatePrompts    []string    `json:"gate_prompts"`
		BlockIfMatches []RegexRule `json:"block_if_matches"`
	}
	s := shadow{}
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	a.ID, a.Kind, a.Keys, a.ExpectsPrompt = s.ID, s.Kind, s.Keys, s.ExpectsPrompt
	a.GatePrompts, a.BlockIfMatches = s.GatePrompts, s.BlockIfMatches
	if s.Timing != nil {
		a.Timing = *s.Timing
	} else {
		a.Timing = defaultTimingRule()
	}
	return nil
}

// KeysOrNil returns the send-keys string, or "" when unset.
func (a ActionRule) KeysOrNil() (string, bool) {
	if a.Keys == nil {
		return "", false
	}
	return *a.Keys, true
}

// FlowRule is a named sequence of actions.
type FlowRule struct {
	ID          string       `json:"id"`
	Description string       `json:"description"`
	Steps       []ActionRule `json:"steps"`
}

// RuleSet is the top-level rule collection loaded from rules.json.
type RuleSet struct {
	Version  string         `json:"version"`
	Game     string         `json:"game"`
	Prompts  []PromptRule   `json:"prompts"`
	Menus    []MenuRule     `json:"menus"`
	Flows    []FlowRule     `json:"flows"`
	Metadata map[string]any `json:"metadata"`
}

// UnmarshalJSON applies RuleSet defaults and enforces the required "game" field.
func (rs *RuleSet) UnmarshalJSON(data []byte) error {
	type shadow struct {
		Version  *string        `json:"version"`
		Game     *string        `json:"game"`
		Prompts  []PromptRule   `json:"prompts"`
		Menus    []MenuRule     `json:"menus"`
		Flows    []FlowRule     `json:"flows"`
		Metadata map[string]any `json:"metadata"`
	}
	s := shadow{}
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	if s.Game == nil {
		return fmt.Errorf("field required: game")
	}
	rs.Game = *s.Game
	rs.Version = "1.0"
	if s.Version != nil {
		rs.Version = *s.Version
	}
	rs.Prompts, rs.Menus, rs.Flows, rs.Metadata = s.Prompts, s.Menus, s.Flows, s.Metadata
	if rs.Prompts == nil {
		rs.Prompts = []PromptRule{}
	}
	if rs.Menus == nil {
		rs.Menus = []MenuRule{}
	}
	if rs.Flows == nil {
		rs.Flows = []FlowRule{}
	}
	if rs.Metadata == nil {
		rs.Metadata = map[string]any{}
	}
	return nil
}

// ToPromptPatterns converts the ruleset's prompts into detector pattern maps,
// preserving order (specific-before-generic). Byte-compatible with the Python
// RuleSet.to_prompt_patterns.
func (rs RuleSet) ToPromptPatterns() []Pattern {
	patterns := make([]Pattern, 0, len(rs.Prompts))
	for _, prompt := range rs.Prompts {
		regex, _ := prompt.Match.ToRegex()
		notes := ""
		if prompt.Notes != nil {
			notes = *prompt.Notes
		}
		pattern := Pattern{
			"id":                   prompt.ID,
			"regex":                regex,
			"input_type":           prompt.InputType,
			"expect_cursor_at_end": prompt.Screen.ExpectCursorAtEnd,
			"notes":                notes,
			"auto_detected":        false,
		}
		if prompt.NegativeMatch != nil {
			neg, _ := prompt.NegativeMatch.ToRegex()
			pattern["negative_regex"] = neg
		}
		if len(prompt.KVExtract) > 0 {
			kv := make([]any, 0, len(prompt.KVExtract))
			for _, item := range prompt.KVExtract {
				kv = append(kv, map[string]any{
					"field":    item.Field,
					"regex":    item.Regex,
					"type":     item.Type,
					"flags":    item.Flags,
					"validate": item.Validate,
					"required": item.Required,
				})
			}
			pattern["kv_extract"] = kv
		}
		patterns = append(patterns, pattern)
	}
	return patterns
}

func orDefault(v, def string) string {
	if v == "" {
		return def
	}
	return v
}
