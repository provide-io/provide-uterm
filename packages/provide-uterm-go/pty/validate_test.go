//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"strings"
	"testing"
)

func TestValidateCommand(t *testing.T) {
	cases := []struct {
		name, cmd, wantErr string
	}{
		{"ok", "/bin/echo", ""},
		{"empty", "", "must not be empty"},
		{"null", "/bin/e\x00cho", "null byte"},
		{"too-long", "/" + strings.Repeat("a", maxPathLen), "too long"},
		{"relative", "echo", "absolute path"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateCommand(tc.cmd)
			assertErr(t, err, tc.wantErr)
		})
	}
}

func TestValidateUsername(t *testing.T) {
	cases := []struct {
		name, user, wantErr string
	}{
		{"ok", "alice.b-1_", ""},
		{"empty", "", "must not be empty"},
		{"null", "al\x00ice", "null byte"},
		{"too-long", strings.Repeat("a", maxUsernameLen+1), "too long"},
		{"bad-char", "al ice", "invalid character"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assertErr(t, ValidateUsername(tc.user), tc.wantErr)
		})
	}
}

func TestValidateServiceName(t *testing.T) {
	cases := []struct {
		name, svc, wantErr string
	}{
		{"ok", "provide-uterm", ""},
		{"empty", "", "must not be empty"},
		{"null", "svc\x00", "null byte"},
		{"too-long", strings.Repeat("s", maxServiceLen+1), "too long"},
		{"bad-char", "svc name", "invalid character"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assertErr(t, ValidateServiceName(tc.svc), tc.wantErr)
		})
	}
}

func TestValidateEnv(t *testing.T) {
	if err := ValidateEnv(map[string]string{"A": "1", "B": "2"}); err != nil {
		t.Fatalf("valid env: %v", err)
	}
	assertErr(t, ValidateEnv(map[string]string{"A=B": "x"}), "must not contain '='")
	assertErr(t, ValidateEnv(map[string]string{"A\x00": "x"}), "null byte")
	assertErr(t, ValidateEnv(map[string]string{"A": "x\x00"}), "null byte")
	assertErr(t, ValidateEnv(map[string]string{"A": strings.Repeat("v", maxEnvValueLen+1)}), "too long")

	big := make(map[string]string, maxEnvKeys+1)
	for i := 0; i <= maxEnvKeys; i++ {
		big[strings.Repeat("k", 1)+itoa(i)] = "v"
	}
	assertErr(t, ValidateEnv(big), "too many keys")
}

// assertErr checks err matches want ("" = expect nil).
func assertErr(t *testing.T, err error, want string) {
	t.Helper()
	if want == "" {
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		return
	}
	if err == nil {
		t.Fatalf("expected error containing %q, got nil", want)
	}
	if !strings.Contains(err.Error(), want) {
		t.Fatalf("error %q does not contain %q", err.Error(), want)
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b []byte
	neg := i < 0
	if neg {
		i = -i
	}
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	if neg {
		b = append([]byte{'-'}, b...)
	}
	return string(b)
}
