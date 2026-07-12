//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite_test

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
)

// goldenExpected mirrors testdata/golden_py.json (produced by the Python
// backend). Field tags match the Python dataclass field names so the same JSON
// validates both the Python-created fixture and a Go-created DB.
type goldenExpected struct {
	Sessions         []goldenSession         `json:"sessions"`
	SessionTokens    []goldenSessionToken    `json:"session_tokens"`
	ResumeTokens     []goldenResumeToken     `json:"resume_tokens"`
	Approvals        []goldenApproval        `json:"approvals"`
	Leases           []goldenLease           `json:"leases"`
	GraphicalTargets []goldenGraphicalTarget `json:"graphical_targets,omitempty"`
	AuditHead        goldenAudit             `json:"audit_head"`
}

type goldenGraphicalTarget struct {
	TargetID                string     `json:"target_id"`
	Endpoint                string     `json:"endpoint"`
	TLSMode                 string     `json:"tls_mode"`
	CASecretRef             *string    `json:"ca_secret_ref"`
	ClientCertSecretRef     *string    `json:"client_cert_secret_ref"`
	ClientKeySecretRef      *string    `json:"client_key_secret_ref"`
	ExpectedServerName      *string    `json:"expected_server_name"`
	AllowedVMPatterns       []string   `json:"allowed_vm_patterns"`
	TenantID                *string    `json:"tenant_id"`
	MinimumRole             string     `json:"minimum_role"`
	ConnectTimeoutS         float64    `json:"connect_timeout_s"`
	HandshakeTimeoutS       float64    `json:"handshake_timeout_s"`
	ReadTimeoutS            float64    `json:"read_timeout_s"`
	WriteTimeoutS           float64    `json:"write_timeout_s"`
	ShutdownTimeoutS        float64    `json:"shutdown_timeout_s"`
	MaxGRPCMessageBytes     int64      `json:"max_grpc_message_bytes"`
	MaxFramebufferWidth     int64      `json:"max_framebuffer_width"`
	MaxFramebufferHeight    int64      `json:"max_framebuffer_height"`
	MaxRectangles           int64      `json:"max_rectangles"`
	MaxClipboardBytes       int64      `json:"max_clipboard_bytes"`
	MaxPixelAllocationBytes int64      `json:"max_pixel_allocation_bytes"`
	AllowedCIDRs            []string   `json:"allowed_cidrs"`
	AuditLabels             [][]string `json:"audit_labels"`
	CreatedAt               float64    `json:"created_at"`
	UpdatedAt               float64    `json:"updated_at"`
}

type goldenSession struct {
	SessionID      string   `json:"session_id"`
	DisplayName    string   `json:"display_name"`
	ConnectorType  string   `json:"connector_type"`
	Owner          *string  `json:"owner"`
	Visibility     string   `json:"visibility"`
	LifecycleState string   `json:"lifecycle_state"`
	CreatedAt      float64  `json:"created_at"`
	UpdatedAt      float64  `json:"updated_at"`
	DeletedAt      *float64 `json:"deleted_at"`
}

type goldenSessionToken struct {
	SessionID  string   `json:"session_id"`
	TokenKind  string   `json:"token_kind"`
	TokenValue string   `json:"token_value"`
	CreatedAt  float64  `json:"created_at"`
	ExpiresAt  *float64 `json:"expires_at"`
	RevokedAt  *float64 `json:"revoked_at"`
}

type goldenResumeToken struct {
	TokenValue     string   `json:"token_value"`
	SessionID      string   `json:"session_id"`
	Role           string   `json:"role"`
	CreatedAt      float64  `json:"created_at"`
	ExpiresAt      float64  `json:"expires_at"`
	WasHijackOwner bool     `json:"was_hijack_owner"`
	RevokedAt      *float64 `json:"revoked_at"`
}

type goldenApproval struct {
	ApprovalID  string   `json:"approval_id"`
	SessionID   string   `json:"session_id"`
	Command     string   `json:"command"`
	RequestedBy *string  `json:"requested_by"`
	State       string   `json:"state"`
	CreatedAt   float64  `json:"created_at"`
	ResolvedAt  *float64 `json:"resolved_at"`
	ResolvedBy  *string  `json:"resolved_by"`
}

type goldenLease struct {
	SessionID      string   `json:"session_id"`
	HijackID       string   `json:"hijack_id"`
	Owner          string   `json:"owner"`
	LeaseExpiresAt float64  `json:"lease_expires_at"`
	CreatedAt      float64  `json:"created_at"`
	DeletedAt      *float64 `json:"deleted_at"`
}

type goldenAudit struct {
	Seq        int64  `json:"seq"`
	RecordHash string `json:"record_hash"`
}

func nullStr(p *string) cp.NullString {
	if p == nil {
		return cp.NullStr()
	}
	return cp.Str(*p)
}

func nullFloat(p *float64) cp.NullFloat {
	if p == nil {
		return cp.NullFlt()
	}
	return cp.Float(*p)
}

// readAll loads every record the golden fixtures describe out of a DB via the Go
// engine and returns it as a goldenExpected for comparison.
func readAll(t *testing.T, path string, want goldenExpected) goldenExpected {
	t.Helper()
	ctx := context.Background()
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: path})
	if err := e.Open(ctx); err != nil {
		t.Fatalf("open: %v", err)
	}
	defer func() { _ = e.Close(ctx) }()
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}

	var got goldenExpected
	for _, s := range want.Sessions {
		rec, err := e.SessionStore(tx).Get(ctx, s.SessionID)
		if err != nil || rec == nil {
			t.Fatalf("session %s: %v", s.SessionID, err)
		}
		got.Sessions = append(got.Sessions, goldenSession{
			SessionID: rec.SessionID, DisplayName: rec.DisplayName, ConnectorType: rec.ConnectorType,
			Owner: ptrStr(rec.Owner), Visibility: rec.Visibility, LifecycleState: rec.LifecycleState,
			CreatedAt: rec.CreatedAt, UpdatedAt: rec.UpdatedAt, DeletedAt: ptrFloat(rec.DeletedAt),
		})
	}
	for _, st := range want.SessionTokens {
		rec, err := e.TokenStore(tx).GetSessionToken(ctx, st.SessionID, st.TokenKind)
		if err != nil || rec == nil {
			t.Fatalf("session token %s/%s: %v", st.SessionID, st.TokenKind, err)
		}
		got.SessionTokens = append(got.SessionTokens, goldenSessionToken{
			SessionID: rec.SessionID, TokenKind: rec.TokenKind, TokenValue: rec.TokenValue,
			CreatedAt: rec.CreatedAt, ExpiresAt: ptrFloat(rec.ExpiresAt), RevokedAt: ptrFloat(rec.RevokedAt),
		})
	}
	for _, rt := range want.ResumeTokens {
		rec, err := e.TokenStore(tx).GetResumeToken(ctx, rt.TokenValue)
		if err != nil || rec == nil {
			t.Fatalf("resume token %s: %v", rt.TokenValue, err)
		}
		got.ResumeTokens = append(got.ResumeTokens, goldenResumeToken{
			TokenValue: rec.TokenValue, SessionID: rec.SessionID, Role: rec.Role, CreatedAt: rec.CreatedAt,
			ExpiresAt: rec.ExpiresAt, WasHijackOwner: rec.WasHijackOwner, RevokedAt: ptrFloat(rec.RevokedAt),
		})
	}
	for _, a := range want.Approvals {
		rec, err := e.ApprovalStore(tx).GetApproval(ctx, a.ApprovalID)
		if err != nil || rec == nil {
			t.Fatalf("approval %s: %v", a.ApprovalID, err)
		}
		got.Approvals = append(got.Approvals, goldenApproval{
			ApprovalID: rec.ApprovalID, SessionID: rec.SessionID, Command: rec.Command,
			RequestedBy: ptrStr(rec.RequestedBy), State: rec.State, CreatedAt: rec.CreatedAt,
			ResolvedAt: ptrFloat(rec.ResolvedAt), ResolvedBy: ptrStr(rec.ResolvedBy),
		})
	}
	for _, l := range want.Leases {
		rec, err := e.LeaseStore(tx).GetLease(ctx, l.SessionID)
		if err != nil || rec == nil {
			t.Fatalf("lease %s: %v", l.SessionID, err)
		}
		got.Leases = append(got.Leases, goldenLease{
			SessionID: rec.SessionID, HijackID: rec.HijackID, Owner: rec.Owner,
			LeaseExpiresAt: rec.LeaseExpiresAt, CreatedAt: rec.CreatedAt, DeletedAt: ptrFloat(rec.DeletedAt),
		})
	}
	for _, target := range want.GraphicalTargets {
		rec, err := e.GraphicalTargetStore(tx).Get(ctx, target.TargetID)
		if err != nil || rec == nil {
			t.Fatalf("graphical target %s: %v", target.TargetID, err)
		}
		labels := make([][]string, 0, len(rec.AuditLabels.Values()))
		for _, label := range rec.AuditLabels.Values() {
			labels = append(labels, []string{label.Key, label.Value})
		}
		got.GraphicalTargets = append(got.GraphicalTargets, goldenGraphicalTarget{
			TargetID: rec.TargetID, Endpoint: rec.Endpoint, TLSMode: rec.TLSMode,
			CASecretRef: ptrStr(rec.CASecretRef), ClientCertSecretRef: ptrStr(rec.ClientCertSecretRef),
			ClientKeySecretRef: ptrStr(rec.ClientKeySecretRef), ExpectedServerName: ptrStr(rec.ExpectedServerName),
			AllowedVMPatterns: rec.AllowedVMPatterns.Values(), TenantID: ptrStr(rec.TenantID), MinimumRole: rec.MinimumRole,
			ConnectTimeoutS: rec.ConnectTimeoutS, HandshakeTimeoutS: rec.HandshakeTimeoutS,
			ReadTimeoutS: rec.ReadTimeoutS, WriteTimeoutS: rec.WriteTimeoutS, ShutdownTimeoutS: rec.ShutdownTimeoutS,
			MaxGRPCMessageBytes: rec.MaxGRPCMessageBytes, MaxFramebufferWidth: rec.MaxFramebufferWidth,
			MaxFramebufferHeight: rec.MaxFramebufferHeight, MaxRectangles: rec.MaxRectangles,
			MaxClipboardBytes: rec.MaxClipboardBytes, MaxPixelAllocationBytes: rec.MaxPixelAllocationBytes,
			AllowedCIDRs: rec.AllowedCIDRs.Values(), AuditLabels: labels, CreatedAt: rec.CreatedAt, UpdatedAt: rec.UpdatedAt,
		})
	}
	// The audit-head accessor takes the engine tx-lock, so the read transaction
	// must be closed first.
	if err := tx.Rollback(ctx); err != nil {
		t.Fatalf("rollback: %v", err)
	}
	head, err := e.GetAuditHead(ctx)
	if err != nil || head == nil {
		t.Fatalf("audit head: %v", err)
	}
	got.AuditHead = goldenAudit{Seq: head.Seq, RecordHash: head.RecordHash}
	return got
}

func ptrStr(n cp.NullString) *string {
	if !n.Valid {
		return nil
	}
	v := n.String
	return &v
}

func ptrFloat(n cp.NullFloat) *float64 {
	if !n.Valid {
		return nil
	}
	v := n.Float64
	return &v
}

// TestCrossCompatPythonToGo reads the committed Python-created golden DB with the
// Go engine and asserts it round-trips to the committed expected JSON. This runs
// in CI with no Python dependency.
func TestCrossCompatPythonToGo(t *testing.T) {
	t.Parallel()
	want := loadGolden(t)
	// Copy the committed fixture to a temp file first: the Go engine opens file
	// DBs in WAL mode (creating -wal/-shm sidecars), which must not touch the
	// checked-in fixture and would otherwise contend for its lock.
	dbCopy := filepath.Join(t.TempDir(), "golden_py.sqlite")
	src, err := os.ReadFile("testdata/golden_py.sqlite")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	if err := os.WriteFile(dbCopy, src, 0o600); err != nil {
		t.Fatalf("copy fixture: %v", err)
	}
	got := readAll(t, dbCopy, want)
	assertGoldenEqual(t, want, got)
}

// TestCrossCompatPythonV2ForwardToGoV3 proves a Python-created pre-target
// database can be migrated by Go and then use the shared v3 target schema.
func TestCrossCompatPythonV2ForwardToGoV3(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	dbCopy := filepath.Join(t.TempDir(), "golden_py.sqlite")
	src, err := os.ReadFile("testdata/golden_py.sqlite")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dbCopy, src, 0o600); err != nil {
		t.Fatal(err)
	}
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: dbCopy})
	if err := e.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	rec := cp.GraphicalTargetRecord{
		TargetID: "cross-target", Endpoint: "dns:///cross.example:443", TLSMode: "mtls",
		AllowedVMPatterns: cp.NewStringTuple("prod-*"), MinimumRole: "operator",
		AllowedCIDRs: cp.NewStringTuple("203.0.113.0/24"), AuditLabels: cp.NewAuditLabels(),
	}
	if err := e.GraphicalTargetStore(tx).Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	readTx, _ := e.Begin(ctx)
	got, err := e.GraphicalTargetStore(readTx).Get(ctx, rec.TargetID)
	if err != nil || got == nil || *got != rec {
		t.Fatalf("target = %#v, %v", got, err)
	}
	_ = readTx.Rollback(ctx)
	_ = e.Close(ctx)
}

// TestCrossCompatGoToPython builds a DB with the Go engine that matches the
// golden expectations, then validates it via the Python sqlite engine. It skips
// when Python/uv is unavailable so CI without Python still passes.
func TestCrossCompatGoToPython(t *testing.T) {
	want := loadGolden(t)
	want.GraphicalTargets = []goldenGraphicalTarget{crossCompatGraphicalTarget()}
	repoRoot := findRepoRoot(t)
	if _, err := exec.LookPath("uv"); err != nil {
		t.Skip("uv not available; skipping live Go->Python cross-compat check")
	}
	validator := filepath.Join(repoRoot, "packages", "provide-uterm-go", "controlplane", "sqlite",
		"testdata", "validate_cross.py")
	if _, err := os.Stat(validator); err != nil {
		t.Skipf("validator script missing: %v", err)
	}

	dbPath := filepath.Join(t.TempDir(), "go_created.db")
	buildGoDB(t, dbPath, want)

	expectedJSON := filepath.Join(t.TempDir(), "expected.json")
	expectedData, _ := json.Marshal(want)
	if err := os.WriteFile(expectedJSON, expectedData, 0o600); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("uv", "run", "python", validator, dbPath, expectedJSON)
	cmd.Dir = repoRoot
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("python validation of Go-created DB failed: %v\n%s", err, out)
	}
	if string(out) == "" || !contains(string(out), "CROSS_COMPAT_OK") {
		t.Fatalf("unexpected validator output: %s", out)
	}
}

func TestCrossCompatPythonV3TargetToGo(t *testing.T) {
	want := loadGolden(t)
	want.GraphicalTargets = []goldenGraphicalTarget{crossCompatGraphicalTarget()}
	repoRoot := findRepoRoot(t)
	if _, err := exec.LookPath("uv"); err != nil {
		t.Skip("uv not available")
	}
	dbPath := filepath.Join(t.TempDir(), "python_v3.db")
	expectedPath := filepath.Join(t.TempDir(), "expected.json")
	b, _ := json.Marshal(want)
	if err := os.WriteFile(expectedPath, b, 0o600); err != nil {
		t.Fatal(err)
	}
	validator := filepath.Join(repoRoot, "packages", "provide-uterm-go", "controlplane", "sqlite", "testdata", "validate_cross.py")
	cmd := exec.Command("uv", "run", "python", validator, "--create", dbPath, expectedPath)
	cmd.Dir = repoRoot
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("create Python v3 DB: %v\n%s", err, out)
	}
	ctx := context.Background()
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: dbPath})
	if err := e.Open(ctx); err != nil {
		t.Fatal(err)
	}
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	got, err := e.GraphicalTargetStore(tx).Get(ctx, want.GraphicalTargets[0].TargetID)
	if err != nil || got == nil {
		t.Fatalf("read Python target: %#v, %v", got, err)
	}
	wantRecord := graphicalTargetRecord(want.GraphicalTargets[0])
	if *got != wantRecord {
		t.Fatalf("Python target mismatch:\n want %#v\n  got %#v", wantRecord, *got)
	}
	_ = tx.Rollback(ctx)
	_ = e.Close(ctx)
}

func graphicalTargetRecord(target goldenGraphicalTarget) cp.GraphicalTargetRecord {
	labels := make([]cp.AuditLabel, 0, len(target.AuditLabels))
	for _, pair := range target.AuditLabels {
		labels = append(labels, cp.AuditLabel{Key: pair[0], Value: pair[1]})
	}
	return cp.GraphicalTargetRecord{
		TargetID: target.TargetID, Endpoint: target.Endpoint, TLSMode: target.TLSMode,
		CASecretRef: nullStr(target.CASecretRef), ClientCertSecretRef: nullStr(target.ClientCertSecretRef),
		ClientKeySecretRef: nullStr(target.ClientKeySecretRef), ExpectedServerName: nullStr(target.ExpectedServerName),
		AllowedVMPatterns: cp.NewStringTuple(target.AllowedVMPatterns...), TenantID: nullStr(target.TenantID),
		MinimumRole: target.MinimumRole, ConnectTimeoutS: target.ConnectTimeoutS,
		HandshakeTimeoutS: target.HandshakeTimeoutS, ReadTimeoutS: target.ReadTimeoutS,
		WriteTimeoutS: target.WriteTimeoutS, ShutdownTimeoutS: target.ShutdownTimeoutS,
		MaxGRPCMessageBytes: target.MaxGRPCMessageBytes, MaxFramebufferWidth: target.MaxFramebufferWidth,
		MaxFramebufferHeight: target.MaxFramebufferHeight, MaxRectangles: target.MaxRectangles,
		MaxClipboardBytes: target.MaxClipboardBytes, MaxPixelAllocationBytes: target.MaxPixelAllocationBytes,
		AllowedCIDRs: cp.NewStringTuple(target.AllowedCIDRs...), AuditLabels: cp.NewAuditLabels(labels...),
		CreatedAt: target.CreatedAt, UpdatedAt: target.UpdatedAt,
	}
}

func crossCompatGraphicalTarget() goldenGraphicalTarget {
	serverName := "tärgét.example"
	certRef := "env:CLIENT_CERT"
	return goldenGraphicalTarget{
		TargetID: "target-unicode", Endpoint: "dns:///tärgét.example:443", TLSMode: "mtls",
		CASecretRef: nil, ClientCertSecretRef: &certRef, ClientKeySecretRef: nil,
		ExpectedServerName: &serverName, AllowedVMPatterns: []string{}, TenantID: nil,
		MinimumRole: "opérateur", ConnectTimeoutS: 1, HandshakeTimeoutS: 2, ReadTimeoutS: 3,
		WriteTimeoutS: 4, ShutdownTimeoutS: 5, MaxGRPCMessageBytes: 1024,
		MaxFramebufferWidth: 800, MaxFramebufferHeight: 600, MaxRectangles: 32,
		MaxClipboardBytes: 64, MaxPixelAllocationBytes: 1920000,
		AllowedCIDRs: []string{"2001:db8::/32"}, AuditLabels: [][]string{{"équipe", "calcul"}},
		CreatedAt: 11, UpdatedAt: 12,
	}
}

// buildGoDB creates a DB with the Go engine populated to match want.
func buildGoDB(t *testing.T, path string, want goldenExpected) {
	t.Helper()
	ctx := context.Background()
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: path})
	e.SetClock(func() float64 { return 0 })
	if err := e.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	tx, _ := e.Begin(ctx)
	for _, s := range want.Sessions {
		_ = e.SessionStore(tx).Upsert(ctx, cp.SessionRecord{
			SessionID: s.SessionID, DisplayName: s.DisplayName, ConnectorType: s.ConnectorType,
			Owner: nullStr(s.Owner), Visibility: s.Visibility, LifecycleState: s.LifecycleState,
			CreatedAt: s.CreatedAt, UpdatedAt: s.UpdatedAt, DeletedAt: nullFloat(s.DeletedAt),
		})
	}
	for _, st := range want.SessionTokens {
		_ = e.TokenStore(tx).PutSessionToken(ctx, cp.SessionTokenRecord{
			SessionID: st.SessionID, TokenKind: st.TokenKind, TokenValue: st.TokenValue,
			CreatedAt: st.CreatedAt, ExpiresAt: nullFloat(st.ExpiresAt), RevokedAt: nullFloat(st.RevokedAt),
		})
	}
	for _, rt := range want.ResumeTokens {
		_ = e.TokenStore(tx).CreateResumeToken(ctx, cp.ResumeTokenRecord{
			TokenValue: rt.TokenValue, SessionID: rt.SessionID, Role: rt.Role, CreatedAt: rt.CreatedAt,
			ExpiresAt: rt.ExpiresAt, WasHijackOwner: rt.WasHijackOwner, RevokedAt: nullFloat(rt.RevokedAt),
		})
	}
	for _, a := range want.Approvals {
		_ = e.ApprovalStore(tx).PutApproval(ctx, cp.ApprovalRecord{
			ApprovalID: a.ApprovalID, SessionID: a.SessionID, Command: a.Command, RequestedBy: nullStr(a.RequestedBy),
			State: a.State, CreatedAt: a.CreatedAt, ResolvedAt: nullFloat(a.ResolvedAt), ResolvedBy: nullStr(a.ResolvedBy),
		})
	}
	for _, l := range want.Leases {
		_ = e.LeaseStore(tx).PutLease(ctx, cp.LeaseRecord{
			SessionID: l.SessionID, HijackID: l.HijackID, Owner: l.Owner,
			LeaseExpiresAt: l.LeaseExpiresAt, CreatedAt: l.CreatedAt, DeletedAt: nullFloat(l.DeletedAt),
		})
	}
	for _, target := range want.GraphicalTargets {
		labels := make([]cp.AuditLabel, 0, len(target.AuditLabels))
		for _, pair := range target.AuditLabels {
			labels = append(labels, cp.AuditLabel{Key: pair[0], Value: pair[1]})
		}
		_ = e.GraphicalTargetStore(tx).Put(ctx, cp.GraphicalTargetRecord{
			TargetID: target.TargetID, Endpoint: target.Endpoint, TLSMode: target.TLSMode,
			CASecretRef: nullStr(target.CASecretRef), ClientCertSecretRef: nullStr(target.ClientCertSecretRef),
			ClientKeySecretRef: nullStr(target.ClientKeySecretRef), ExpectedServerName: nullStr(target.ExpectedServerName),
			AllowedVMPatterns: cp.NewStringTuple(target.AllowedVMPatterns...), TenantID: nullStr(target.TenantID),
			MinimumRole: target.MinimumRole, ConnectTimeoutS: target.ConnectTimeoutS,
			HandshakeTimeoutS: target.HandshakeTimeoutS, ReadTimeoutS: target.ReadTimeoutS,
			WriteTimeoutS: target.WriteTimeoutS, ShutdownTimeoutS: target.ShutdownTimeoutS,
			MaxGRPCMessageBytes: target.MaxGRPCMessageBytes, MaxFramebufferWidth: target.MaxFramebufferWidth,
			MaxFramebufferHeight: target.MaxFramebufferHeight, MaxRectangles: target.MaxRectangles,
			MaxClipboardBytes: target.MaxClipboardBytes, MaxPixelAllocationBytes: target.MaxPixelAllocationBytes,
			AllowedCIDRs: cp.NewStringTuple(target.AllowedCIDRs...), AuditLabels: cp.NewAuditLabels(labels...),
			CreatedAt: target.CreatedAt, UpdatedAt: target.UpdatedAt,
		})
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if err := e.SetAuditHead(ctx, want.AuditHead.Seq, want.AuditHead.RecordHash); err != nil {
		t.Fatal(err)
	}
	_ = e.Close(ctx)
}

func loadGolden(t *testing.T) goldenExpected {
	t.Helper()
	data, err := os.ReadFile("testdata/golden_py.json")
	if err != nil {
		t.Fatalf("read golden json: %v", err)
	}
	var want goldenExpected
	if err := json.Unmarshal(data, &want); err != nil {
		t.Fatalf("unmarshal golden json: %v", err)
	}
	return want
}

func assertGoldenEqual(t *testing.T, want, got goldenExpected) {
	t.Helper()
	wj, _ := json.Marshal(want)
	gj, _ := json.Marshal(got)
	if string(wj) != string(gj) {
		t.Fatalf("cross-compat mismatch:\n want %s\n  got %s", wj, gj)
	}
}

func findRepoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, ".git")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Skip("repo root not found; skipping live cross-compat check")
		}
		dir = parent
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
