//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"strings"
)

// cmdStorage dispatches the storage list|get subcommands against the DO
// storage. Port of commands/storage.py:cmd_storage.
func cmdStorage(ctx context.Context, c *Context, arg string) Result {
	if c == nil || c.Storage == nil {
		return textResult(ErrorMsg("storage not available in this context") + Prompt)
	}
	storage := c.Storage

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
		keys, err := storage.List(ctx)
		if err != nil {
			return textResult(ErrorMsg(err.Error()) + Prompt)
		}
		var kept []string
		for _, k := range keys {
			if k != "" {
				kept = append(kept, "  "+Cyan+k+Reset)
			}
		}
		if len(kept) == 0 {
			return textResult(InfoMsg("no storage keys found") + Prompt)
		}
		return textResult(strings.Join(kept, "\r\n") + "\r\n" + Prompt)

	case "get":
		if keyArg == "" {
			return textResult(ErrorMsg("usage: storage get <key>") + Prompt)
		}
		value, err := storage.Get(ctx, keyArg)
		if err != nil {
			return textResult(ErrorMsg(err.Error()) + Prompt)
		}
		if value == nil {
			return textResult(InfoMsg("key not found: "+keyArg) + Prompt)
		}
		return textResult(Dim + keyArg + Reset + "\r\n" + *value + "\r\n" + Prompt)
	}

	return textResult(ErrorMsg("usage: storage list | storage get <key>") + Prompt)
}
