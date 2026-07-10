//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"testing"
)

// dispatch is a test helper that dispatches a line and returns the first text
// frame. It fails the test if the result is animated.
func dispatchText(t *testing.T, d *CommandDispatcher, line string) string {
	t.Helper()
	r := d.Dispatch(context.Background(), line)
	if r.Animated != nil {
		t.Fatalf("line %q returned an animation, want text", line)
	}
	if len(r.Text) == 0 {
		t.Fatalf("line %q returned no text frames", line)
	}
	return r.Text[0]
}

// newDispatcher builds a dispatcher over ctx (nil → empty).
func newDispatcher(ctx *Context) *CommandDispatcher {
	return NewCommandDispatcher(ctx)
}

// fakeKV is an in-memory KVStore.
type fakeKV struct {
	names    []string
	values   map[string]string
	listErr  error
	getErr   error
	putErr   error
	delErr   error
	putCalls []struct{ key, value string }
	delCalls []string
	getCalls []string
}

func (k *fakeKV) List(_ context.Context, _ string) ([]string, error) {
	return k.names, k.listErr
}

func (k *fakeKV) Get(_ context.Context, key string) (*string, error) {
	k.getCalls = append(k.getCalls, key)
	if k.getErr != nil {
		return nil, k.getErr
	}
	if k.values == nil {
		return nil, nil
	}
	v, ok := k.values[key]
	if !ok {
		return nil, nil
	}
	return &v, nil
}

func (k *fakeKV) Put(_ context.Context, key, value string) error {
	k.putCalls = append(k.putCalls, struct{ key, value string }{key, value})
	return k.putErr
}

func (k *fakeKV) Delete(_ context.Context, key string) error {
	k.delCalls = append(k.delCalls, key)
	return k.delErr
}

// fakeDO is a DONamespace.
type fakeDO struct {
	err    error
	killed []string
}

func (d *fakeDO) Kill(_ context.Context, sessionID string) error {
	d.killed = append(d.killed, sessionID)
	return d.err
}

// fakeStorage is a Storage.
type fakeStorage struct {
	names   []string
	values  map[string]string
	listErr error
	getErr  error
}

func (s *fakeStorage) List(_ context.Context) ([]string, error) {
	return s.names, s.listErr
}

func (s *fakeStorage) Get(_ context.Context, key string) (*string, error) {
	if s.getErr != nil {
		return nil, s.getErr
	}
	if s.values == nil {
		return nil, nil
	}
	v, ok := s.values[key]
	if !ok {
		return nil, nil
	}
	return &v, nil
}

// fakeEnv is an Env with optional bindings and attributes.
type fakeEnv struct {
	registry KVStore
	runtime  DONamespace
	attrs    map[string]string
}

func (e *fakeEnv) Registry() KVStore    { return e.registry }
func (e *fakeEnv) Runtime() DONamespace { return e.runtime }
func (e *fakeEnv) Attrs() map[string]string {
	if e.attrs == nil {
		return map[string]string{}
	}
	return e.attrs
}
