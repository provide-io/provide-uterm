#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
set -eo pipefail
export PATH="$PATH:$HOME/.dotnet/tools:$HOME/go/bin"

HUGO_DIR="$(pwd)/../site-uterm-io/content/docs/api"
mkdir -p "$HUGO_DIR/python" "$HUGO_DIR/go" "$HUGO_DIR/csharp"

# 1. Generate Go Docs (using gomarkdoc)
echo "Generating Go API docs..."
go run github.com/princjef/gomarkdoc/cmd/gomarkdoc@latest --output "$HUGO_DIR/go/api.md" ./packages/provide-uterm-go/...

# 2. Generate Python Docs
echo "Generating Python API docs..."
uv run python -m pdoc -o "$HUGO_DIR/python" ./packages/provide-uterm/src/provide/uterm

# 3. Generate C# Docs
echo "Generating C# API docs..."
dotnet build packages/provide-uterm-csharp/Provide.Uterm.slnx -c Release
# dotnet tool install -g xmldocmd || true
# xmldocmd packages/provide-uterm-csharp/src/Provide.Uterm/bin/Release/net10.0/Provide.Uterm.dll "$HUGO_DIR/csharp" || true
echo "Skipping C# xmldocmd due to .NET 7 runtime requirement on macOS"
