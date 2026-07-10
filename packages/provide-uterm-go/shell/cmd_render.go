//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"net/http"
	"strconv"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/render"
)

// imageRenderer converts image bytes to ANSI frames plus a source FPS. It
// matches render.ImageToANSIFrames and is a dispatcher field so tests can
// inject alternative behaviours (e.g. an empty-frame result).
type imageRenderer func(data []byte, cols, rows int, mode render.ColorMode) ([]string, float64, error)

const renderUsage = "usage: render [--mode truecolor|256|16] [--cols N] [--rows N] [--fps N] [--loop] <url>"

// cmdRender fetches an image URL and converts it to ANSI frames. Port of
// commands/render.py:cmd_render.
//
// The Python "missing dependency" ImportError branch is unreachable here: the
// render package is always compiled in, so there is no optional import to fail.
func cmdRender(ctx context.Context, client *http.Client, renderImage imageRenderer, arg string) Result {
	if arg == "" {
		return textResult(ErrorMsg(renderUsage) + Prompt)
	}

	mode := render.ModeTruecolor
	cols := 80
	rows := 24
	loop := false
	var fpsOverride *float64
	url := ""

	tokens := pyFields(arg)
	for i := 0; i < len(tokens); {
		tok := tokens[i]
		switch {
		case tok == "--mode" && i+1 < len(tokens):
			raw := tokens[i+1]
			if raw != "truecolor" && raw != "256" && raw != "16" {
				return textResult(ErrorMsg("unknown mode '"+raw+"' (use truecolor, 256, or 16)") + Prompt)
			}
			mode = render.ColorMode(raw)
			i += 2
		case tok == "--cols" && i+1 < len(tokens):
			v, err := strconv.Atoi(tokens[i+1])
			if err != nil {
				return textResult(ErrorMsg("invalid --cols value: "+tokens[i+1]) + Prompt)
			}
			cols = v
			i += 2
		case tok == "--rows" && i+1 < len(tokens):
			v, err := strconv.Atoi(tokens[i+1])
			if err != nil {
				return textResult(ErrorMsg("invalid --rows value: "+tokens[i+1]) + Prompt)
			}
			rows = v
			i += 2
		case tok == "--fps" && i+1 < len(tokens):
			v, err := strconv.ParseFloat(tokens[i+1], 64)
			if err != nil {
				return textResult(ErrorMsg("invalid --fps value: "+tokens[i+1]) + Prompt)
			}
			fpsOverride = &v
			i += 2
		case tok == "--loop":
			loop = true
			i++
		case !strings.HasPrefix(tok, "--"):
			url = tok
			i++
		default:
			return textResult(ErrorMsg("unknown flag: "+tok) + Prompt)
		}
	}

	if url == "" {
		return textResult(ErrorMsg(renderUsage) + Prompt)
	}

	data, errRes, ok := fetchBytes(ctx, client, url)
	if !ok {
		return errRes
	}

	frames, sourceFPS, err := renderImage(data, cols, rows, mode)
	if err != nil {
		return textResult(ErrorMsg("cannot decode image: "+err.Error()) + Prompt)
	}

	fpsFinal := sourceFPS
	if fpsOverride != nil {
		fpsFinal = *fpsOverride
	}

	if len(frames) <= 1 || fpsFinal <= 0 {
		if len(frames) > 0 {
			return textResult(frames[0] + Prompt)
		}
		return textResult(ErrorMsg("empty image") + Prompt)
	}

	return animatedResult(AnimatedResult{Frames: frames, FPS: fpsFinal, Loop: loop})
}
