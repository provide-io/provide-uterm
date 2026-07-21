//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"crypto/tls"
	"net"
	"net/http"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/policy"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

// humanVncServe upgrades the browser WebSocket and proxies RFB via litevirt.
// Overridable in tests so the post-gate success path can be asserted without a
// real ProxyVNC upstream.
var humanVncServe = vnc.ServeHumanRelay

// humanVncDial, when non-nil, replaces dialLitevirtTarget for the human-relay
// route (tests inject a fake ClientConnInterface).
var humanVncDial func(s *Server, w http.ResponseWriter, r *http.Request, target *graphical.Definition) (grpc.ClientConnInterface, string, func() error, bool)

// humanVncRelay is the resolved, pre-upgrade state for GET .../gui/vnc.
type humanVncRelay struct {
	WorkerID      string
	HijackID      string
	SessionID     string // worker session id (same as worker_id)
	LeaseID       string // hijack_id when caller owns the lease; else ""
	PrincipalID   string
	PrincipalRole string
	Target        *graphical.Definition
}

// handleHumanVnc is GET/WS /worker/{worker_id}/hijack/{hijack_id}/gui/vnc —
// the production browser→litevirt human-relay path. All authz/lease/ownership/
// target gates run before WebSocket upgrade so failures return JSON status codes.
func (s *Server) handleHumanVnc(w http.ResponseWriter, r *http.Request) {
	relay, ok := s.resolveHumanVnc(w, r)
	if !ok {
		return
	}

	var (
		cc      grpc.ClientConnInterface
		vmName  string
		closeFn func() error
	)
	if humanVncDial != nil {
		cc, vmName, closeFn, ok = humanVncDial(s, w, r, relay.Target)
	} else {
		var conn *grpc.ClientConn
		conn, vmName, ok = s.dialLitevirtTarget(w, r, relay.Target)
		if ok {
			cc = conn
			closeFn = conn.Close
		}
	}
	if !ok {
		return
	}
	if closeFn != nil {
		defer func() { _ = closeFn() }()
	}

	humanVncServe(
		w, r, cc, vmName,
		&policy.Strict{},
		relay.SessionID, relay.LeaseID,
		relay.PrincipalID, relay.PrincipalRole,
	)
}

// resolveHumanVnc enforces authz, hijack presence, lease ownership, tenant-
// scoped target lookup, and protocol=litevirt. Writes HTTP errors and returns
// ok=false before any WebSocket upgrade attempt.
func (s *Server) resolveHumanVnc(w http.ResponseWriter, r *http.Request) (*humanVncRelay, bool) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return nil, false
	}
	// Same capability gate as GUI inject (session.control.hijack).
	if !s.authorizeHubRoute(w, r, workerID, hubHijack) {
		return nil, false
	}

	p := principalOf(r)
	if p == nil {
		detailError(w, http.StatusUnauthorized, "authentication required")
		return nil, false
	}

	hs, _ := s.deps.Hub.GetRestSession(r.Context(), workerID, hijackID)
	if hs == nil {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return nil, false
	}

	// Ownership: another principal holds the lease → 403. Unbound (legacy/empty)
	// AcquiredBy is allowed through for view-only (leaseID stays empty).
	leaseID := ""
	if hs.AcquiredBy != nil && *hs.AcquiredBy != "" {
		if *hs.AcquiredBy != p.SubjectID {
			bridgeError(w, http.StatusForbidden, "hijack lease not owned by caller")
			return nil, false
		}
		leaseID = hijackID
	}

	targetID := strings.TrimSpace(r.URL.Query().Get("target_id"))
	if targetID == "" {
		detailError(w, http.StatusUnprocessableEntity, "target_id is required for human VNC")
		return nil, false
	}

	tenant := ""
	if p.TenantID != nil {
		tenant = *p.TenantID
	}
	scope, scopeOK := graphical.ScopeForTenant(tenant)
	if !scopeOK {
		detailError(w, http.StatusForbidden, "graphical target access denied")
		return nil, false
	}

	target, err := s.deps.GraphicalTargets.Get(scope, targetID)
	if err != nil {
		graphicalRouteError(w, err)
		return nil, false
	}
	if target == nil {
		detailError(w, http.StatusNotFound, "target not found")
		return nil, false
	}

	protocol := strings.ToLower(strings.TrimSpace(target.Protocol))
	if protocol != graphical.ProtocolLitevirt {
		// memory / rfb (and anything else) are not served by ServeHumanRelay.
		detailError(w, http.StatusNotImplemented, "human VNC requires litevirt protocol; got "+protocol)
		return nil, false
	}

	return &humanVncRelay{
		WorkerID:      workerID,
		HijackID:      hijackID,
		SessionID:     workerID,
		LeaseID:       leaseID,
		PrincipalID:   p.SubjectID,
		PrincipalRole: principalPolicyRole(p),
		Target:        target,
	}, true
}

// principalPolicyRole maps RBAC roles onto the policy.Engine role rank string.
func principalPolicyRole(p *serverauth.Principal) string {
	if p == nil {
		return "viewer"
	}
	if p.Roles.Has("admin") {
		return "admin"
	}
	if p.Roles.Has("operator") {
		return "operator"
	}
	return "viewer"
}

// dialLitevirtTarget dials a registry-backed litevirt gRPC endpoint after the
// same egress-guard + TLS policy used by buildLitevirtSession / handleGUIAttach.
// On failure it writes the HTTP error and returns ok=false.
func (s *Server) dialLitevirtTarget(
	w http.ResponseWriter, r *http.Request, target *graphical.Definition,
) (*grpc.ClientConn, string, bool) {
	endpoint := ""
	if target.Endpoint != nil {
		endpoint = *target.Endpoint
	}
	vmName := ""
	if v, ok := target.Config["vm_name"].(string); ok {
		vmName = v
	}

	host, _, err := net.SplitHostPort(endpoint)
	if err != nil {
		host = endpoint
	}
	// Metadata / cloud-IMDS always blocked; private ranges follow
	// security.block_private_connector_targets (same as GUI attach).
	guard := s.egress
	if guard == nil {
		guard = NewEgressGuard(nil, nil)
	}
	blockPrivate := false
	if s.cfg != nil {
		blockPrivate = s.cfg.Security.BlockPrivateConnectorTargets
	}
	if err := guard.AssertConnectorTargetAllowed(r.Context(), host, blockPrivate); err != nil {
		detailError(w, http.StatusForbidden, "invalid endpoint: "+err.Error())
		return nil, "", false
	}

	var dialOpts grpc.DialOption
	insecureNoTLS := false
	if v, ok := target.Config["insecure_no_tls"].(bool); ok {
		insecureNoTLS = v
	}
	if insecureNoTLS {
		if err := AssertIPAllowed(host, false); err != nil {
			ips, lerr := net.LookupIP(host)
			if lerr != nil || len(ips) == 0 {
				detailError(w, http.StatusForbidden, "insecure_no_tls requires resolvable loopback endpoint")
				return nil, "", false
			}
			for _, ip := range ips {
				if !ip.IsLoopback() {
					detailError(w, http.StatusForbidden, "insecure_no_tls only allowed for loopback endpoints")
					return nil, "", false
				}
			}
		} else if ip := net.ParseIP(strings.Trim(host, "[]")); ip == nil || !ip.IsLoopback() {
			detailError(w, http.StatusForbidden, "insecure_no_tls only allowed for loopback endpoints")
			return nil, "", false
		}
		dialOpts = grpc.WithTransportCredentials(insecure.NewCredentials())
	} else {
		dialOpts = grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{}))
	}
	cc, err := grpc.NewClient(endpoint, dialOpts)
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return nil, "", false
	}
	return cc, vmName, true
}
