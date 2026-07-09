//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package defaults

import (
	"os"
	"path/filepath"
	"testing"
)

func TestConstantValues(t *testing.T) {
	stringConsts := []struct {
		name string
		got  string
		want string
	}{
		{"TelnetHost", TelnetHost, "127.0.0.1"},
		{"BindAll", BindAll, "0.0.0.0"},
		{"ProxyWSPath", ProxyWSPath, "/ws/terminal"},
		{"ServerHost", ServerHost, "127.0.0.1"},
	}
	for _, c := range stringConsts {
		if c.got != c.want {
			t.Errorf("%s = %q, want %q", c.name, c.got, c.want)
		}
	}

	intConsts := []struct {
		name string
		got  int
		want int
	}{
		{"TelnetPort", TelnetPort, 2102},
		{"SSHPort", SSHPort, 2222},
		{"GatewayTelnetPort", GatewayTelnetPort, 2112},
		{"GatewaySSHPort", GatewaySSHPort, 2222},
		{"ProxyPort", ProxyPort, 8765},
		{"ServerPort", ServerPort, 8780},
		{"TelnetRemotePort", TelnetRemotePort, 23},
		{"SSHRemotePort", SSHRemotePort, 22},
		{"WSPingInterval", WSPingInterval, 20},
		{"WSPingTimeout", WSPingTimeout, 20},
		{"WSCloseTimeout", WSCloseTimeout, 10},
		{"ReconnectMaxRetries", ReconnectMaxRetries, 5},
	}
	for _, c := range intConsts {
		if c.got != c.want {
			t.Errorf("%s = %d, want %d", c.name, c.got, c.want)
		}
	}

	if ReconnectBaseBackoffS != 0.5 {
		t.Errorf("ReconnectBaseBackoffS = %v, want 0.5", ReconnectBaseBackoffS)
	}
	if ReconnectMaxBackoffS != 30.0 {
		t.Errorf("ReconnectMaxBackoffS = %v, want 30.0", ReconnectMaxBackoffS)
	}
}

func TestTokenFile(t *testing.T) {
	got, err := TokenFile()
	if err != nil {
		t.Fatalf("TokenFile() error: %v", err)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatalf("UserHomeDir() error: %v", err)
	}
	want := filepath.Join(home, ".uterm", "session_token")
	if got != want {
		t.Errorf("TokenFile() = %q, want %q", got, want)
	}
}

func TestTokenFileNoHome(t *testing.T) {
	// os.UserHomeDir fails when $HOME is empty on Unix.
	t.Setenv("HOME", "")
	if _, err := TokenFile(); err == nil {
		t.Error("TokenFile() expected an error when $HOME is unset")
	}
}
