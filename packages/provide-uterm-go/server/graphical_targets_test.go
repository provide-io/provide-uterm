// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package server

import (
	"context"
	"errors"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/memory"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func graphicalTarget(id string, tenant *string) serverconfig.GraphicalTargetDefinition {
	return serverconfig.GraphicalTargetDefinition{TargetID: id, Endpoint: "dns:///" + id + ".example:443", TLSMode: "tls", AllowedVMPatterns: []string{"*"}, TenantID: tenant, MinimumRole: "viewer", ConnectTimeoutS: 10, HandshakeTimeoutS: 10, ReadTimeoutS: 30, WriteTimeoutS: 30, ShutdownTimeoutS: 5, MaxGRPCMessageBytes: 16 << 20, MaxFramebufferWidth: 8192, MaxFramebufferHeight: 8192, MaxRectangles: 4096, MaxClipboardBytes: 1 << 20, MaxPixelAllocationBytes: 256 << 20}
}

func TestGraphicalRegistryMergeScopeCRUDAndStaticPrecedence(t *testing.T) {
	ctx := context.Background()
	engine := memory.New(cp.Config{})
	if err := engine.Open(ctx); err != nil {
		t.Fatal(err)
	}
	one := "one"
	static := graphicalTarget("static", &one)
	registry, err := NewGraphicalTargetRegistry([]serverconfig.GraphicalTargetDefinition{static}, engine, true)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = registry.Close(context.Background()) })
	tenant, _ := NewTenantTargetScope("one")
	other, _ := NewTenantTargetScope("two")
	system := SystemTargetScope()
	runtime := graphicalTarget("runtime", &one)
	if _, err = registry.Create(ctx, tenant, runtime); err != nil {
		t.Fatal(err)
	}
	got, err := registry.List(ctx, system)
	if err != nil || len(got) != 2 || got[0].TargetID != "runtime" || got[1].TargetID != "static" {
		t.Fatalf("merge: %#v %v", got, err)
	}
	if got, _ := registry.Get(ctx, other, "runtime"); got != nil {
		t.Fatal("cross tenant read")
	}
	if _, err := registry.Create(ctx, system, static); !errors.Is(err, ErrGraphicalTargetAlreadyExists) {
		t.Fatalf("static create: %v", err)
	}
	if _, err := registry.Update(ctx, system, static); !errors.Is(err, ErrGraphicalTargetImmutable) {
		t.Fatalf("static update: %v", err)
	}
	if err := registry.Delete(ctx, system, "static"); !errors.Is(err, ErrGraphicalTargetImmutable) {
		t.Fatalf("static delete: %v", err)
	}
}

func TestGraphicalRegistryCopiesValuesAndCloses(t *testing.T) {
	ctx := context.Background()
	engine := memory.New(cp.Config{})
	_ = engine.Open(ctx)
	target := graphicalTarget("safe", nil)
	registry, err := NewGraphicalTargetRegistry([]serverconfig.GraphicalTargetDefinition{target}, engine, false)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := registry.Get(ctx, SystemTargetScope(), "safe")
	got.AllowedVMPatterns[0] = "mutated"
	again, _ := registry.Get(ctx, SystemTargetScope(), "safe")
	if again.AllowedVMPatterns[0] != "*" {
		t.Fatal("static value alias")
	}
	if err := registry.Close(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.List(ctx, SystemTargetScope()); !errors.Is(err, ErrGraphicalTargetClosed) {
		t.Fatalf("post close: %v", err)
	}
}
