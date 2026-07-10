//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// cmdFetch issues an HTTP request and returns a formatted response preview.
// Port of commands/fetch.py:cmd_fetch (the js.fetch browser branch is dropped;
// Go always uses the net/http path).
func cmdFetch(ctx context.Context, client *http.Client, arg string) Result {
	if arg == "" {
		return textResult(ErrorMsg("usage: fetch [-X METHOD] <url> [body]") + Prompt)
	}

	method := "GET"
	rest := arg
	if rest == "-X" || strings.HasPrefix(rest, "-X ") || strings.HasPrefix(rest, "-X\t") {
		parts := pySplit1(rest[2:])
		if len(parts) == 0 {
			return textResult(ErrorMsg("usage: fetch [-X METHOD] <url> [body]") + Prompt)
		}
		method = strings.ToUpper(parts[0])
		if len(parts) > 1 {
			rest = parts[1]
		} else {
			rest = ""
		}
	}

	urlBody := pySplit1(rest)
	url := ""
	if len(urlBody) > 0 {
		url = urlBody[0]
	}
	var body *string
	if len(urlBody) > 1 {
		body = &urlBody[1]
	}

	if url == "" {
		return textResult(ErrorMsg("usage: fetch [-X METHOD] <url> [body]") + Prompt)
	}

	status, data, err := doHTTP(ctx, client, method, url, body, 10*time.Second, 4096, "")
	if err != nil {
		return textResult(ErrorMsg(err.Error()) + Prompt)
	}

	text := strings.ToValidUTF8(string(data), "�")
	runes := []rune(text)
	previewRunes := runes
	if len(previewRunes) > 800 {
		previewRunes = previewRunes[:800]
	}
	preview := strings.ReplaceAll(string(previewRunes), "\n", "\r\n")
	truncated := ""
	if len(runes) > 800 {
		truncated = " …"
	}
	color := Green
	if status >= 500 {
		color = Red
	} else if status >= 400 {
		color = Yellow
	}
	return textResult(fmt.Sprintf("%sHTTP %d%s\r\n%s%s\r\n", color, status, Reset, preview, truncated) + Prompt)
}
