package sqlite_test

import (
	"context"
	"strings"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
)

func graphicalTarget(id, endpoint string) cp.GraphicalTargetRecord {
	return cp.GraphicalTargetRecord{
		TargetID: id, Endpoint: endpoint, TLSMode: "mtls", CASecretRef: cp.Str("file:/run/secrets/ca.pem"),
		ClientCertSecretRef: cp.Str("env:CLIENT_CERT"), ClientKeySecretRef: cp.Str("file:/run/secrets/client.key"),
		ExpectedServerName: cp.Str(id + ".example"), AllowedVMPatterns: cp.NewStringTuple("prod-*", "shared-??"),
		TenantID: cp.Str("tenant-1"), MinimumRole: "operator", ConnectTimeoutS: 5, HandshakeTimeoutS: 10,
		ReadTimeoutS: 30, WriteTimeoutS: 15, ShutdownTimeoutS: 3, MaxGRPCMessageBytes: 1048576,
		MaxFramebufferWidth: 4096, MaxFramebufferHeight: 2160, MaxRectangles: 1024,
		MaxClipboardBytes: 65536, MaxPixelAllocationBytes: 35389440,
		AllowedCIDRs: cp.NewStringTuple("203.0.113.0/24", "2001:db8::/32"),
		AuditLabels:  cp.NewAuditLabels(cp.AuditLabel{Key: "owner", Value: "compute"}, cp.AuditLabel{Key: "environment", Value: "production"}),
		CreatedAt:    1, UpdatedAt: 2,
	}
}

func TestGraphicalTargetStoreRoundTripReplaceOrderDeleteRollbackAndReopen(t *testing.T) {
	ctx := context.Background()
	e, path := newPlaneWithPath(t)
	tx, _ := e.Begin(ctx)
	s := e.GraphicalTargetStore(tx)
	a := graphicalTarget("target-a", "dns:///a:443")
	b := graphicalTarget("target-b", "dns:///b:443")
	replacement := graphicalTarget("target-b", "dns:///replacement:443")
	for _, rec := range []cp.GraphicalTargetRecord{b, a, replacement} {
		if err := s.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
	}
	got, err := s.List(ctx)
	if err != nil || len(got) != 2 || got[0] != a || got[1] != replacement {
		t.Fatalf("list = %#v, %v", got, err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if err := e.Close(ctx); err != nil {
		t.Fatal(err)
	}

	reopened := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: path})
	if err := reopened.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	readTx, _ := reopened.Begin(ctx)
	got, err = reopened.GraphicalTargetStore(readTx).List(ctx)
	if err != nil || len(got) != 2 || got[1] != replacement {
		t.Fatalf("reopened list = %#v, %v", got, err)
	}
	_ = readTx.Rollback(ctx)

	delTx, _ := reopened.Begin(ctx)
	del := reopened.GraphicalTargetStore(delTx)
	if ok, _ := del.Delete(ctx, "missing"); ok {
		t.Fatal("missing delete returned true")
	}
	if ok, _ := del.Delete(ctx, "target-a"); !ok {
		t.Fatal("existing delete returned false")
	}
	_ = delTx.Rollback(ctx)
	checkTx, _ := reopened.Begin(ctx)
	if rec, _ := reopened.GraphicalTargetStore(checkTx).Get(ctx, "target-a"); rec == nil {
		t.Fatal("rollback persisted delete")
	}
	_ = checkTx.Rollback(ctx)
	_ = reopened.Close(ctx)

	raw := openRaw(t, path)
	defer func() { _ = raw.Close() }()
	var patterns, cidrs, labels string
	if err := raw.QueryRow("SELECT allowed_vm_patterns, allowed_cidrs, audit_labels FROM cp_graphical_targets WHERE target_id = ?", "target-b").Scan(&patterns, &cidrs, &labels); err != nil {
		t.Fatal(err)
	}
	if patterns != `["prod-*","shared-??"]` || cidrs != `["203.0.113.0/24","2001:db8::/32"]` || labels != `[["owner","compute"],["environment","production"]]` {
		t.Fatalf("JSON = %q %q %q", patterns, cidrs, labels)
	}
}

func TestGraphicalTargetStoreStableRedactedDataErrors(t *testing.T) {
	ctx := context.Background()
	tests := []struct {
		field string
		value any
	}{
		{"allowed_vm_patterns", "{"}, {"allowed_vm_patterns", `{"pattern":"prod-*"}`}, {"allowed_vm_patterns", `[1]`},
		{"allowed_cidrs", []byte(`["secret-value"]`)}, {"audit_labels", `[["owner"]]`}, {"audit_labels", `[[1,"compute"]]`},
	}
	for _, tc := range tests {
		t.Run(tc.field, func(t *testing.T) {
			e, path := newPlaneWithPath(t)
			tx, _ := e.Begin(ctx)
			_ = e.GraphicalTargetStore(tx).Put(ctx, graphicalTarget("corrupt", "dns:///safe:443"))
			_ = tx.Commit(ctx)
			raw := openRaw(t, path)
			defer func() { _ = raw.Close() }()
			if _, err := raw.Exec("UPDATE cp_graphical_targets SET "+tc.field+" = ? WHERE target_id = ?", tc.value, "corrupt"); err != nil {
				t.Fatal(err)
			}
			rtx, _ := e.Begin(ctx)
			_, err := e.GraphicalTargetStore(rtx).Get(ctx, "corrupt")
			_ = rtx.Rollback(ctx)
			if err == nil || !cp.IsDataError(err) {
				t.Fatalf("error = %T %v", err, err)
			}
			if !strings.Contains(err.Error(), "graphical target "+`"corrupt"`+" has invalid "+tc.field) || strings.Contains(err.Error(), "secret-value") {
				t.Fatalf("unstable or unredacted error: %v", err)
			}
		})
	}
}

type graphicalForeignTx struct{}

func (graphicalForeignTx) Commit(context.Context) error   { return nil }
func (graphicalForeignTx) Rollback(context.Context) error { return nil }

func TestGraphicalTargetStoreRejectsForeignTransaction(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic")
		}
	}()
	sqlite.New(cp.Config{}).GraphicalTargetStore(graphicalForeignTx{})
}

func TestGraphicalTargetStoreUsesExplicitColumnsAcrossSchemaExtension(t *testing.T) {
	ctx := context.Background()
	e, path := newPlaneWithPath(t)
	raw := openRaw(t, path)
	if _, err := raw.Exec("ALTER TABLE cp_graphical_targets ADD COLUMN future_value TEXT NOT NULL DEFAULT 'future'"); err != nil {
		t.Fatal(err)
	}
	_ = raw.Close()
	tx, _ := e.Begin(ctx)
	rec := graphicalTarget("extended", "dns:///extended:443")
	store := e.GraphicalTargetStore(tx)
	if err := store.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	got, err := store.Get(ctx, rec.TargetID)
	if err != nil || got == nil || *got != rec {
		t.Fatalf("record = %#v, %v", got, err)
	}
	_ = tx.Rollback(ctx)
}
