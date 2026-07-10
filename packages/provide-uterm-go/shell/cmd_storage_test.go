//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"strings"
	"testing"
)

func storageDispatcher(s Storage) *CommandDispatcher {
	return newDispatcher(&Context{Storage: s})
}

func TestStorageNoStorage(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "storage list"); !strings.Contains(got, "not available") {
		t.Fatalf("= %q", got)
	}
}

func TestStorageList(t *testing.T) {
	t.Run("with_keys", func(t *testing.T) {
		d := storageDispatcher(&fakeStorage{names: []string{"key1", "key2"}})
		got := dispatchText(t, d, "storage list")
		if !strings.Contains(got, "key1") || !strings.Contains(got, "key2") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("empty", func(t *testing.T) {
		d := storageDispatcher(&fakeStorage{})
		if got := dispatchText(t, d, "storage list"); !strings.Contains(got, "no storage keys found") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("blank_names_only", func(t *testing.T) {
		d := storageDispatcher(&fakeStorage{names: []string{""}})
		if got := dispatchText(t, d, "storage list"); !strings.Contains(got, "no storage keys found") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("error", func(t *testing.T) {
		d := storageDispatcher(&fakeStorage{listErr: errString("storage error")})
		if got := dispatchText(t, d, "storage list"); !strings.Contains(got, "storage error") {
			t.Fatalf("= %q", got)
		}
	})
}

func TestStorageGet(t *testing.T) {
	t.Run("no_key", func(t *testing.T) {
		d := storageDispatcher(&fakeStorage{})
		if got := dispatchText(t, d, "storage get"); !strings.Contains(got, "usage: storage get") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("success", func(t *testing.T) {
		d := storageDispatcher(&fakeStorage{values: map[string]string{"mykey": "myvalue"}})
		got := dispatchText(t, d, "storage get mykey")
		if !strings.Contains(got, "myvalue") || !strings.Contains(got, "mykey") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("not_found", func(t *testing.T) {
		d := storageDispatcher(&fakeStorage{values: map[string]string{}})
		if got := dispatchText(t, d, "storage get mykey"); !strings.Contains(got, "key not found") {
			t.Fatalf("= %q", got)
		}
	})
	t.Run("error", func(t *testing.T) {
		d := storageDispatcher(&fakeStorage{getErr: errString("get error")})
		if got := dispatchText(t, d, "storage get mykey"); !strings.Contains(got, "get error") {
			t.Fatalf("= %q", got)
		}
	})
}

func TestStorageInvalidSubcommand(t *testing.T) {
	d := storageDispatcher(&fakeStorage{})
	if got := dispatchText(t, d, "storage bogus"); !strings.Contains(got, "usage: storage list") {
		t.Fatalf("= %q", got)
	}
}
