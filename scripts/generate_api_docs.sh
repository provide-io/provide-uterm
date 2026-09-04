#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
set -eo pipefail
export PATH="$PATH:$HOME/.dotnet/tools:$HOME/go/bin"

# Where the site checkout lives. The default is the historical sibling path,
# which is right when both repos are cloned side by side and wrong everywhere
# else -- on a machine whose real checkout is elsewhere it resolved to an empty
# directory, and because the script only ever ran `mkdir -p`, it created the
# tree and wrote a full API reference into a decoy nobody reads. Validate the
# target instead of conjuring it: a real checkout has hugo.toml at its root.
SITE_DIR="${SITE_DIR:-$(pwd)/../site-uterm-io}"
# Canonicalise before reporting. `..` is resolved against the SYMLINK TARGET,
# not the path you typed, so from a symlinked checkout of this repo the sibling
# default lands somewhere the name gives no hint of -- which is how the decoy
# above got written in the first place. Say where it really looked.
if [ -d "$SITE_DIR" ]; then
  SITE_DIR="$(cd "$SITE_DIR" && pwd -P)"
fi
if [ ! -f "$SITE_DIR/hugo.toml" ]; then
  echo "error: not a site-uterm-io checkout: $SITE_DIR" >&2
  echo "       (looked for $SITE_DIR/hugo.toml)" >&2
  echo "       set SITE_DIR=/path/to/site-uterm-io and re-run" >&2
  exit 1
fi

HUGO_DIR="$SITE_DIR/content/docs/api"
mkdir -p "$HUGO_DIR/python" "$HUGO_DIR/go" "$HUGO_DIR/csharp"

# 1. Generate Go Docs (using gomarkdoc)
echo "Generating Go API docs..."
go run github.com/princjef/gomarkdoc/cmd/gomarkdoc@latest --output "$HUGO_DIR/go/api.md" ./packages/provide-uterm-go/...

# gomarkdoc emits no Hugo front matter, so the page rendered with an empty
# <title> and no name in the docs list. Prepend it here rather than by hand:
# the file carries a "DO NOT EDIT" banner and is overwritten on every run, so a
# hand-edit would survive exactly until the next regeneration.
go_api="$HUGO_DIR/go/api.md"
printf '%s\n' '---' 'title: "Go API Reference"' 'type: "docs"' '---' '' | cat - "$go_api" >"$go_api.tmp"
mv "$go_api.tmp" "$go_api"

# 2. Generate Python Docs
echo "Generating Python API docs..."
uv run python -m pdoc -o "$HUGO_DIR/python" ./packages/provide-uterm/src/provide/uterm

# 3. Generate C# Docs
echo "Generating C# API docs..."
dotnet build packages/provide-uterm-csharp/Provide.Uterm.slnx -c Release
# dotnet tool install -g xmldocmd || true
# xmldocmd packages/provide-uterm-csharp/src/Provide.Uterm/bin/Release/net10.0/Provide.Uterm.dll "$HUGO_DIR/csharp" || true
echo "Skipping C# xmldocmd due to .NET 7 runtime requirement on macOS"
