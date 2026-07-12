package memory_test

import (
	"context"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/memory"
)

func target(id, endpoint string) cp.GraphicalTargetRecord {
	return cp.GraphicalTargetRecord{
		TargetID: id, Endpoint: endpoint, TLSMode: "mtls",
		CASecretRef: cp.Str("file:/run/secrets/ca.pem"), ClientCertSecretRef: cp.Str("env:CLIENT_CERT"),
		ClientKeySecretRef: cp.Str("file:/run/secrets/client.key"), ExpectedServerName: cp.Str(id + ".example"),
		AllowedVMPatterns: cp.NewStringTuple("prod-*", "shared-??"), TenantID: cp.Str("tenant-1"),
		MinimumRole: "operator", ConnectTimeoutS: 5, HandshakeTimeoutS: 10, ReadTimeoutS: 30,
		WriteTimeoutS: 15, ShutdownTimeoutS: 3, MaxGRPCMessageBytes: 1048576,
		MaxFramebufferWidth: 4096, MaxFramebufferHeight: 2160, MaxRectangles: 1024,
		MaxClipboardBytes: 65536, MaxPixelAllocationBytes: 35389440,
		AllowedCIDRs: cp.NewStringTuple("203.0.113.0/24", "2001:db8::/32"),
		AuditLabels:  cp.NewAuditLabels(cp.AuditLabel{Key: "owner", Value: "compute"}), CreatedAt: 1, UpdatedAt: 2,
	}
}

func TestGraphicalTargetStoreCRUDReplacementOrderingAndIsolation(t *testing.T) {
	ctx := context.Background()
	e := memory.New(cp.Config{})
	tx, _ := e.Begin(ctx)
	store := e.GraphicalTargetStore(tx)
	b := target("target-b", "dns:///b:443")
	a := target("target-a", "dns:///a:443")
	replacement := target("target-b", "dns:///replacement:443")
	if err := store.Put(ctx, b); err != nil {
		t.Fatal(err)
	}
	if err := store.Put(ctx, a); err != nil {
		t.Fatal(err)
	}
	if err := store.Put(ctx, replacement); err != nil {
		t.Fatal(err)
	}
	got, err := store.List(ctx)
	if err != nil || len(got) != 2 || got[0] != a || got[1] != replacement {
		t.Fatalf("list = %#v, %v", got, err)
	}
	patterns := got[0].AllowedVMPatterns.Values()
	patterns[0] = "mutated"
	again, _ := store.Get(ctx, "target-a")
	if again == nil || again.AllowedVMPatterns.Values()[0] != "prod-*" {
		t.Fatal("tuple accessor leaked mutable state")
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}

	delTx, _ := e.Begin(ctx)
	del := e.GraphicalTargetStore(delTx)
	if ok, _ := del.Delete(ctx, "missing"); ok {
		t.Fatal("missing delete returned true")
	}
	if ok, _ := del.Delete(ctx, "target-a"); !ok {
		t.Fatal("existing delete returned false")
	}
	_ = delTx.Rollback(ctx)
	readTx, _ := e.Begin(ctx)
	if rec, _ := e.GraphicalTargetStore(readTx).Get(ctx, "target-a"); rec == nil {
		t.Fatal("rollback persisted delete")
	}
	_ = readTx.Rollback(ctx)
}

func TestGraphicalTargetStoreConflictAndDisjointMerge(t *testing.T) {
	ctx := context.Background()
	e := memory.New(cp.Config{})
	tx1, _ := e.Begin(ctx)
	tx2, _ := e.Begin(ctx)
	_ = e.GraphicalTargetStore(tx1).Put(ctx, target("shared", "dns:///one:443"))
	_ = e.GraphicalTargetStore(tx2).Put(ctx, target("shared", "dns:///two:443"))
	if err := tx1.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if err := tx2.Commit(ctx); !cp.IsConflict(err) {
		t.Fatalf("commit error = %v, want conflict", err)
	}

	tx3, _ := e.Begin(ctx)
	tx4, _ := e.Begin(ctx)
	_ = e.GraphicalTargetStore(tx3).Put(ctx, target("three", "dns:///three:443"))
	_ = e.GraphicalTargetStore(tx4).Put(ctx, target("four", "dns:///four:443"))
	if err := tx3.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if err := tx4.Commit(ctx); err != nil {
		t.Fatalf("disjoint commit: %v", err)
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
	memory.New(cp.Config{}).GraphicalTargetStore(graphicalForeignTx{})
}
