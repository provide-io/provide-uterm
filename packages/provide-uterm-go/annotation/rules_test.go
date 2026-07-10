//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import (
	"reflect"
	"testing"
)

func findRule(t *testing.T, ruleID string) DetectionRule {
	t.Helper()
	for _, r := range BuiltinRules {
		if r.RuleID == ruleID {
			return r
		}
	}
	t.Fatalf("rule %q not found in BuiltinRules", ruleID)
	return DetectionRule{}
}

func ruleMatches(t *testing.T, ruleID, text string) bool {
	t.Helper()
	return findRule(t, ruleID).Pattern.FindStringIndex(text) != nil
}

// --- Structural tests -----------------------------------------------------

func TestBuiltinRulesCount(t *testing.T) {
	if len(BuiltinRules) != 20 {
		t.Fatalf("expected 20 rules, got %d", len(BuiltinRules))
	}
}

func TestAllFiveCategoriesPresent(t *testing.T) {
	got := map[string]struct{}{}
	for _, r := range BuiltinRules {
		got[r.Category] = struct{}{}
	}
	want := map[string]struct{}{
		"credentials": {}, "escalation": {}, "destructive": {}, "connections": {}, "lifecycle": {},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("categories mismatch: %v", got)
	}
}

func TestAllRulesHaveBothEventTypes(t *testing.T) {
	want := NewEventTypeSet("read", "send")
	for _, r := range BuiltinRules {
		if !reflect.DeepEqual(r.EventTypes, want) {
			t.Fatalf("rule %s has unexpected event types: %v", r.RuleID, r.EventTypes)
		}
	}
}

func TestRuleIDsAreUnique(t *testing.T) {
	seen := map[string]struct{}{}
	for _, r := range BuiltinRules {
		if _, dup := seen[r.RuleID]; dup {
			t.Fatalf("duplicate rule id: %s", r.RuleID)
		}
		seen[r.RuleID] = struct{}{}
	}
}

// --- Credentials — positive -----------------------------------------------

func TestCredentialPositiveMatches(t *testing.T) {
	cases := []struct{ id, text string }{
		{"cred.aws_access_key", "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"}, // pragma: allowlist secret
		{"cred.aws_access_key", "AKIAIOSFODNN7EXAMPLE"},                          // pragma: allowlist secret
		{"cred.github_token", "ghp_" + repeat("A", 36)},
		{"cred.github_token", "ghs_" + repeat("b", 40)},
		{"cred.generic_secret", "password=hunter2"}, // pragma: allowlist secret
		{"cred.generic_secret", "TOKEN: supersecretvalue123"},
		{"cred.bearer_token", "Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig"},
		{"cred.bearer_token", "Bearer eyJhbGciOiJIUzI1NiJ9.test"},
		{"cred.private_key_header", "-----BEGIN RSA PRIVATE KEY-----"},     // pragma: allowlist secret
		{"cred.private_key_header", "-----BEGIN OPENSSH PRIVATE KEY-----"}, // pragma: allowlist secret
	}
	for _, c := range cases {
		if !ruleMatches(t, c.id, c.text) {
			t.Errorf("%s should match %q", c.id, c.text)
		}
	}
}

func TestNormalTextDoesNotMatchCredentials(t *testing.T) {
	text := "Hello world, this is a normal terminal output line."
	for _, r := range BuiltinRules {
		if r.Category == "credentials" && r.Pattern.FindStringIndex(text) != nil {
			t.Errorf("rule %s falsely matched %q", r.RuleID, text)
		}
	}
}

// --- Escalation / destructive / connections / lifecycle — positive --------

func TestOtherPositiveMatches(t *testing.T) {
	cases := []struct{ id, text string }{
		{"esc.sudo", "sudo apt-get update"},
		{"esc.sudo", "then sudo reboot"},
		{"esc.su_dash", "su -"},
		{"esc.su_dash", "su - root"},
		{"esc.pkexec", "pkexec /usr/bin/gparted"},
		{"esc.pkexec", "pkexec"},
		{"dest.rm_rf", "rm -rf /tmp/build"},
		{"dest.rm_rf", "sudo rm -rf /"},
		{"dest.drop_table", "DROP TABLE users;"},
		{"dest.drop_table", "drop database mydb;"},
		{"dest.kubectl_delete", "kubectl delete pod mypod"},
		{"dest.kubectl_delete", "kubectl delete namespace staging"},
		{"dest.dd_if", "dd if=/dev/urandom of=/dev/sda"},
		{"dest.mkfs", "mkfs.ext4 /dev/sdb1"},
		{"dest.mkfs", "mkfs.vfat /dev/sdc1"},
		{"conn.ssh", "ssh deploy@example.com"},
		{"conn.ssh", "ssh admin@192.168.1.1 -p 2222"},
		{"conn.curl", "curl http://example.com/api"},
		{"conn.curl", "curl https://api.github.com/repos"},
		{"conn.wget", "wget https://releases.ubuntu.com/latest.iso"},
		{"conn.wget", "wget http://example.com/file.tar.gz"},
		{"conn.scp", "scp file.txt user@host:/remote/path/"},
		{"conn.scp", "scp user@host:/etc/passwd ."},
		{"life.exit", "exit"},
		{"life.exit", "exit 1"},
		{"life.shutdown", "shutdown -h now"},
		{"life.shutdown", "shutdown -r 5"},
		{"life.reboot", "reboot"},
		{"life.reboot", "sudo reboot now"},
	}
	for _, c := range cases {
		if !ruleMatches(t, c.id, c.text) {
			t.Errorf("%s should match %q", c.id, c.text)
		}
	}
}

func repeat(s string, n int) string {
	out := make([]byte, 0, len(s)*n)
	for i := 0; i < n; i++ {
		out = append(out, s...)
	}
	return string(out)
}
