//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"encoding/json"
	"fmt"
	"os"
)

// RulesPath is a filesystem path to a rules.json file. It exists so
// LoadRuleset can distinguish a path (Python's pathlib.Path) from an inline
// JSON string, matching the reference load_ruleset overloads.
type RulesPath string

// RuleSetFromJSON parses a RuleSet from a JSON byte slice (model_validate).
func RuleSetFromJSON(data []byte) (*RuleSet, error) {
	var rs RuleSet
	if err := json.Unmarshal(data, &rs); err != nil {
		return nil, err
	}
	return &rs, nil
}

// RuleSetFromJSONFile loads a RuleSet from a file. Mirrors
// RuleSet.from_json_file, including its error prefix.
func RuleSetFromJSONFile(path string) (*RuleSet, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		//nolint:staticcheck // ST1005: message kept byte-identical to the Python implementation
		return nil, fmt.Errorf("Failed to load rules from %s: %w", path, err)
	}
	rs, err := RuleSetFromJSON(data)
	if err != nil {
		//nolint:staticcheck // ST1005: message kept byte-identical to the Python implementation
		return nil, fmt.Errorf("Failed to load rules from %s: %w", path, err)
	}
	return rs, nil
}

// LoadRuleset loads a RuleSet from a *RuleSet (passthrough), a RulesPath
// (file), or a string (inline JSON). Faithful to the Python load_ruleset,
// including its error messages.
func LoadRuleset(source any) (*RuleSet, error) {
	switch src := source.(type) {
	case *RuleSet:
		return src, nil
	case RuleSet:
		return &src, nil
	case RulesPath:
		if _, err := os.Stat(string(src)); err != nil {
			//nolint:staticcheck // ST1005: message kept byte-identical to the Python implementation
			return nil, fmt.Errorf("Rules file not found: %s", string(src))
		}
		return RuleSetFromJSONFile(string(src))
	case string:
		rs, err := RuleSetFromJSON([]byte(src))
		if err != nil {
			//nolint:staticcheck // ST1005: message kept byte-identical to the Python implementation
			return nil, fmt.Errorf("Failed to parse rules: %w", err)
		}
		return rs, nil
	default:
		//nolint:staticcheck // ST1005: message kept byte-identical to the Python implementation
		return nil, fmt.Errorf("Failed to parse rules: unsupported source type %T", source)
	}
}
