//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"strings"
	"testing"
)

func kvDispatcher(kv KVStore) *CommandDispatcher {
	return newDispatcher(&Context{Env: &fakeEnv{registry: kv}})
}

func TestKVNoEnv(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "kv list"); !strings.Contains(got, "not available") {
		t.Fatalf("kv = %q", got)
	}
}

func TestKVEnvWithoutRegistry(t *testing.T) {
	d := newDispatcher(&Context{Env: &fakeEnv{}})
	if got := dispatchText(t, d, "kv list"); !strings.Contains(got, "not available") {
		t.Fatalf("kv = %q", got)
	}
}

func TestKVList(t *testing.T) {
	t.Run("with_keys", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{names: []string{"session:abc", "session:def", ""}})
		got := dispatchText(t, d, "kv list")
		if !strings.Contains(got, "session:abc") || !strings.Contains(got, "session:def") {
			t.Fatalf("kv list = %q", got)
		}
	})
	t.Run("empty", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{names: nil})
		if got := dispatchText(t, d, "kv list"); !strings.Contains(got, "no keys") {
			t.Fatalf("kv list = %q", got)
		}
	})
	t.Run("only_blank_names", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{names: []string{""}})
		if got := dispatchText(t, d, "kv list"); !strings.Contains(got, "no keys") {
			t.Fatalf("kv list = %q", got)
		}
	})
	t.Run("error", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{listErr: errString("list error")})
		if got := dispatchText(t, d, "kv list"); !strings.Contains(got, "list error") {
			t.Fatalf("kv list = %q", got)
		}
	})
}

func TestKVGet(t *testing.T) {
	t.Run("no_key", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{})
		if got := dispatchText(t, d, "kv get"); !strings.Contains(got, "usage: kv get") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("with_prefix", func(t *testing.T) {
		kv := &fakeKV{values: map[string]string{"session:mykey": "some_value"}}
		d := kvDispatcher(kv)
		got := dispatchText(t, d, "kv get session:mykey")
		if !strings.Contains(got, "some_value") || !strings.Contains(got, "session:mykey") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("adds_prefix", func(t *testing.T) {
		kv := &fakeKV{values: map[string]string{"session:mykey": "val"}}
		d := kvDispatcher(kv)
		dispatchText(t, d, "kv get mykey")
		if len(kv.getCalls) != 1 || kv.getCalls[0] != "session:mykey" {
			t.Fatalf("get calls = %v", kv.getCalls)
		}
	})
	t.Run("missing", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{values: map[string]string{}})
		if got := dispatchText(t, d, "kv get missing"); !strings.Contains(got, "key not found") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("error", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{getErr: errString("get error")})
		if got := dispatchText(t, d, "kv get mykey"); !strings.Contains(got, "get error") {
			t.Fatalf("= %q", got)
		}
	})
}

func TestKVSet(t *testing.T) {
	t.Run("no_args", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{})
		if got := dispatchText(t, d, "kv set"); !strings.Contains(got, "usage: kv set") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("missing_value", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{})
		if got := dispatchText(t, d, "kv set mykey"); !strings.Contains(got, "usage: kv set") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("success", func(t *testing.T) {
		kv := &fakeKV{}
		d := kvDispatcher(kv)
		got := dispatchText(t, d, "kv set mykey value")
		if !strings.Contains(got, "set") {
			t.Fatalf("= %q", got)
		}
		if len(kv.putCalls) != 1 || kv.putCalls[0].key != "session:mykey" || kv.putCalls[0].value != "value" {
			t.Fatalf("put calls = %v", kv.putCalls)
		}
	})
	t.Run("with_prefix", func(t *testing.T) {
		kv := &fakeKV{}
		d := kvDispatcher(kv)
		dispatchText(t, d, "kv set session:mykey value")
		if kv.putCalls[0].key != "session:mykey" {
			t.Fatalf("put key = %q", kv.putCalls[0].key)
		}
	})
	t.Run("error", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{putErr: errString("put error")})
		if got := dispatchText(t, d, "kv set mykey value"); !strings.Contains(got, "put error") {
			t.Fatalf("= %q", got)
		}
	})
}

func TestKVDelete(t *testing.T) {
	t.Run("no_arg", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{})
		if got := dispatchText(t, d, "kv delete"); !strings.Contains(got, "usage: kv delete") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("success", func(t *testing.T) {
		kv := &fakeKV{}
		d := kvDispatcher(kv)
		got := dispatchText(t, d, "kv delete mykey")
		if !strings.Contains(got, "deleted") {
			t.Fatalf("= %q", got)
		}
		if kv.delCalls[0] != "session:mykey" {
			t.Fatalf("del = %v", kv.delCalls)
		}
	})
	t.Run("with_prefix", func(t *testing.T) {
		kv := &fakeKV{}
		d := kvDispatcher(kv)
		dispatchText(t, d, "kv delete session:mykey")
		if kv.delCalls[0] != "session:mykey" {
			t.Fatalf("del = %v", kv.delCalls)
		}
	})
	t.Run("error", func(t *testing.T) {
		d := kvDispatcher(&fakeKV{delErr: errString("delete error")})
		if got := dispatchText(t, d, "kv delete mykey"); !strings.Contains(got, "delete error") {
			t.Fatalf("= %q", got)
		}
	})
}

func TestKVInvalidSubcommand(t *testing.T) {
	for _, line := range []string{"kv foo", "kv"} {
		d := kvDispatcher(&fakeKV{})
		if got := dispatchText(t, d, line); !strings.Contains(got, "usage: kv list") {
			t.Fatalf("%q → %q", line, got)
		}
	}
}
