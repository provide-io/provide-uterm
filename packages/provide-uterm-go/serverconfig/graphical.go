// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package serverconfig

import (
	"fmt"
	"net/netip"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

var (
	graphicalNameRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)
	vmPatternRE     = regexp.MustCompile(`^[A-Za-z0-9_*?.:-]{1,256}$`)
	labelRE         = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`)
	dnsLabelRE      = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$`)
	envNameRE       = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
)

type GraphicalConfig struct {
	AllowDynamicTargets bool     `json:"allow_dynamic_targets" toml:"allow_dynamic_targets"`
	DynamicAllowedCIDRs []string `json:"dynamic_allowed_cidrs" toml:"dynamic_allowed_cidrs"`
}

type GraphicalTargetDefinition struct {
	TargetID                string            `json:"target_id" toml:"target_id"`
	Endpoint                string            `json:"endpoint" toml:"endpoint"`
	TLSMode                 string            `json:"tls_mode" toml:"tls_mode"`
	CASecretRef             *string           `json:"ca_secret_ref" toml:"ca_secret_ref"`
	ClientCertSecretRef     *string           `json:"client_cert_secret_ref" toml:"client_cert_secret_ref"`
	ClientKeySecretRef      *string           `json:"client_key_secret_ref" toml:"client_key_secret_ref"`
	ExpectedServerName      *string           `json:"expected_server_name" toml:"expected_server_name"`
	AllowedVMPatterns       []string          `json:"allowed_vm_patterns" toml:"allowed_vm_patterns"`
	TenantID                *string           `json:"tenant_id" toml:"tenant_id"`
	MinimumRole             string            `json:"minimum_role" toml:"minimum_role"`
	ConnectTimeoutS         float64           `json:"connect_timeout_s" toml:"connect_timeout_s"`
	HandshakeTimeoutS       float64           `json:"handshake_timeout_s" toml:"handshake_timeout_s"`
	ReadTimeoutS            float64           `json:"read_timeout_s" toml:"read_timeout_s"`
	WriteTimeoutS           float64           `json:"write_timeout_s" toml:"write_timeout_s"`
	ShutdownTimeoutS        float64           `json:"shutdown_timeout_s" toml:"shutdown_timeout_s"`
	MaxGRPCMessageBytes     int64             `json:"max_grpc_message_bytes" toml:"max_grpc_message_bytes"`
	MaxFramebufferWidth     int64             `json:"max_framebuffer_width" toml:"max_framebuffer_width"`
	MaxFramebufferHeight    int64             `json:"max_framebuffer_height" toml:"max_framebuffer_height"`
	MaxRectangles           int64             `json:"max_rectangles" toml:"max_rectangles"`
	MaxClipboardBytes       int64             `json:"max_clipboard_bytes" toml:"max_clipboard_bytes"`
	MaxPixelAllocationBytes int64             `json:"max_pixel_allocation_bytes" toml:"max_pixel_allocation_bytes"`
	AllowedCIDRs            []string          `json:"allowed_cidrs" toml:"allowed_cidrs"`
	AuditLabels             map[string]string `json:"audit_labels" toml:"audit_labels"`
}

func defaultGraphicalTarget() GraphicalTargetDefinition {
	return GraphicalTargetDefinition{TLSMode: "tls", AllowedVMPatterns: []string{"*"}, MinimumRole: "viewer", ConnectTimeoutS: 10, HandshakeTimeoutS: 10, ReadTimeoutS: 30, WriteTimeoutS: 30, ShutdownTimeoutS: 5, MaxGRPCMessageBytes: 16 << 20, MaxFramebufferWidth: 8192, MaxFramebufferHeight: 8192, MaxRectangles: 4096, MaxClipboardBytes: 1 << 20, MaxPixelAllocationBytes: 256 << 20, AllowedCIDRs: []string{}, AuditLabels: map[string]string{}}
}

// Validate applies the strict graphical target schema and canonicalizes
// deduplicated tuple fields in place.
func (t *GraphicalTargetDefinition) Validate() error { return t.validate() }

func validateIdentity(value string) error {
	if value == "" || len(value) > 253 {
		return fmt.Errorf("network identity must be an ASCII DNS name or IP address")
	}
	for _, r := range value {
		if r > 127 {
			return fmt.Errorf("network identity must be an ASCII DNS name or IP address")
		}
	}
	if _, err := netip.ParseAddr(value); err == nil {
		return nil
	}
	for _, label := range strings.Split(value, ".") {
		if !dnsLabelRE.MatchString(label) {
			return fmt.Errorf("network identity must be a valid DNS name or IP address")
		}
	}
	return nil
}

func validateEndpoint(value string) error {
	u, err := url.Parse(value)
	if err != nil || !strings.HasPrefix(value, "dns:///") || u.Scheme != "dns" || u.Host != "" || u.RawQuery != "" || u.Fragment != "" {
		return fmt.Errorf("endpoint must use dns:///host:port syntax")
	}
	address := strings.TrimPrefix(value, "dns:///")
	if strings.ContainsAny(address, "/@") {
		return fmt.Errorf("endpoint must include a valid host and port")
	}
	var host, port string
	if strings.HasPrefix(address, "[") {
		i := strings.Index(address, "]")
		if i < 0 || i+1 >= len(address) || address[i+1] != ':' {
			return fmt.Errorf("endpoint must include a valid host and port")
		}
		host, port = address[1:i], address[i+2:]
		a, e := netip.ParseAddr(host)
		if e != nil || !a.Is6() {
			return fmt.Errorf("endpoint contains an invalid IPv6 literal")
		}
	} else {
		if strings.Count(address, ":") != 1 {
			return fmt.Errorf("endpoint must bracket IPv6 literals")
		}
		host, port, _ = strings.Cut(address, ":")
	}
	if err := validateIdentity(host); err != nil {
		return err
	}
	p, e := strconv.Atoi(port)
	if e != nil || p < 1 || p > 65535 {
		return fmt.Errorf("endpoint must include a valid host and port")
	}
	return nil
}

func canonicalCIDRs(values []string) ([]string, error) {
	out := make([]string, 0, len(values))
	seen := map[string]bool{}
	for _, v := range values {
		p, e := netip.ParsePrefix(v)
		if e != nil || p.String() != v {
			return nil, fmt.Errorf("allowed_cidrs must contain canonical networks")
		}
		if !seen[v] {
			seen[v] = true
			out = append(out, v)
		}
	}
	return out, nil
}
func validSecretRef(v *string) bool {
	if v == nil {
		return true
	}
	s := *v
	if strings.HasPrefix(s, "env:") {
		return envNameRE.MatchString(strings.TrimPrefix(s, "env:"))
	}
	if strings.HasPrefix(s, "file:") {
		x := strings.TrimPrefix(s, "file:")
		return x != "" && !strings.Contains(x, "\x00") && (strings.HasPrefix(x, "/") || !strings.Contains("/"+x+"/", "/../"))
	}
	return false
}

func (t *GraphicalTargetDefinition) validate() error {
	if !graphicalNameRE.MatchString(t.TargetID) {
		return fmt.Errorf("target_id must be a safe identifier")
	}
	if err := validateEndpoint(t.Endpoint); err != nil {
		return err
	}
	if !inSet(t.TLSMode, "disabled", "tls", "mtls") {
		return literalError("tls_mode", "disabled", "tls", "mtls")
	}
	if t.TenantID != nil && !graphicalNameRE.MatchString(*t.TenantID) {
		return fmt.Errorf("tenant_id must be a safe identifier")
	}
	if !inSet(t.MinimumRole, "viewer", "operator", "admin") {
		return fmt.Errorf("minimum_role must be viewer, operator, or admin")
	}
	if t.ExpectedServerName != nil {
		if err := validateIdentity(*t.ExpectedServerName); err != nil {
			return err
		}
	}
	for _, r := range []*string{t.CASecretRef, t.ClientCertSecretRef, t.ClientKeySecretRef} {
		if !validSecretRef(r) {
			return fmt.Errorf("invalid secret reference syntax")
		}
	}
	anyClient := t.ClientCertSecretRef != nil || t.ClientKeySecretRef != nil              // pragma: allowlist secret
	pair := t.ClientCertSecretRef != nil && t.ClientKeySecretRef != nil                   // pragma: allowlist secret
	if t.TLSMode == "disabled" && (t.CASecretRef != nil || t.ExpectedServerName != nil) { // pragma: allowlist secret
		return fmt.Errorf("disabled TLS may not specify CA or server name")
	}
	if t.TLSMode != "mtls" && anyClient {
		return fmt.Errorf("client certificate references require mtls")
	}
	if t.TLSMode == "mtls" && !pair {
		return fmt.Errorf("mtls requires both client certificate and key references")
	}
	if len(t.AllowedVMPatterns) == 0 {
		return fmt.Errorf("allowed_vm_patterns must contain safe glob patterns")
	}
	seen := map[string]bool{}
	patterns := t.AllowedVMPatterns[:0]
	for _, v := range t.AllowedVMPatterns {
		if !vmPatternRE.MatchString(v) {
			return fmt.Errorf("allowed_vm_patterns must contain safe glob patterns")
		}
		if !seen[v] {
			seen[v] = true
			patterns = append(patterns, v)
		}
	}
	t.AllowedVMPatterns = patterns
	for _, v := range []float64{t.ConnectTimeoutS, t.HandshakeTimeoutS, t.ReadTimeoutS, t.WriteTimeoutS, t.ShutdownTimeoutS} {
		if v <= 0 {
			return fmt.Errorf("value must be positive")
		}
	}
	for _, v := range []int64{t.MaxGRPCMessageBytes, t.MaxFramebufferWidth, t.MaxFramebufferHeight, t.MaxRectangles, t.MaxClipboardBytes, t.MaxPixelAllocationBytes} {
		if v <= 0 {
			return fmt.Errorf("value must be positive")
		}
	}
	var err error
	t.AllowedCIDRs, err = canonicalCIDRs(t.AllowedCIDRs)
	if err != nil {
		return err
	}
	for k, v := range t.AuditLabels {
		if !labelRE.MatchString(k) || len(v) > 256 {
			return fmt.Errorf("audit_labels contain an invalid label")
		}
	}
	return nil
}
