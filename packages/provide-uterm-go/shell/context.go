//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import "context"

// The Python dispatcher receives a ctx dict of duck-typed objects (Cloudflare
// KV/DO bindings, a storage handle, an async session lister). Go replaces that
// duck typing with the small interfaces below; a nil field means "binding not
// available in this context", exactly as a missing dict key does in Python.

// KVStore is the Cloudflare KV binding used by the kv command (Python
// env.SESSION_REGISTRY). Key-shape duck typing from the Python (dict / object
// with .name / bare string) collapses to plain key names here.
type KVStore interface {
	// List returns the names of keys with the given prefix.
	List(ctx context.Context, prefix string) ([]string, error)
	// Get returns the value for key, or (nil, nil) when the key is absent.
	Get(ctx context.Context, key string) (*string, error)
	// Put writes value at key.
	Put(ctx context.Context, key, value string) error
	// Delete removes key.
	Delete(ctx context.Context, key string) error
}

// DONamespace is the Durable-Object namespace used to force-terminate a
// session (Python env.SESSION_RUNTIME + idFromName/get/fetch DELETE).
type DONamespace interface {
	// Kill sends the terminate signal for the given session id.
	Kill(ctx context.Context, sessionID string) error
}

// Storage is the Durable-Object storage handle used by the storage command
// (Python ctx.storage). Key-shape duck typing collapses to plain names.
type Storage interface {
	// List returns the storage key names.
	List(ctx context.Context) ([]string, error)
	// Get returns the value for key, or (nil, nil) when absent.
	Get(ctx context.Context, key string) (*string, error)
}

// Env is the Cloudflare env object (Python ctx["env"]). Registry and Runtime
// return the SESSION_REGISTRY / SESSION_RUNTIME bindings (nil when absent), and
// Attrs lists the public attribute names → type labels for the env command.
type Env interface {
	Registry() KVStore
	Runtime() DONamespace
	Attrs() map[string]string
}

// SessionLister lists sessions from the KV registry (Python
// ctx["list_kv_sessions"]). Each entry mirrors the Python dict so missing
// fields can default to "?" exactly as s.get("field", "?") does.
type SessionLister func(ctx context.Context) ([]map[string]any, error)

// Context is the runtime context passed to CommandDispatcher. It is the Go
// analogue of the Python ctx dict.
type Context struct {
	// Values holds arbitrary named context entries; the env command lists
	// their keys when Env is nil (Python's ctx-key fallback).
	Values map[string]any
	// Env is the Cloudflare env object, or nil.
	Env Env
	// Storage is the DO storage handle, or nil.
	Storage Storage
	// ListKVSessions lists KV sessions, or nil when unavailable.
	ListKVSessions SessionLister
}
