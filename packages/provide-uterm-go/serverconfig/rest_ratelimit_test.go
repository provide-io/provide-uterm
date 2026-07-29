//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// restRateKeys are the two top-level config keys that carry a REST hijack
// token-bucket rate. Every refusal below must hold for both, so each case is
// run once per key rather than written twice.
var restRateKeys = []string{"rest_acquire_rate_limit_per_sec", "rest_send_rate_limit_per_sec"}

// TestRestRateLimitDefaults pins the defaults a deployment that sets neither
// key gets: the hub's own built-in rates, so an unset config is unchanged.
func TestRestRateLimitDefaults(t *testing.T) {
	for name, c := range map[string]*UtermServerConfig{
		"DefaultServerConfig": DefaultServerConfig(),
		"empty mapping":       mustConfig(t, map[string]any{}),
	} {
		if c.RestAcquireRateLimitPerSec != 5 {
			t.Errorf("%s: rest_acquire_rate_limit_per_sec = %v, want 5", name, c.RestAcquireRateLimitPerSec)
		}
		if c.RestSendRateLimitPerSec != 20 {
			t.Errorf("%s: rest_send_rate_limit_per_sec = %v, want 20", name, c.RestSendRateLimitPerSec)
		}
	}
}

// TestRestRateLimitParsed pins that each key lands on its own field, from both
// a float and an integer TOML literal.
func TestRestRateLimitParsed(t *testing.T) {
	c := mustConfig(t, map[string]any{
		"rest_acquire_rate_limit_per_sec": 1.5,
		"rest_send_rate_limit_per_sec":    int64(50),
	})
	if c.RestAcquireRateLimitPerSec != 1.5 {
		t.Errorf("rest_acquire_rate_limit_per_sec = %v, want 1.5", c.RestAcquireRateLimitPerSec)
	}
	if c.RestSendRateLimitPerSec != 50 {
		t.Errorf("rest_send_rate_limit_per_sec = %v, want 50", c.RestSendRateLimitPerSec)
	}
}

// TestRestRateLimitKeysAccepted pins that the keys are known top-level config
// (extra="forbid" parity would otherwise refuse them outright).
func TestRestRateLimitKeysAccepted(t *testing.T) {
	for _, key := range restRateKeys {
		if _, err := ConfigFromMapping(map[string]any{key: 3.0}); err != nil {
			t.Errorf("%s should be an accepted top-level key: %v", key, err)
		}
	}
}

// TestRestRateLimitRefusesUnhonourableRates covers every value the limiter
// cannot honour verbatim. Each is refused at load — a server that boots with a
// nonsense limit and discovers it at first use is a server running unprotected.
func TestRestRateLimitRefusesUnhonourableRates(t *testing.T) {
	for _, tc := range []struct {
		name  string
		value any
		want  string
		why   string
	}{
		{
			"zero", 0.0, "got: 0.0",
			"read as unlimited it silently disables the limit; read as refuse-everything " +
				"it silently bricks the REST hijack API, and nothing says which was meant",
		},
		{
			"zero_int", int64(0), "got: 0.0",
			"a bare 0 in TOML decodes as an integer and must be refused the same way",
		},
		{
			"negative", -1.0, "got: -1.0",
			"same ambiguity as zero, with no reading under which it is a policy",
		},
		{
			"below_floor", 0.05, "got: 0.05",
			"the limiter clamps to the floor, so accepting this would hand back a " +
				"looser rate than the operator wrote",
		},
		{
			"just_below_floor", 0.99, "got: 0.99",
			"burst is one second of the rate, so a sub-1/sec bucket never holds the whole " +
				"token a call costs — it refuses everything forever, the same silent bricking 0 is refused for",
		},
		{
			"tenth", 0.1, "got: 0.1",
			"one call every ten seconds sounds like a policy but is unimplementable by a " +
				"bucket whose burst equals its rate; it admits nothing at all",
		},
		{
			"nan", math.NaN(), "got: nan",
			"NaN compares false against everything, so the check is written as `not >=` " +
				"to refuse it rather than let it slide through a `<` test",
		},
		{
			"positive_infinity", math.Inf(1), "got: inf",
			"an unbounded limit is the same silent disabling as 0, and `not value >= MIN` " +
				"does not catch it — inf compares true against everything",
		},
		{
			"negative_infinity", math.Inf(-1), "got: -inf",
			"not a rate under any reading",
		},
	} {
		for _, key := range restRateKeys {
			t.Run(tc.name+"/"+key, func(t *testing.T) {
				_, err := ConfigFromMapping(map[string]any{key: tc.value})
				if err == nil {
					t.Fatalf("%s = %v accepted; want refusal (%s)", key, tc.value, tc.why)
				}
				if !strings.Contains(err.Error(), key) {
					t.Errorf("error must name the offending key %q, got: %v", key, err)
				}
				if !strings.Contains(err.Error(), "must be >= 1.0") {
					t.Errorf("error must state the floor, got: %v", err)
				}
				if !strings.Contains(err.Error(), tc.want) {
					t.Errorf("error must echo the offending value (%s), got: %v", tc.want, err)
				}
			})
		}
	}
}

// TestRestRateLimitAcceptsFloorAndAbove pins that the floor itself is a real
// policy — one call per second is the tightest a bucket whose burst equals its
// rate can honour — as is any rate above it, fractional or not.
func TestRestRateLimitAcceptsFloorAndAbove(t *testing.T) {
	for _, value := range []float64{1, 1.5, 5, 20, 1000} {
		for _, key := range restRateKeys {
			if _, err := ConfigFromMapping(map[string]any{key: value}); err != nil {
				t.Errorf("%s = %v should be accepted: %v", key, value, err)
			}
		}
	}
}

// TestRestRateLimitRefusedAtLoad pins that the refusal arrives when the TOML
// file is loaded — the boot path — not at first use. A server that boots with
// a nonsense limit and discovers it later is a server running unprotected.
func TestRestRateLimitRefusedAtLoad(t *testing.T) {
	for _, literal := range []string{"0", "-1", "0.05", "0.99", "nan", "inf", "-inf"} {
		path := filepath.Join(t.TempDir(), "server.toml")
		body := "rest_acquire_rate_limit_per_sec = " + literal + "\n"
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatalf("write config: %v", err)
		}
		_, err := LoadServerConfig(path)
		if err == nil {
			t.Errorf("rest_acquire_rate_limit_per_sec = %s loaded; want refusal", literal)
			continue
		}
		if !strings.Contains(err.Error(), "rest_acquire_rate_limit_per_sec must be >= 1.0") {
			t.Errorf("%s: unexpected error: %v", literal, err)
		}
	}
}

// TestPyFloatMatchesPythonRepr pins the value rendering inside a refusal, so
// the same bad config reads identically from either port.
func TestPyFloatMatchesPythonRepr(t *testing.T) {
	for _, tc := range []struct {
		value float64
		want  string
	}{
		{0, "0.0"},
		{-1, "-1.0"},
		{0.05, "0.05"},
		{0.1, "0.1"},
		{1, "1.0"},
		{20, "20.0"},
		{math.NaN(), "nan"},
		{math.Inf(1), "inf"},
		{math.Inf(-1), "-inf"},
		{1e21, "1e+21"},
	} {
		if got := pyFloat(tc.value); got != tc.want {
			t.Errorf("pyFloat(%v) = %q, want %q", tc.value, got, tc.want)
		}
	}
}
