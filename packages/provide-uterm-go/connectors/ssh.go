//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"fmt"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// sshConfigKeys mirrors SshSessionConnector._VALID_CONFIG_KEYS (minus the
// asyncssh-specific client_keys list form, which has no Go analogue).
var sshConfigKeys = keySet(
	"host", "port", "username", "password",
	"client_key", "client_key_data", "client_key_path",
	"known_hosts", "insecure_no_host_check", "input_mode",
	"hub_overlay", "block_private_connector_targets",
)

// sshKeyPEM extracts a PEM private key from client_key / client_key_data.
func sshKeyPEM(config map[string]any) ([]byte, error) {
	if config["client_key_path"] != nil { // pragma: allowlist secret
		return nil, fmt.Errorf("ssh connector_config.client_key_path is not supported")
	}
	if v := configStr(config, "client_key", ""); v != "" {
		return []byte(v), nil
	}
	switch v := config["client_key_data"].(type) {
	case []byte:
		return v, nil
	case string:
		if v != "" {
			return []byte(v), nil
		}
	}
	return nil, nil
}

// newSSH builds an SSH connector over the SSH transport. Host-key policy mirrors
// the Python connector: known_hosts is required unless insecure_no_host_check is
// set.
func newSSH(sessionID, displayName string, config map[string]any) (*transportConnector, error) {
	if err := validateKeys(config, "ssh", sshConfigKeys); err != nil {
		return nil, err
	}
	host := configStr(config, "host", defaults.TelnetHost)
	port := configInt(config, "port", defaults.SSHRemotePort)
	user := configStr(config, "username", "guest")
	password := configStr(config, "password", "")
	knownHosts := configStr(config, "known_hosts", "")
	insecure := configBool(config, "insecure_no_host_check", false)
	inputMode := configStr(config, "input_mode", "open")

	if knownHosts == "" && !insecure {
		return nil, fmt.Errorf(
			"ssh connector requires known_hosts for session %q connecting to %s; "+
				"set connector_config.known_hosts to a known_hosts file path, "+
				"or set insecure_no_host_check=true to disable host key verification",
			sessionID, host,
		)
	}
	keyPEM, err := sshKeyPEM(config)
	if err != nil {
		return nil, err
	}

	sshOpts := transports.SSHOptions{
		User:                      user,
		Password:                  password,
		InsecureSkipHostKeyVerify: insecure,
	}
	if knownHosts != "" {
		sshOpts.KnownHostsFiles = []string{knownHosts}
	}
	if len(keyPEM) > 0 {
		sshOpts.Key = transports.SSHKeyAuth{PrivateKeyPEM: keyPEM}
	}

	build := func() *termsession.TransportSession {
		tr := transports.NewSSHTransport()
		connect := func(ctx context.Context) error {
			return tr.Connect(ctx, host, port, transports.ConnectOptions{SSH: sshOpts})
		}
		return termsession.New(tr, connect, termsession.Options{SendEncoding: termsession.EncodingUTF8})
	}
	upstream := fmt.Sprintf("ssh://%s@%s:%d", user, host, port)
	return newTransportConnector(sessionID, displayName, "ssh", upstream, inputMode, build), nil
}
