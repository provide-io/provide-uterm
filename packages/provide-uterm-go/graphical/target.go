//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package graphical is a Go port of the C# graphical-target model + registry
// (packages/provide-uterm-csharp/src/Provide.Uterm/Server/GraphicalTargets.cs).
//
// A GraphicalTargetDefinition describes a remote graphical console (memory or
// rfb). Definitions live in a tenant-scoped Registry: every read/write is
// gated by a Scope derived from the authenticated principal's tenant, never
// from client input. PublicCopy strips secrets from any value crossing the
// REST boundary. The wire shape (snake_case JSON keys, error codes, validation
// and endpoint-parsing rules) mirrors the C# canonical source byte-for-byte.
package graphical

import (
	"fmt"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Protocol constants (GraphicalTargetConstants.Protocol*).
const (
	ProtocolMemory   = "memory"
	ProtocolRfb      = "rfb"
	ProtocolLitevirt = "litevirt"
)

// Error code strings surfaced in the REST {"detail":{"code":...}} envelope
// (GraphicalTargetConstants.Error*).
const (
	ErrInvalidPayload   = "graphical_target_invalid"
	ErrAlreadyExists    = "graphical_target_exists"
	ErrNotFound         = "graphical_target_not_found"
	ErrImmutable        = "graphical_target_immutable"
	ErrConflict         = "graphical_target_conflict"
	ErrUnavailable      = "graphical_target_unavailable"
	ErrBackend          = "graphical_target_backend_error"
	ErrTenantManaged    = "tenant_managed"
	ErrTargetIDMismatch = "target_id_mismatch"
)

// supportedProtocols mirrors GraphicalTargetConstants.SupportedProtocols.
var supportedProtocols = map[string]struct{}{ProtocolMemory: {}, ProtocolRfb: {}, ProtocolLitevirt: {}}

// namePattern ports GraphicalTargetModels.GraphicalNamePattern (also the tenant
// name pattern). secretRefPattern ports GraphicalTargetModels.SecretRefPattern.
var (
	namePattern      = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)
	secretRefPattern = regexp.MustCompile(`^(?:env:[A-Za-z_][A-Za-z0-9_]*|file:/[^\x00]+)$`)
)

// PayloadKeys ports GraphicalTargetModels.GraphicalTargetPayloadKeys — the
// accepted create/update body keys.
var PayloadKeys = map[string]struct{}{
	"tenant_id": {}, "target_id": {}, "display_name": {}, "protocol": {},
	"endpoint": {}, "secret": {}, "width": {}, "height": {},
	"ca_secret_ref": {}, "client_cert_secret_ref": {}, "client_key_secret_ref": {},
	"is_system": {}, "is_static": {}, "config": {},
}

// ErrorCode ports GraphicalTargetErrorCode.
type ErrorCode int

// Error codes (GraphicalTargetErrorCode enum).
const (
	CodeAlreadyExists ErrorCode = iota
	CodeNotFound
	CodeImmutable
	CodeForbidden
	CodeConflict
	CodeInvalid
	CodeClosed
	CodeBackend
)

// Error ports GraphicalTargetException — a coded registry/validation error.
type Error struct {
	Code    ErrorCode
	Message string
}

func (e *Error) Error() string { return e.Message }

func newError(code ErrorCode, msg string) *Error { return &Error{Code: code, Message: msg} }

// Definition ports GraphicalTargetDefinition. Nullable fields are pointers so
// their JSON is omitted when unset (System.Text.Json WhenWritingNull parity).
type Definition struct {
	TargetID            string  `json:"target_id"`
	TenantID            string  `json:"tenant_id"`
	DisplayName         string  `json:"display_name"`
	Protocol            string  `json:"protocol"`
	Endpoint            *string `json:"endpoint,omitempty"`
	Secret              *string `json:"secret,omitempty"`
	Width               int     `json:"width"`
	Height              int     `json:"height"`
	IsSystem            bool    `json:"is_system"`
	IsStatic            bool    `json:"is_static"`
	CaSecretRef         *string `json:"ca_secret_ref,omitempty"`
	ClientCertSecretRef *string `json:"client_cert_secret_ref,omitempty"`
	ClientKeySecretRef  *string `json:"client_key_secret_ref,omitempty"`
	// Config carries generic per-target, protocol-specific parameters (JSON key
	// "config") — e.g. the litevirt vm_name. It is NOT a secret, so it survives
	// both Clone and PublicCopy. Empty is omitted from the wire (Go's nullable
	// convention) where C# emits {}; a documented cosmetic deviation.
	Config    map[string]any `json:"config,omitempty"`
	CreatedBy *string        `json:"created_by,omitempty"`
	CreatedAt time.Time      `json:"created_at"`
	UpdatedBy *string        `json:"updated_by,omitempty"`
	UpdatedAt *time.Time     `json:"updated_at,omitempty"`
}

// NewDefinition returns a definition with the same defaults as the C# model
// (rfb protocol, 640x480).
func NewDefinition() *Definition {
	return &Definition{Protocol: ProtocolRfb, Width: 640, Height: 480, Config: map[string]any{}}
}

// Clone ports GraphicalTargetDefinition.Clone — a deep copy. Pointer fields are
// re-allocated so a stored value never aliases a caller's copy.
func (d *Definition) Clone() *Definition {
	c := *d
	c.Endpoint = clonePtr(d.Endpoint)
	c.Secret = clonePtr(d.Secret)
	c.CaSecretRef = clonePtr(d.CaSecretRef)
	c.ClientCertSecretRef = clonePtr(d.ClientCertSecretRef)
	c.ClientKeySecretRef = clonePtr(d.ClientKeySecretRef)
	c.CreatedBy = clonePtr(d.CreatedBy)
	c.Config = cloneConfig(d.Config)
	if d.UpdatedAt != nil {
		t := *d.UpdatedAt
		c.UpdatedAt = &t
	}
	return &c
}

// cloneConfig copies the protocol-specific config map so a stored value never
// aliases a caller's copy (mirrors C# new Dictionary<>(Config)). A nil source
// yields an empty (non-nil) map.
func cloneConfig(src map[string]any) map[string]any {
	out := make(map[string]any, len(src))
	for k, v := range src {
		out[k] = v
	}
	return out
}

// PublicCopy ports GraphicalTargetDefinition.PublicCopy — a clone with every
// secret (secret + the three secret refs) stripped for the REST boundary.
func (d *Definition) PublicCopy() *Definition {
	c := d.Clone()
	c.Secret = nil              // pragma: allowlist secret
	c.CaSecretRef = nil         // pragma: allowlist secret
	c.ClientCertSecretRef = nil // pragma: allowlist secret
	c.ClientKeySecretRef = nil  // pragma: allowlist secret
	return c
}

// Validate ports GraphicalTargetDefinition.Validate. It normalizes Protocol and
// Endpoint in place and returns an *Error (CodeInvalid) on any violation.
func (d *Definition) Validate() error {
	if !namePattern.MatchString(d.TargetID) {
		return newError(CodeInvalid, "target_id must be a safe identifier")
	}

	protocol := strings.ToLower(strings.TrimSpace(d.Protocol))
	if _, ok := supportedProtocols[protocol]; !ok {
		return newError(CodeInvalid, "unsupported protocol")
	}
	d.Protocol = protocol

	switch protocol {
	case ProtocolRfb:
		host, port, err := ParseRfbEndpoint(d.Endpoint)
		if err != nil {
			return err
		}
		ep := fmt.Sprintf("%s:%d", host, port)
		d.Endpoint = &ep
	case ProtocolLitevirt:
		// A litevirt endpoint is a plain host:port gRPC target (no rfb:// scheme).
		host, port, err := ParseLitevirtEndpoint(d.Endpoint)
		if err != nil {
			return err
		}
		ep := fmt.Sprintf("%s:%d", host, port)
		d.Endpoint = &ep
	}

	if d.Width < 1 || d.Width > 8192 {
		return newError(CodeInvalid, "width out of range")
	}
	if d.Height < 1 || d.Height > 8192 {
		return newError(CodeInvalid, "height out of range")
	}

	if strings.TrimSpace(d.TenantID) != "" && !namePattern.MatchString(d.TenantID) {
		return newError(CodeInvalid, "tenant_id is invalid")
	}

	for _, ref := range []*string{d.CaSecretRef, d.ClientCertSecretRef, d.ClientKeySecretRef} {
		if ref != nil && !secretRefPattern.MatchString(*ref) {
			return newError(CodeInvalid, "invalid secret reference syntax")
		}
	}
	return nil
}

// ParseRfbEndpoint ports GraphicalTargetParsing.ParseRfbEndpoint: accept
// host:port, rfb://host:port, or a dns:/// prefix; require a 1..65535 port.
func ParseRfbEndpoint(rawEndpoint *string) (string, int, error) {
	raw := ""
	if rawEndpoint != nil {
		raw = *rawEndpoint
	}
	if strings.TrimSpace(raw) == "" {
		return "", 0, newError(CodeInvalid, "endpoint is required for protocol rfb")
	}

	endpoint := strings.TrimSpace(raw)
	if strings.HasPrefix(strings.ToLower(endpoint), "dns:///") {
		endpoint = endpoint[len("dns:///"):]
	}

	if !strings.HasPrefix(strings.ToLower(endpoint), "rfb://") {
		if !strings.Contains(endpoint, ":") {
			return "", 0, newError(CodeInvalid, "invalid endpoint; expected host:port or rfb://host:port")
		}
		endpoint = "rfb://" + endpoint
	}

	u, err := url.Parse(endpoint)
	if err != nil || u.Hostname() == "" {
		return "", 0, newError(CodeInvalid, "invalid endpoint; expected host:port or rfb://host:port")
	}

	portStr := u.Port()
	if portStr == "" {
		return "", 0, newError(CodeInvalid, "invalid endpoint port")
	}
	port, err := strconv.Atoi(portStr)
	if err != nil || port < 1 || port > 65535 {
		return "", 0, newError(CodeInvalid, "invalid endpoint port")
	}
	return u.Hostname(), port, nil
}

// ParseLitevirtEndpoint ports GraphicalTargetParsing.ParseLitevirtEndpoint.
// Unlike rfb, a litevirt gRPC endpoint carries no wire scheme — it is a plain
// host:port target (optionally prefixed with dns:///). Require it non-empty and
// shaped like host:port with a valid 1..65535 port; the scheme wrapper is used
// only to lean on net/url's host:port parsing and is discarded.
func ParseLitevirtEndpoint(rawEndpoint *string) (string, int, error) {
	raw := ""
	if rawEndpoint != nil {
		raw = *rawEndpoint
	}
	if strings.TrimSpace(raw) == "" {
		return "", 0, newError(CodeInvalid, "endpoint is required for protocol litevirt")
	}

	endpoint := strings.TrimSpace(raw)
	if strings.HasPrefix(strings.ToLower(endpoint), "dns:///") {
		endpoint = endpoint[len("dns:///"):]
	}

	u, err := url.Parse("grpc://" + endpoint)
	if err != nil || u.Hostname() == "" {
		return "", 0, newError(CodeInvalid, "invalid endpoint; expected host:port")
	}

	portStr := u.Port()
	if portStr == "" {
		return "", 0, newError(CodeInvalid, "invalid endpoint port")
	}
	port, err := strconv.Atoi(portStr)
	if err != nil || port < 1 || port > 65535 {
		return "", 0, newError(CodeInvalid, "invalid endpoint port")
	}
	return u.Hostname(), port, nil
}

func clonePtr(s *string) *string {
	if s == nil {
		return nil
	}
	v := *s
	return &v
}
