//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"
	"strings"
)

// FlowStep is the decision returned by FlowEngine.Advance. A nil
// CurrentPromptID or NextAction corresponds to Python's None.
type FlowStep struct {
	FlowID          string
	CurrentPromptID *string
	NextAction      *string
	Done            bool
	KVData          map[string]any
}

// FlowEngine advances named flows using prompt detectors and rule metadata.
type FlowEngine struct {
	flows          map[string]FlowRule
	promptPatterns map[string]Pattern
	// detectorCache holds one PromptDetector per prompt-id set. Advance calls
	// detectPrompt once per flow step and login polls Advance ~every 0.2s, so
	// caching avoids recompiling every pattern on every call (the perf fix
	// preserved from commit 1ff5d8d4).
	detectorCache map[string]*PromptDetector
	// posRegexCache caches the no-flags regex used by matchPosition.
	posRegexCache map[string]*regexp.Regexp
}

// NewFlowEngine builds a flow engine from a ruleset.
func NewFlowEngine(ruleset *RuleSet) *FlowEngine {
	flows := make(map[string]FlowRule, len(ruleset.Flows))
	for _, flow := range ruleset.Flows {
		flows[flow.ID] = flow
	}
	patterns := make(map[string]Pattern)
	for _, pattern := range ruleset.ToPromptPatterns() {
		patterns[asString(pattern["id"])] = pattern
	}
	return &FlowEngine{
		flows:          flows,
		promptPatterns: patterns,
		detectorCache:  map[string]*PromptDetector{},
		posRegexCache:  map[string]*regexp.Regexp{},
	}
}

type flowCandidate struct {
	position [2]int
	index    int
	action   ActionRule
	match    *PromptMatch
}

// Advance returns the next action for flowID on the current screen. When
// several steps' prompts match, the one whose match sits closest to the tail
// wins; ties keep the earliest flow step.
func (fe *FlowEngine) Advance(flowID, screen string, cursor *[2]int) (FlowStep, error) {
	flow, ok := fe.flows[flowID]
	if !ok {
		return FlowStep{}, fmt.Errorf("unknown flow: %s", flowID)
	}
	snapshot := fe.snapshotFor(screen, cursor)
	lastIndex := len(flow.Steps) - 1

	var best *flowCandidate
	for index, action := range flow.Steps {
		promptIDs := fe.candidatePromptIDs(action)
		match := fe.detectPrompt(snapshot, promptIDs)
		if match == nil {
			continue
		}
		position := fe.matchPosition(screen, match.PromptID)
		if best == nil || positionGreater(position, best.position) {
			best = &flowCandidate{position: position, index: index, action: action, match: match}
		}
	}

	if best == nil {
		return FlowStep{FlowID: flow.ID, KVData: map[string]any{}}, nil
	}

	pattern := fe.promptPatterns[best.match.PromptID]
	kvData := ExtractKV(screen, pattern["kv_extract"])
	if kvData == nil {
		kvData = map[string]any{}
	}
	terminal := fe.isTerminal(best.action, best.index == lastIndex)

	var nextAction *string
	if !terminal && best.action.Kind == "send_keys" {
		nextAction = best.action.Keys
	}
	promptID := best.match.PromptID
	return FlowStep{
		FlowID:          flow.ID,
		CurrentPromptID: &promptID,
		NextAction:      nextAction,
		Done:            terminal,
		KVData:          kvData,
	}, nil
}

// matchPosition returns the tail-most ranking key (end, -start) for the
// prompt's regex in screen, defaulting to (len(screen), 0) when it finds
// nothing (treated as tail-most).
func (fe *FlowEngine) matchPosition(screen, promptID string) [2]int {
	re := fe.posRegex(promptID)
	best := [2]int{len(screen), 0}
	found := false
	if re != nil {
		for _, loc := range re.FindAllStringIndex(screen, -1) {
			key := [2]int{loc[1], -loc[0]}
			if !found || positionGreater(key, best) {
				best = key
				found = true
			}
		}
	}
	return best
}

func (fe *FlowEngine) posRegex(promptID string) *regexp.Regexp {
	if re, ok := fe.posRegexCache[promptID]; ok {
		return re
	}
	var re *regexp.Regexp
	if pattern, exists := fe.promptPatterns[promptID]; exists {
		if compiled, err := compilePyRegex(asString(pattern["regex"]), ""); err == nil {
			re = compiled
		}
	}
	fe.posRegexCache[promptID] = re
	return re
}

func (fe *FlowEngine) candidatePromptIDs(action ActionRule) []string {
	candidates := append([]string{}, action.GatePrompts...)
	if action.ExpectsPrompt != nil && *action.ExpectsPrompt != "" {
		ep := *action.ExpectsPrompt
		if !containsString(candidates, ep) {
			candidates = append(candidates, ep)
		}
	}
	return candidates
}

func (fe *FlowEngine) detectPrompt(snapshot Snapshot, promptIDs []string) *PromptMatch {
	if len(promptIDs) == 0 {
		return nil
	}
	key := strings.Join(promptIDs, "\x00")
	detector, ok := fe.detectorCache[key]
	if !ok {
		patterns := make([]Pattern, 0, len(promptIDs))
		for _, id := range promptIDs {
			if p, exists := fe.promptPatterns[id]; exists {
				patterns = append(patterns, p)
			}
		}
		if len(patterns) == 0 {
			return nil
		}
		detector = mustDetector(patterns)
		fe.detectorCache[key] = detector
	}
	return detector.DetectPrompt(snapshot)
}

func (fe *FlowEngine) isTerminal(action ActionRule, isLast bool) bool {
	if action.Kind == "noop" {
		return true
	}
	return isLast && action.Keys == nil
}

func (fe *FlowEngine) snapshotFor(screen string, cursor *[2]int) Snapshot {
	var cursorDict map[string]any
	if cursor == nil {
		cursorDict = map[string]any{"x": 0, "y": strings.Count(screen, "\n")}
	} else {
		cursorDict = map[string]any{"x": cursor[0], "y": cursor[1]}
	}
	sum := sha256.Sum256([]byte(screen))
	return Snapshot{
		"screen":             screen,
		"screen_hash":        hex.EncodeToString(sum[:]),
		"cursor_at_end":      true,
		"has_trailing_space": strings.HasSuffix(screen, " "),
		"cursor":             cursorDict,
	}
}

// positionGreater reports whether a > b under Python tuple ordering.
func positionGreater(a, b [2]int) bool {
	if a[0] != b[0] {
		return a[0] > b[0]
	}
	return a[1] > b[1]
}

func containsString(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}
