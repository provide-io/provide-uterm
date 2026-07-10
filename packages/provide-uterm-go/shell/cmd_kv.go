//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"fmt"
	"strings"
)

// kvPrefix is the KV key prefix for session entries. Port of
// commands/kv.py:_KV_PREFIX.
const kvPrefix = "session:"

// strOrDefault mirrors Python str(m.get(key, def)): the string form of the
// value when key is present, else def.
func strOrDefault(m map[string]any, key, def string) string {
	if v, ok := m[key]; ok {
		return fmt.Sprint(v)
	}
	return def
}

// cmdSessions lists sessions stored in the KV registry. Port of
// commands/kv.py:cmd_sessions.
func cmdSessions(ctx context.Context, c *Context) Result {
	if c == nil || c.ListKVSessions == nil {
		return textResult(ErrorMsg("list_kv_sessions not available in this context") + Prompt)
	}
	sessions, err := c.ListKVSessions(ctx)
	if err != nil {
		return textResult(ErrorMsg(err.Error()) + Prompt)
	}
	if len(sessions) == 0 {
		return textResult(InfoMsg("no sessions found") + Prompt)
	}
	rows := make([][]string, 0, len(sessions))
	for _, s := range sessions {
		status := "idle"
		if truthy(s["connected"]) {
			status = "live"
		}
		rows = append(rows, []string{
			strOrDefault(s, "session_id", "?"),
			strOrDefault(s, "lifecycle_state", "?"),
			strOrDefault(s, "connector_type", "?"),
			status,
		})
	}
	table := FmtTable(rows, []string{"session_id", "state", "type", "status"})
	return textResult(table + Prompt)
}

// cmdSessionsKill force-terminates a session Durable Object. Port of
// commands/kv.py:cmd_sessions_kill.
func cmdSessionsKill(ctx context.Context, c *Context, sessionID string) Result {
	if sessionID == "" {
		return textResult(ErrorMsg("usage: sessions kill <session_id>") + Prompt)
	}
	var ns DONamespace
	if c != nil && c.Env != nil {
		ns = c.Env.Runtime()
	}
	if ns == nil {
		return textResult(ErrorMsg("SESSION_RUNTIME DO binding not available") + Prompt)
	}
	if err := ns.Kill(ctx, sessionID); err != nil {
		return textResult(ErrorMsg(err.Error()) + Prompt)
	}
	return textResult(SuccessMsg("kill signal sent to "+sessionID) + Prompt)
}

// cmdKV dispatches the kv list|get|set|delete subcommands. Port of
// commands/kv.py:cmd_kv.
func cmdKV(ctx context.Context, c *Context, arg string) Result {
	var kv KVStore
	if c != nil && c.Env != nil {
		kv = c.Env.Registry()
	}
	if kv == nil {
		return textResult(ErrorMsg("SESSION_REGISTRY KV binding not available") + Prompt)
	}

	subParts := pySplit1(arg)
	sub := ""
	keyArg := ""
	if len(subParts) > 0 {
		sub = strings.ToLower(subParts[0])
	}
	if len(subParts) > 1 {
		keyArg = pyStrip(subParts[1])
	}

	switch sub {
	case "list":
		names, err := kv.List(ctx, kvPrefix)
		if err != nil {
			return textResult(ErrorMsg(err.Error()) + Prompt)
		}
		var kept []string
		for _, n := range names {
			if n != "" {
				kept = append(kept, "  "+Cyan+n+Reset)
			}
		}
		if len(kept) == 0 {
			return textResult(InfoMsg("no keys found") + Prompt)
		}
		return textResult(strings.Join(kept, "\r\n") + "\r\n" + Prompt)

	case "get":
		if keyArg == "" {
			return textResult(ErrorMsg("usage: kv get <key>") + Prompt)
		}
		fullKey := withPrefix(keyArg)
		value, err := kv.Get(ctx, fullKey)
		if err != nil {
			return textResult(ErrorMsg(err.Error()) + Prompt)
		}
		if value == nil {
			return textResult(InfoMsg("key not found: "+fullKey) + Prompt)
		}
		return textResult(Dim + fullKey + Reset + "\r\n" + *value + "\r\n" + Prompt)

	case "set":
		if keyArg == "" {
			return textResult(ErrorMsg("usage: kv set <key> <value>") + Prompt)
		}
		kvParts := pySplit1(keyArg)
		if len(kvParts) < 2 {
			return textResult(ErrorMsg("usage: kv set <key> <value>") + Prompt)
		}
		fullKey := withPrefix(kvParts[0])
		if err := kv.Put(ctx, fullKey, kvParts[1]); err != nil {
			return textResult(ErrorMsg(err.Error()) + Prompt)
		}
		return textResult(SuccessMsg("set "+fullKey) + Prompt)

	case "delete":
		if keyArg == "" {
			return textResult(ErrorMsg("usage: kv delete <key>") + Prompt)
		}
		fullKey := withPrefix(keyArg)
		if err := kv.Delete(ctx, fullKey); err != nil {
			return textResult(ErrorMsg(err.Error()) + Prompt)
		}
		return textResult(SuccessMsg("deleted "+fullKey) + Prompt)
	}

	return textResult(ErrorMsg("usage: kv list | kv get <key> | kv set <key> <value> | kv delete <key>") + Prompt)
}

// withPrefix prepends the session: prefix unless it is already present.
func withPrefix(key string) string {
	if strings.HasPrefix(key, kvPrefix) {
		return key
	}
	return kvPrefix + key
}
