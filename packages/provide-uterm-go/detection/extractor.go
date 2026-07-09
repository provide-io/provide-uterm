//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// ExtractKV extracts structured key/value data from screen text using the
// configured patterns. kvConfig may be a single field config (map[string]any
// with a "field" key), a list of field configs ([]any of maps), or nil.
//
// It mirrors KVExtractor.extract with validation enabled (the engine calls it
// that way): the returned map includes a "_validation" entry unless nothing
// was extracted, in which case the result is nil.
func ExtractKV(screen string, kvConfig any) map[string]any {
	return kvExtract(screen, kvConfig, true)
}

// kvExtract is the full KVExtractor.extract with an explicit run_validation
// toggle (exposed to tests via ExtractKVWithValidation).
func kvExtract(screen string, kvConfig any, runValidation bool) map[string]any {
	if !pyTruthy(kvConfig) {
		return nil
	}
	configs, ok := normalizeConfigs(kvConfig)
	if !ok {
		return nil
	}

	extracted := map[string]any{}
	for _, cfg := range configs {
		name, value, matched := extractSingleField(screen, cfg)
		if matched {
			extracted[name] = value
		}
	}
	if len(extracted) == 0 {
		return nil
	}

	if runValidation {
		extracted["_validation"] = validateExtracted(extracted, configs)
	}
	return extracted
}

// ExtractKVWithValidation exposes the run_validation toggle for tests.
func ExtractKVWithValidation(screen string, kvConfig any, runValidation bool) map[string]any {
	return kvExtract(screen, kvConfig, runValidation)
}

// normalizeConfigs converts the polymorphic kv_config into a list of config
// maps, matching Python's single-dict / list handling. The bool reports
// whether the shape was valid.
func normalizeConfigs(kvConfig any) ([]map[string]any, bool) {
	switch cfg := kvConfig.(type) {
	case map[string]any:
		if _, hasField := cfg["field"]; hasField {
			return []map[string]any{cfg}, true
		}
		return nil, false
	case []map[string]any:
		return cfg, true
	case []any:
		out := make([]map[string]any, 0, len(cfg))
		for _, item := range cfg {
			if m, ok := item.(map[string]any); ok {
				out = append(out, m)
			}
		}
		return out, true
	default:
		return nil, false
	}
}

// extractSingleField extracts one field from screen using its config map,
// returning (field, value, matched). matched is false when the field/regex is
// missing, nothing matched, or the type conversion failed.
func extractSingleField(screen string, config map[string]any) (string, any, bool) {
	fieldName := patternString(config, "field", "")
	fieldType := patternString(config, "type", "string")
	pattern := patternString(config, "regex", "")

	if fieldName == "" || pattern == "" {
		return "", nil, false
	}

	re, err := compilePyRegex(pattern, "mi")
	if err != nil {
		return "", nil, false
	}
	// Use the last match: screen buffers carry scroll history, so the most
	// recent value is at the end.
	all := re.FindAllStringSubmatchIndex(screen, -1)
	if len(all) == 0 {
		return "", nil, false
	}
	loc := all[len(all)-1]

	var valueStr string
	if re.NumSubexp() >= 1 && loc[2] >= 0 { // group 1 participated
		valueStr = screen[loc[2]:loc[3]]
	} else {
		valueStr = screen[loc[0]:loc[1]]
	}

	value, ok := convertType(valueStr, fieldType)
	if !ok {
		return "", nil, false
	}
	return fieldName, value, true
}

// convertType converts a captured string to the target type, returning ok=false
// on conversion failure (mirroring Python's ValueError -> None).
func convertType(valueStr, targetType string) (any, bool) {
	valueStr = pyStrip(valueStr)

	switch targetType {
	case "string":
		return valueStr, true
	case "int":
		n, err := strconv.Atoi(strings.ReplaceAll(valueStr, ",", ""))
		if err != nil {
			return nil, false
		}
		return n, true
	case "float":
		f, err := strconv.ParseFloat(strings.ReplaceAll(valueStr, ",", ""), 64)
		if err != nil {
			return nil, false
		}
		return f, true
	case "bool":
		switch strings.ToLower(valueStr) {
		case "true", "yes", "y", "1", "on":
			return true, true
		case "false", "no", "n", "0", "off":
			return false, true
		default:
			return nil, false
		}
	default:
		return valueStr, true
	}
}

// validateExtracted validates extracted values against the config constraints,
// returning {"valid": bool, "errors": []string}. Error strings are byte-for-
// byte compatible with the Python implementation.
func validateExtracted(extracted map[string]any, configs []map[string]any) map[string]any {
	errors := []string{}
	for _, cfg := range configs {
		fieldRaw, hasField := cfg["field"]
		field, isStr := fieldRaw.(string)
		if !hasField || !isStr {
			continue
		}
		value, present := extracted[field]
		rules, _ := cfg["validate"].(map[string]any)
		required := pyTruthy(cfg["required"])
		fieldType := patternString(cfg, "type", "string")

		if required && !present {
			errors = append(errors, fmt.Sprintf("%s: required but not found", field))
			continue
		}
		if !present {
			continue
		}
		switch fieldType {
		case "int", "float":
			validateNumeric(field, value, rules, &errors, fieldType)
		case "string":
			validateString(field, value, rules, &errors)
		}
	}
	return map[string]any{"valid": len(errors) == 0, "errors": errors}
}

func validateNumeric(field string, value any, rules map[string]any, errors *[]string, fieldType string) {
	wantInt := fieldType == "int"
	typeOK := false
	switch value.(type) {
	case int, int64:
		typeOK = wantInt
	case float64:
		typeOK = !wantInt
	}
	if !typeOK {
		*errors = append(*errors, fmt.Sprintf("%s: expected %s, got %s", field, fieldType, pyTypeName(value)))
		return
	}
	valF, _ := toFloat(value)
	if mn, ok := rules["min"]; ok {
		if mnF, fok := toFloat(mn); fok && valF < mnF {
			*errors = append(*errors, fmt.Sprintf("%s: value %s below min %s", field, pyNum(value), pyNum(mn)))
		}
	}
	if mx, ok := rules["max"]; ok {
		if mxF, fok := toFloat(mx); fok && valF > mxF {
			*errors = append(*errors, fmt.Sprintf("%s: value %s exceeds max %s", field, pyNum(value), pyNum(mx)))
		}
	}
}

func validateString(field string, value any, rules map[string]any, errors *[]string) {
	s, ok := value.(string)
	if !ok {
		*errors = append(*errors, fmt.Sprintf("%s: expected string, got %s", field, pyTypeName(value)))
		return
	}
	if pat, ok := rules["pattern"].(string); ok {
		// Python re.match: the pattern must match starting at position 0
		// (no MULTILINE/IGNORECASE flags).
		re, err := compilePyRegex(pat, "")
		if err != nil || !matchAtStart(re, s) {
			*errors = append(*errors, fmt.Sprintf("%s: value '%s' does not match pattern %s", field, s, pat))
		}
	}
	if allowed, ok := rules["allowed_values"]; ok {
		if !inAllowed(s, allowed) {
			*errors = append(*errors, fmt.Sprintf("%s: value '%s' not in allowed values %s", field, s, pyList(allowed)))
		}
	}
}

// matchAtStart mirrors Python's re.match: the pattern must match starting at
// position 0 of s.
func matchAtStart(re *regexp.Regexp, s string) bool {
	loc := re.FindStringIndex(s)
	return loc != nil && loc[0] == 0
}

func inAllowed(value string, allowed any) bool {
	switch xs := allowed.(type) {
	case []any:
		for _, item := range xs {
			if s, ok := item.(string); ok && s == value {
				return true
			}
		}
	case []string:
		for _, item := range xs {
			if item == value {
				return true
			}
		}
	}
	return false
}

func pyTypeName(v any) string {
	switch v.(type) {
	case string:
		return "str"
	case int, int64:
		return "int"
	case float64:
		return "float"
	case bool:
		return "bool"
	default:
		return "NoneType"
	}
}

// pyNum formats an int/float the way Python's str() would inside an f-string.
func pyNum(v any) string {
	switch x := v.(type) {
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64)
	default:
		return fmt.Sprintf("%v", v)
	}
}

// pyList renders a slice the way Python's str(list) would (for allowed_values
// diagnostics), e.g. ['Normal', 'Test'].
func pyList(v any) string {
	items, ok := v.([]any)
	if !ok {
		return fmt.Sprintf("%v", v)
	}
	parts := make([]string, 0, len(items))
	for _, it := range items {
		if s, ok := it.(string); ok {
			parts = append(parts, "'"+s+"'")
		} else {
			parts = append(parts, pyNum(it))
		}
	}
	return "[" + strings.Join(parts, ", ") + "]"
}
