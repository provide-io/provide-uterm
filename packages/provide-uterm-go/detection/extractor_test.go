//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"strings"
	"testing"
)

func cfg(field, ftype, regex string) map[string]any {
	return map[string]any{"field": field, "type": ftype, "regex": regex}
}

func TestExtractStringField(t *testing.T) {
	result := ExtractKVWithValidation("Player: TestUser\nScore: 1000", cfg("player", "string", `Player:\s*(\w+)`), false)
	if result == nil || result["player"] != "TestUser" {
		t.Fatalf("result = %v", result)
	}
}

func TestExtractIntField(t *testing.T) {
	result := ExtractKVWithValidation("Score: 1000\nLevel: 5", cfg("score", "int", `Score:\s*(\d+)`), false)
	if result == nil || result["score"] != 1000 {
		t.Fatalf("result = %v", result)
	}
}

func TestExtractIntWithCommas(t *testing.T) {
	result := ExtractKVWithValidation("Credits: 1,234,567", cfg("credits", "int", `Credits:\s*([\d,]+)`), false)
	if result == nil || result["credits"] != 1234567 {
		t.Fatalf("result = %v", result)
	}
}

func TestExtractFloatField(t *testing.T) {
	result := ExtractKVWithValidation("Temperature: 98.6 degrees", cfg("temp", "float", `Temperature:\s*([\d.]+)`), false)
	if result == nil || result["temp"] != 98.6 {
		t.Fatalf("result = %v", result)
	}
	result = ExtractKVWithValidation("Value: 1,234.56", cfg("value", "float", `Value:\s*([\d,.]+)`), false)
	if result == nil || result["value"] != 1234.56 {
		t.Fatalf("float commas = %v", result)
	}
}

func TestExtractBoolField(t *testing.T) {
	for _, v := range []string{"true", "True", "TRUE", "yes", "Yes", "y", "Y", "1", "on", "ON"} {
		result := ExtractKVWithValidation("Flag: "+v, cfg("flag", "bool", `Flag:\s*(\w+)`), false)
		if result == nil || result["flag"] != true {
			t.Errorf("true variant %q -> %v", v, result)
		}
	}
	for _, v := range []string{"false", "False", "FALSE", "no", "No", "n", "N", "0", "off", "OFF"} {
		result := ExtractKVWithValidation("Flag: "+v, cfg("flag", "bool", `Flag:\s*(\w+)`), false)
		if result == nil || result["flag"] != false {
			t.Errorf("false variant %q -> %v", v, result)
		}
	}
	if ExtractKVWithValidation("Flag: maybe", cfg("flag", "bool", `Flag:\s*(\w+)`), false) != nil {
		t.Error("invalid bool should fail extraction")
	}
}

func TestExtractConversionFailures(t *testing.T) {
	if ExtractKVWithValidation("Score: abc", cfg("score", "int", `Score:\s*(\w+)`), false) != nil {
		t.Error("int conversion failure")
	}
	if ExtractKVWithValidation("Temp: abc", cfg("temp", "float", `Temp:\s*(\w+)`), false) != nil {
		t.Error("float conversion failure")
	}
}

func TestExtractUnknownTypeReturnsString(t *testing.T) {
	result := ExtractKVWithValidation("Value: test123", cfg("value", "unknown", `Value:\s*(\w+)`), false)
	if result == nil || result["value"] != "test123" {
		t.Fatalf("result = %v", result)
	}
}

func TestExtractNoMatchReturnsNil(t *testing.T) {
	if ExtractKVWithValidation("Player: TestUser", cfg("score", "int", `Score:\s*(\d+)`), false) != nil {
		t.Error("no match")
	}
}

func TestExtractInvalidConfig(t *testing.T) {
	if ExtractKVWithValidation("x", nil, false) != nil {
		t.Error("nil config")
	}
	if ExtractKVWithValidation("x", map[string]any{}, false) != nil {
		t.Error("empty dict config")
	}
	// dict without "field" key
	if ExtractKVWithValidation("Score: 100", map[string]any{"regex": `Score:\s*(\d+)`}, false) != nil {
		t.Error("dict without field")
	}
	// unsupported shape
	if ExtractKVWithValidation("x", 42, false) != nil {
		t.Error("int config")
	}
	// empty field / empty pattern entries skipped
	configs := []any{
		map[string]any{"field": "", "regex": `Score:\s*(\d+)`, "type": "int"},
		map[string]any{"field": "score", "regex": "", "type": "int"},
	}
	if ExtractKVWithValidation("Score: 100", configs, false) != nil {
		t.Error("skipped entries")
	}
	// invalid regex entry skipped
	if ExtractKVWithValidation("Score: 100", cfg("score", "int", `[bad`), false) != nil {
		t.Error("invalid regex")
	}
	// non-map entries in []any ignored
	if got := ExtractKVWithValidation("Score: 100", []any{"junk", cfg("score", "int", `Score:\s*(\d+)`)}, false); got == nil || got["score"] != 100 {
		t.Errorf("mixed list = %v", got)
	}
}

func TestExtractMultipleFields(t *testing.T) {
	screen := "\nPlayer: TestUser\nScore: 1000\nLevel: 5\n"
	configs := []any{
		cfg("player", "string", `Player:\s*(\w+)`),
		cfg("score", "int", `Score:\s*(\d+)`),
		cfg("level", "int", `Level:\s*(\d+)`),
	}
	result := ExtractKVWithValidation(screen, configs, false)
	if result == nil || result["player"] != "TestUser" || result["score"] != 1000 || result["level"] != 5 {
		t.Fatalf("result = %v", result)
	}
	// typed-slice form
	typed := []map[string]any{cfg("player", "string", `Player:\s*(\w+)`)}
	if got := ExtractKVWithValidation(screen, typed, false); got == nil || got["player"] != "TestUser" {
		t.Errorf("typed slice = %v", got)
	}
}

func TestExtractPartialMatch(t *testing.T) {
	configs := []any{
		cfg("player", "string", `Player:\s*(\w+)`),
		cfg("level", "int", `Level:\s*(\d+)`),
	}
	result := ExtractKVWithValidation("Player: TestUser\nScore: 1000", configs, false)
	if result == nil || result["player"] != "TestUser" {
		t.Fatal("partial")
	}
	if _, has := result["level"]; has {
		t.Error("level should be absent")
	}
}

func TestExtractLastMatchWins(t *testing.T) {
	// scroll history: most recent value at the end
	result := ExtractKVWithValidation("Sector 1\nSector 42", cfg("sector", "int", `Sector\s+(\d+)`), false)
	if result == nil || result["sector"] != 42 {
		t.Fatalf("last match = %v", result)
	}
}

func TestExtractNoCaptureGroupUsesWholeMatch(t *testing.T) {
	result := ExtractKVWithValidation("Score: 1000", cfg("score", "int", `\d+`), false)
	if result == nil || result["score"] != 1000 {
		t.Fatalf("whole match = %v", result)
	}
	strRes := ExtractKVWithValidation("Hello World", cfg("match", "string", `Hello`), false)
	if strRes == nil || strRes["match"] != "Hello" {
		t.Fatalf("string whole match = %v", strRes)
	}
}

func TestExtractFirstGroupOfMany(t *testing.T) {
	result := ExtractKVWithValidation("Player: TestUser (Level 5)",
		cfg("player", "string", `Player:\s*(\w+)\s*\(Level\s*(\d+)\)`), false)
	if result == nil || result["player"] != "TestUser" {
		t.Fatalf("first group = %v", result)
	}
}

func TestExtractOptionalGroupNotParticipating(t *testing.T) {
	// group 1 exists but does not participate -> Python lastindex is None,
	// falls back to whole match.
	result := ExtractKVWithValidation("xyz", cfg("v", "string", `xyz(\d+)?`), false)
	if result == nil || result["v"] != "xyz" {
		t.Fatalf("optional group = %v", result)
	}
}

func TestExtractCaseInsensitiveMultiline(t *testing.T) {
	result := ExtractKVWithValidation("PLAYER: TestUser", cfg("player", "string", `player:\s*(\w+)`), false)
	if result == nil || result["player"] != "TestUser" {
		t.Fatalf("case-insensitive = %v", result)
	}
}

// --- validation --------------------------------------------------------------

func vmap(res map[string]any, t *testing.T) map[string]any {
	t.Helper()
	v, ok := res["_validation"].(map[string]any)
	if !ok {
		t.Fatalf("no _validation in %v", res)
	}
	return v
}

func verrs(res map[string]any, t *testing.T) []string {
	t.Helper()
	return vmap(res, t)["errors"].([]string)
}

func TestValidationPasses(t *testing.T) {
	c := cfg("score", "int", `Score:\s*(\d+)`)
	c["validate"] = map[string]any{"min": 0, "max": 2000}
	result := ExtractKV("Score: 1000", c)
	if result == nil || vmap(result, t)["valid"] != true || len(verrs(result, t)) != 0 {
		t.Fatalf("result = %v", result)
	}
}

func TestValidationMinMax(t *testing.T) {
	c := cfg("score", "int", `Score:\s*(-?\d+)`)
	c["validate"] = map[string]any{"min": 0}
	result := ExtractKV("Score: -10", c)
	if vmap(result, t)["valid"] != false || !containsSubstr(verrs(result, t), "below min") {
		t.Errorf("min: %v", result)
	}
	c2 := cfg("score", "int", `Score:\s*(\d+)`)
	c2["validate"] = map[string]any{"max": 2000}
	result2 := ExtractKV("Score: 5000", c2)
	if vmap(result2, t)["valid"] != false || !containsSubstr(verrs(result2, t), "exceeds max") {
		t.Errorf("max: %v", result2)
	}
}

func TestValidationRequiredMissing(t *testing.T) {
	c := cfg("score", "int", `Score:\s*(\d+)`)
	c["required"] = true
	// Nothing extracted at all -> nil result (matches Python)
	if ExtractKV("Player: TestUser", c) != nil {
		t.Error("nothing extracted -> nil")
	}
	// present
	result := ExtractKV("Score: 1000", c)
	if result == nil || result["score"] != 1000 || vmap(result, t)["valid"] != true {
		t.Errorf("present = %v", result)
	}
	// required missing among other extracted fields
	configs := []any{
		cfg("score", "int", `Score:\s*(\d+)`),
		func() map[string]any { c := cfg("level", "int", `Level:\s*(\d+)`); c["required"] = true; return c }(),
	}
	res := ExtractKV("Score: 100", configs)
	if vmap(res, t)["valid"] != false || !containsSubstr(verrs(res, t), "required but not found") {
		t.Errorf("required among = %v", res)
	}
}

func TestValidationPattern(t *testing.T) {
	c := cfg("name", "string", `Name:\s*(\S+)`)
	c["validate"] = map[string]any{"pattern": `^[A-Za-z]+$`}
	result := ExtractKV("Name: Test123", c)
	if vmap(result, t)["valid"] != false || !containsSubstr(verrs(result, t), "does not match pattern") {
		t.Errorf("pattern fail = %v", result)
	}
	pass := ExtractKV("Name: Alice", c)
	if vmap(pass, t)["valid"] != true {
		t.Errorf("pattern pass = %v", pass)
	}
	// invalid validation pattern regex -> counted as mismatch
	c2 := cfg("name", "string", `Name:\s*(\S+)`)
	c2["validate"] = map[string]any{"pattern": `[bad`}
	bad := ExtractKV("Name: Alice", c2)
	if vmap(bad, t)["valid"] != false {
		t.Errorf("bad pattern = %v", bad)
	}
	// re.match semantics: pattern must match at position 0
	c3 := cfg("name", "string", `Name:\s*(\S+)`)
	c3["validate"] = map[string]any{"pattern": `lice`}
	mid := ExtractKV("Name: Alice", c3)
	if vmap(mid, t)["valid"] != false {
		t.Errorf("mid-string match must fail re.match semantics: %v", mid)
	}
}

func TestValidationAllowedValues(t *testing.T) {
	c := cfg("mode", "string", `Mode:\s*(\w+)`)
	c["validate"] = map[string]any{"allowed_values": []any{"Normal", "Test", "Production"}}
	result := ExtractKV("Mode: Debug", c)
	if vmap(result, t)["valid"] != false || !containsSubstr(verrs(result, t), "not in allowed values") {
		t.Errorf("allowed fail = %v", result)
	}
	pass := ExtractKV("Mode: Normal", c)
	if vmap(pass, t)["valid"] != true {
		t.Errorf("allowed pass = %v", pass)
	}
	// []string form
	c2 := cfg("mode", "string", `Mode:\s*(\w+)`)
	c2["validate"] = map[string]any{"allowed_values": []string{"Normal"}}
	if vmap(ExtractKV("Mode: Normal", c2), t)["valid"] != true {
		t.Error("[]string allowed")
	}
	// unsupported allowed shape -> not allowed
	c3 := cfg("mode", "string", `Mode:\s*(\w+)`)
	c3["validate"] = map[string]any{"allowed_values": "Normal"}
	if vmap(ExtractKV("Mode: Normal", c3), t)["valid"] != false {
		t.Error("string allowed shape rejected")
	}
}

func TestValidationMultipleFields(t *testing.T) {
	configs := []any{
		func() map[string]any {
			c := cfg("score", "int", `Score:\s*(\d+)`)
			c["validate"] = map[string]any{"min": 0, "max": 2000}
			return c
		}(),
		func() map[string]any {
			c := cfg("level", "int", `Level:\s*(\d+)`)
			c["validate"] = map[string]any{"min": 1, "max": 100}
			return c
		}(),
	}
	result := ExtractKV("Score: 1000\nLevel: 150", configs)
	if result == nil || result["score"] != 1000 || result["level"] != 150 {
		t.Fatalf("values = %v", result)
	}
	if vmap(result, t)["valid"] != false {
		t.Error("level > 100 must fail")
	}
}

func TestValidateInternals(t *testing.T) {
	// non-string field key skipped
	res := validateExtracted(map[string]any{"score": 100}, []map[string]any{{"field": 42}})
	if res["valid"] != true {
		t.Error("non-string field skipped")
	}
	// float type check fails on string value
	res = validateExtracted(map[string]any{"value": "not_a_float"}, []map[string]any{{"field": "value", "type": "float"}})
	if res["valid"] != true && !containsSubstr(res["errors"].([]string), "expected float") {
		t.Errorf("float type = %v", res)
	}
	// float min/max
	res = validateExtracted(map[string]any{"temp": 200.0},
		[]map[string]any{{"field": "temp", "type": "float", "validate": map[string]any{"min": 0.0, "max": 100.0}}})
	if res["valid"] != false || !containsSubstr(res["errors"].([]string), "exceeds max") {
		t.Errorf("float max = %v", res)
	}
	res = validateExtracted(map[string]any{"temp": -5.0},
		[]map[string]any{{"field": "temp", "type": "float", "validate": map[string]any{"min": 0.0}}})
	if res["valid"] != false || !containsSubstr(res["errors"].([]string), "below min") {
		t.Errorf("float min = %v", res)
	}
	// string type check fails on int value
	res = validateExtracted(map[string]any{"name": 42}, []map[string]any{{"field": "name", "type": "string"}})
	if res["valid"] != false || !containsSubstr(res["errors"].([]string), "expected string") {
		t.Errorf("string type = %v", res)
	}
	// int type check fails on string value
	res = validateExtracted(map[string]any{"score": "not_an_int"}, []map[string]any{{"field": "score", "type": "int"}})
	if res["valid"] != false || !containsSubstr(res["errors"].([]string), "expected int") {
		t.Errorf("int type = %v", res)
	}
	// value absent and not required -> skipped
	res = validateExtracted(map[string]any{},
		[]map[string]any{{"field": "score", "type": "int", "required": false, "validate": map[string]any{"min": 0}}})
	if res["valid"] != true {
		t.Error("absent not-required skipped")
	}
	// unknown type falls through
	res = validateExtracted(map[string]any{"flag": true}, []map[string]any{{"field": "flag", "type": "bool"}})
	if res["valid"] != true {
		t.Error("bool type no checks")
	}
	// int64 value passes int check
	res = validateExtracted(map[string]any{"n": int64(5)},
		[]map[string]any{{"field": "n", "type": "int", "validate": map[string]any{"min": 1}}})
	if res["valid"] != true {
		t.Errorf("int64 = %v", res)
	}
}

func TestPyFormattersAndHelpers(t *testing.T) {
	if pyTypeName("x") != "str" || pyTypeName(1) != "int" || pyTypeName(int64(1)) != "int" ||
		pyTypeName(1.5) != "float" || pyTypeName(true) != "bool" || pyTypeName(nil) != "NoneType" {
		t.Error("pyTypeName")
	}
	if pyNum(5) != "5" || pyNum(int64(7)) != "7" || pyNum(2.5) != "2.5" || pyNum("x") != "x" {
		t.Error("pyNum")
	}
	if pyList([]any{"a", 1}) != "['a', 1]" {
		t.Errorf("pyList = %q", pyList([]any{"a", 1}))
	}
	if pyList("notalist") != "notalist" {
		t.Errorf("pyList passthrough = %q", pyList("notalist"))
	}
	// min/max rules with unparseable bounds are ignored
	res := validateExtracted(map[string]any{"n": 5},
		[]map[string]any{{"field": "n", "type": "int", "validate": map[string]any{"min": "x", "max": "y"}}})
	if res["valid"] != true {
		t.Errorf("string bounds ignored = %v", res)
	}
}

func containsSubstr(errs []string, sub string) bool {
	for _, e := range errs {
		if strings.Contains(e, sub) {
			return true
		}
	}
	return false
}
