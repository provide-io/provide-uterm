//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"io"
	"net/http"
	"strings"
	"time"
)

// doHTTP issues an HTTP request and returns the status and (bounded) body.
//
// Security note: the reference (fetch.py / cast.py / render.py via urllib)
// applies NO allowlist, host filtering, or SSRF protection — a single-tenant
// convenience shell deliberately allows arbitrary requests. This port
// preserves that: no destination is blocked. The one hardening deviation is
// that net/http speaks only http/https, whereas urllib could also open
// file://, ftp://, etc.; the cast/render commands still handle file:// URLs
// explicitly (see their own scheme checks), so behaviour is preserved for the
// schemes those commands accept.
//
// timeout bounds the whole request. maxRead caps the number of body bytes read
// (<= 0 reads the full body). A user-agent header is set to match the Python
// requests that send one.
func doHTTP(
	ctx context.Context,
	client *http.Client,
	method, url string,
	body *string,
	timeout time.Duration,
	maxRead int64,
	userAgent string,
) (int, []byte, error) {
	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	var bodyReader io.Reader
	if body != nil {
		bodyReader = strings.NewReader(*body)
	}
	req, err := http.NewRequestWithContext(reqCtx, method, url, bodyReader)
	if err != nil {
		return 0, nil, err
	}
	if userAgent != "" {
		req.Header.Set("User-Agent", userAgent)
	}

	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer func() { _ = resp.Body.Close() }()

	var reader io.Reader = resp.Body
	if maxRead > 0 {
		reader = io.LimitReader(resp.Body, maxRead)
	}
	data, err := io.ReadAll(reader)
	if err != nil {
		return 0, nil, err
	}
	return resp.StatusCode, data, nil
}
