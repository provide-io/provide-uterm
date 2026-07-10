//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"errors"
	"testing"
)

func TestRegistryStartsEmpty(t *testing.T) {
	r := NewWorkerRegistry()
	mustEqual(t, r.Len(), 0, "len")
	mustTrue(t, r.Get("nope") == nil, "get missing")
	mustFalse(t, r.Contains("nope"), "contains missing")
	mustEqual(t, len(r.All()), 0, "all")
	mustEqual(t, len(r.Keys()), 0, "keys")
}

func TestRegistryPutGetPopRoundtrip(t *testing.T) {
	r := NewWorkerRegistry()
	st := NewWorkerTermState()
	r.Put("w1", st)
	mustTrue(t, r.Get("w1") == st, "get returns same")
	mustTrue(t, r.Contains("w1"), "contains")
	mustEqual(t, r.Len(), 1, "len")
	mustDeepEqual(t, r.Keys(), []string{"w1"}, "keys")
	mustEqual(t, len(r.All()), 1, "all len")
	mustTrue(t, r.All()[0] == st, "all[0]")
	popped := r.Pop("w1")
	mustTrue(t, popped == st, "pop returns same")
	mustTrue(t, r.Get("w1") == nil, "get after pop")
	mustTrue(t, r.Pop("w1") == nil, "pop absent -> nil")
}

func TestRegistrySetDefaultKeepsExisting(t *testing.T) {
	r := NewWorkerRegistry()
	first := NewWorkerTermState()
	second := NewWorkerTermState()
	mustTrue(t, r.SetDefault("w1", first) == first, "setdefault inserts")
	mustTrue(t, r.SetDefault("w1", second) == first, "setdefault keeps existing")
	mustTrue(t, r.Get("w1") == first, "get is first")
}

func TestRegistryDiscardReturnsTruth(t *testing.T) {
	r := NewWorkerRegistry()
	r.Put("w1", NewWorkerTermState())
	mustTrue(t, r.Discard("w1"), "discard present")
	mustFalse(t, r.Discard("w1"), "discard absent")
}

func TestRegistryRequireRaisesOnMissing(t *testing.T) {
	r := NewWorkerRegistry()
	st := NewWorkerTermState()
	r.Put("w1", st)
	got, err := r.Require("w1")
	mustTrue(t, err == nil && got == st, "require present")
	_, err = r.Require("missing")
	var notFound *ErrWorkerNotFound
	mustTrue(t, errors.As(err, &notFound), "require missing errors")
	mustEqual(t, notFound.WorkerID, "missing", "error names worker id")
	mustTrue(t, notFound.Error() != "", "error string")
}

func TestRegistryKeysSortedSnapshot(t *testing.T) {
	r := NewWorkerRegistry()
	r.Put("b", NewWorkerTermState())
	r.Put("a", NewWorkerTermState())
	r.Put("c", NewWorkerTermState())
	mustDeepEqual(t, r.Keys(), []string{"a", "b", "c"}, "sorted keys")
	mustEqual(t, len(r.All()), 3, "all len")
}
