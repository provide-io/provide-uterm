// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package server

import (
	"context"
	"errors"
	"runtime"
	"sort"
	"sync"
	"time"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

var (
	ErrGraphicalTargetAlreadyExists = errors.New("graphical target already exists")
	ErrGraphicalTargetNotFound      = errors.New("graphical target not found")
	ErrGraphicalTargetImmutable     = errors.New("static graphical target is immutable")
	ErrGraphicalTargetForbidden     = errors.New("graphical target tenant scope denied")
	ErrGraphicalTargetTransaction   = errors.New("graphical target transaction conflicted")
	ErrGraphicalTargetClosed        = errors.New("graphical target registry is closed")
)

type TargetScope struct {
	tenant string
	system bool
}

func NewTenantTargetScope(tenant string) (TargetScope, error) {
	if tenant == "" {
		return TargetScope{}, errors.New("tenant scope requires exactly one tenant_id")
	}
	return TargetScope{tenant: tenant}, nil
}
func SystemTargetScope() TargetScope { return TargetScope{system: true} }
func (s TargetScope) valid() bool    { return s.system != (s.tenant != "") }
func (s TargetScope) permits(tenant *string) bool {
	if !s.valid() {
		return false
	}
	if s.system {
		return true
	}
	return tenant != nil && *tenant == s.tenant
}

type GraphicalTargetRegistry struct {
	static       map[string]serverconfig.GraphicalTargetDefinition
	engine       cp.Engine
	owns         bool
	mu           sync.Mutex
	active       int
	closing      bool
	closed       bool
	drain        chan struct{}
	closeAttempt *graphicalCloseAttempt
}

type graphicalCloseAttempt struct {
	done    chan struct{}
	err     error
	waiters int
}

func NewGraphicalTargetRegistry(static []serverconfig.GraphicalTargetDefinition, engine cp.Engine, owns bool) (*GraphicalTargetRegistry, error) {
	if engine == nil {
		return nil, errors.New("graphical target control plane is required")
	}
	r := &GraphicalTargetRegistry{static: make(map[string]serverconfig.GraphicalTargetDefinition, len(static)), engine: engine, owns: owns, drain: make(chan struct{})}
	for _, target := range static {
		if err := target.Validate(); err != nil {
			return nil, errors.New("invalid static graphical target")
		}
		if _, ok := r.static[target.TargetID]; ok {
			return nil, errors.New("duplicate graphical target_id")
		}
		r.static[target.TargetID] = cloneTarget(target)
	}
	return r, nil
}

func cloneString(v *string) *string {
	if v == nil {
		return nil
	}
	x := *v
	return &x
}
func cloneTarget(t serverconfig.GraphicalTargetDefinition) serverconfig.GraphicalTargetDefinition {
	t.CASecretRef = cloneString(t.CASecretRef)
	t.ClientCertSecretRef = cloneString(t.ClientCertSecretRef)
	t.ClientKeySecretRef = cloneString(t.ClientKeySecretRef)
	t.ExpectedServerName = cloneString(t.ExpectedServerName)
	t.TenantID = cloneString(t.TenantID)
	t.AllowedVMPatterns = append([]string(nil), t.AllowedVMPatterns...)
	t.AllowedCIDRs = append([]string(nil), t.AllowedCIDRs...)
	labels := make(map[string]string, len(t.AuditLabels))
	for k, v := range t.AuditLabels {
		labels[k] = v
	}
	t.AuditLabels = labels
	return t
}

func (r *GraphicalTargetRegistry) enter() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing || r.closed {
		return ErrGraphicalTargetClosed
	}
	r.active++
	return nil
}
func (r *GraphicalTargetRegistry) leave() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.active--
	if r.closing && r.active == 0 {
		select {
		case <-r.drain:
		default:
			close(r.drain)
		}
	}
}
func (r *GraphicalTargetRegistry) scoped(scope TargetScope, tenant *string) error {
	if !scope.valid() || !scope.permits(tenant) {
		return ErrGraphicalTargetForbidden
	}
	return nil
}

func (r *GraphicalTargetRegistry) Get(ctx context.Context, scope TargetScope, id string) (*serverconfig.GraphicalTargetDefinition, error) {
	if err := r.enter(); err != nil {
		return nil, err
	}
	defer r.leave()
	if !scope.valid() {
		return nil, ErrGraphicalTargetForbidden
	}
	if t, ok := r.static[id]; ok {
		if !scope.permits(t.TenantID) {
			return nil, nil
		}
		v := cloneTarget(t)
		return &v, nil
	}
	rec, err := r.getRecord(ctx, id)
	if err != nil || rec == nil {
		return nil, err
	}
	t := fromGraphicalRecord(*rec)
	if !scope.permits(t.TenantID) {
		return nil, nil
	}
	return &t, nil
}
func (r *GraphicalTargetRegistry) List(ctx context.Context, scope TargetScope) ([]serverconfig.GraphicalTargetDefinition, error) {
	if err := r.enter(); err != nil {
		return nil, err
	}
	defer r.leave()
	if !scope.valid() {
		return nil, ErrGraphicalTargetForbidden
	}
	records, err := runGraphicalTx(ctx, r.engine, func(s cp.GraphicalTargetStore) ([]cp.GraphicalTargetRecord, error) { return s.List(ctx) })
	if err != nil {
		return nil, err
	}
	merged := map[string]serverconfig.GraphicalTargetDefinition{}
	for _, rec := range records {
		if _, shadow := r.static[rec.TargetID]; !shadow {
			merged[rec.TargetID] = fromGraphicalRecord(rec)
		}
	}
	for id, t := range r.static {
		merged[id] = cloneTarget(t)
	}
	out := make([]serverconfig.GraphicalTargetDefinition, 0, len(merged))
	for _, t := range merged {
		if scope.permits(t.TenantID) {
			out = append(out, cloneTarget(t))
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TargetID < out[j].TargetID })
	return out, nil
}
func (r *GraphicalTargetRegistry) Create(ctx context.Context, scope TargetScope, t serverconfig.GraphicalTargetDefinition) (serverconfig.GraphicalTargetDefinition, error) {
	if err := r.enter(); err != nil {
		return t, err
	}
	defer r.leave()
	if err := t.Validate(); err != nil {
		return t, errors.New("invalid graphical target")
	}
	if err := r.scoped(scope, t.TenantID); err != nil {
		return t, err
	}
	if _, ok := r.static[t.TargetID]; ok {
		return t, ErrGraphicalTargetAlreadyExists
	}
	err := r.mutate(ctx, func(s cp.GraphicalTargetStore) error {
		cur, e := s.Get(ctx, t.TargetID)
		if e != nil {
			return e
		}
		if cur != nil {
			return ErrGraphicalTargetAlreadyExists
		}
		return s.Put(ctx, toGraphicalRecord(t, 0))
	})
	return cloneTarget(t), err
}
func (r *GraphicalTargetRegistry) Update(ctx context.Context, scope TargetScope, t serverconfig.GraphicalTargetDefinition) (serverconfig.GraphicalTargetDefinition, error) {
	if err := r.enter(); err != nil {
		return t, err
	}
	defer r.leave()
	if err := t.Validate(); err != nil {
		return t, errors.New("invalid graphical target")
	}
	if err := r.scoped(scope, t.TenantID); err != nil {
		return t, err
	}
	if _, ok := r.static[t.TargetID]; ok {
		return t, ErrGraphicalTargetImmutable
	}
	err := r.mutate(ctx, func(s cp.GraphicalTargetStore) error {
		cur, e := s.Get(ctx, t.TargetID)
		if e != nil {
			return e
		}
		if cur == nil {
			return ErrGraphicalTargetNotFound
		}
		old := fromGraphicalRecord(*cur)
		if e = r.scoped(scope, old.TenantID); e != nil {
			return e
		}
		return s.Put(ctx, toGraphicalRecord(t, cur.CreatedAt))
	})
	return cloneTarget(t), err
}
func (r *GraphicalTargetRegistry) Delete(ctx context.Context, scope TargetScope, id string) error {
	if err := r.enter(); err != nil {
		return err
	}
	defer r.leave()
	if t, ok := r.static[id]; ok {
		if err := r.scoped(scope, t.TenantID); err != nil {
			return err
		}
		return ErrGraphicalTargetImmutable
	}
	return r.mutate(ctx, func(s cp.GraphicalTargetStore) error {
		cur, e := s.Get(ctx, id)
		if e != nil {
			return e
		}
		if cur == nil {
			return ErrGraphicalTargetNotFound
		}
		t := fromGraphicalRecord(*cur)
		if e = r.scoped(scope, t.TenantID); e != nil {
			return e
		}
		_, e = s.Delete(ctx, id)
		return e
	})
}
func (r *GraphicalTargetRegistry) RuntimeRecord(ctx context.Context, scope TargetScope, id string) (*cp.GraphicalTargetRecord, error) {
	if err := r.enter(); err != nil {
		return nil, err
	}
	defer r.leave()
	if !scope.valid() {
		return nil, ErrGraphicalTargetForbidden
	}
	rec, err := r.getRecord(ctx, id)
	if err != nil || rec == nil {
		return rec, err
	}
	t := fromGraphicalRecord(*rec)
	if !scope.permits(t.TenantID) {
		return nil, nil
	}
	copy := *rec
	return &copy, nil
}
func (r *GraphicalTargetRegistry) getRecord(ctx context.Context, id string) (*cp.GraphicalTargetRecord, error) {
	return runGraphicalTx(ctx, r.engine, func(s cp.GraphicalTargetStore) (*cp.GraphicalTargetRecord, error) { return s.Get(ctx, id) })
}
func (r *GraphicalTargetRegistry) mutate(ctx context.Context, fn func(cp.GraphicalTargetStore) error) error {
	_, err := runGraphicalTx(ctx, r.engine, func(s cp.GraphicalTargetStore) (struct{}, error) { return struct{}{}, fn(s) })
	return err
}

func runGraphicalTx[T any](ctx context.Context, engine cp.Engine, op func(cp.GraphicalTargetStore) (T, error)) (zero T, err error) {
	for attempt := 0; attempt < 3; attempt++ {
		if err = ctx.Err(); err != nil {
			return zero, err
		}
		tx, e := engine.Begin(ctx)
		if e != nil {
			return zero, e
		}
		committed := false
		defer func() {
			if !committed {
				rollbackCtx, cancel := context.WithTimeout(context.Background(), time.Second)
				_ = tx.Rollback(rollbackCtx)
				cancel()
			}
		}()
		store := engine.GraphicalTargetStore(tx)
		result, e := op(store)
		if e == nil {
			e = tx.Commit(ctx)
			if e == nil {
				committed = true
				return result, nil
			}
		}
		rollbackCtx, cancel := context.WithTimeout(context.Background(), time.Second)
		_ = tx.Rollback(rollbackCtx)
		cancel()
		committed = true
		if !cp.IsConflict(e) {
			return zero, e
		}
		if attempt == 2 {
			return zero, ErrGraphicalTargetTransaction
		}
		runtime.Gosched()
	}
	return zero, ErrGraphicalTargetTransaction
}

func (r *GraphicalTargetRegistry) Close(ctx context.Context) error {
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		return nil
	}
	if !r.closing {
		r.closing = true
		r.closeAttempt = &graphicalCloseAttempt{done: make(chan struct{})}
		if r.active == 0 {
			select {
			case <-r.drain:
			default:
				close(r.drain)
			}
		}
		go r.finishClose(r.closeAttempt)
	}
	attempt := r.closeAttempt
	attempt.waiters++
	r.mu.Unlock()
	select {
	case <-attempt.done:
		return attempt.err
	default:
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-attempt.done:
		return attempt.err
	}
}
func (r *GraphicalTargetRegistry) finishClose(attempt *graphicalCloseAttempt) {
	<-r.drain
	var err error
	if r.owns {
		err = r.engine.Close(context.Background())
	}
	r.mu.Lock()
	attempt.err = err
	r.closed = err == nil
	r.closing = false
	if r.closeAttempt == attempt {
		r.closeAttempt = nil
	}
	close(attempt.done)
	r.mu.Unlock()
}

func nullPtr(n cp.NullString) *string {
	if !n.Valid {
		return nil
	}
	v := n.String
	return &v
}
func ptrNull(v *string) cp.NullString {
	if v == nil {
		return cp.NullStr()
	}
	return cp.Str(*v)
}
func toGraphicalRecord(t serverconfig.GraphicalTargetDefinition, created float64) cp.GraphicalTargetRecord {
	now := float64(time.Now().UnixNano()) / 1e9
	if created == 0 {
		created = now
	}
	labels := make([]cp.AuditLabel, 0, len(t.AuditLabels))
	keys := make([]string, 0, len(t.AuditLabels))
	for k := range t.AuditLabels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		labels = append(labels, cp.AuditLabel{Key: k, Value: t.AuditLabels[k]})
	}
	return cp.GraphicalTargetRecord{TargetID: t.TargetID, Endpoint: t.Endpoint, TLSMode: t.TLSMode, CASecretRef: ptrNull(t.CASecretRef), ClientCertSecretRef: ptrNull(t.ClientCertSecretRef), ClientKeySecretRef: ptrNull(t.ClientKeySecretRef), ExpectedServerName: ptrNull(t.ExpectedServerName), AllowedVMPatterns: cp.NewStringTuple(t.AllowedVMPatterns...), TenantID: ptrNull(t.TenantID), MinimumRole: t.MinimumRole, ConnectTimeoutS: t.ConnectTimeoutS, HandshakeTimeoutS: t.HandshakeTimeoutS, ReadTimeoutS: t.ReadTimeoutS, WriteTimeoutS: t.WriteTimeoutS, ShutdownTimeoutS: t.ShutdownTimeoutS, MaxGRPCMessageBytes: t.MaxGRPCMessageBytes, MaxFramebufferWidth: t.MaxFramebufferWidth, MaxFramebufferHeight: t.MaxFramebufferHeight, MaxRectangles: t.MaxRectangles, MaxClipboardBytes: t.MaxClipboardBytes, MaxPixelAllocationBytes: t.MaxPixelAllocationBytes, AllowedCIDRs: cp.NewStringTuple(t.AllowedCIDRs...), AuditLabels: cp.NewAuditLabels(labels...), CreatedAt: created, UpdatedAt: now}
}
func fromGraphicalRecord(r cp.GraphicalTargetRecord) serverconfig.GraphicalTargetDefinition {
	labels := map[string]string{}
	for _, v := range r.AuditLabels.Values() {
		labels[v.Key] = v.Value
	}
	return serverconfig.GraphicalTargetDefinition{TargetID: r.TargetID, Endpoint: r.Endpoint, TLSMode: r.TLSMode, CASecretRef: nullPtr(r.CASecretRef), ClientCertSecretRef: nullPtr(r.ClientCertSecretRef), ClientKeySecretRef: nullPtr(r.ClientKeySecretRef), ExpectedServerName: nullPtr(r.ExpectedServerName), AllowedVMPatterns: r.AllowedVMPatterns.Values(), TenantID: nullPtr(r.TenantID), MinimumRole: r.MinimumRole, ConnectTimeoutS: r.ConnectTimeoutS, HandshakeTimeoutS: r.HandshakeTimeoutS, ReadTimeoutS: r.ReadTimeoutS, WriteTimeoutS: r.WriteTimeoutS, ShutdownTimeoutS: r.ShutdownTimeoutS, MaxGRPCMessageBytes: r.MaxGRPCMessageBytes, MaxFramebufferWidth: r.MaxFramebufferWidth, MaxFramebufferHeight: r.MaxFramebufferHeight, MaxRectangles: r.MaxRectangles, MaxClipboardBytes: r.MaxClipboardBytes, MaxPixelAllocationBytes: r.MaxPixelAllocationBytes, AllowedCIDRs: r.AllowedCIDRs.Values(), AuditLabels: labels}
}
