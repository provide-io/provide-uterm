#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# provide-uterm — Go language server image
#
# Build (from repo root):
#   docker build -f docker/Dockerfile.go -t provide-uterm-server-go .
#
# Run (fails closed until a real config is mounted — same as Dockerfile.server):
#   docker run --rm -p 27781:27780 provide-uterm-server-go
#   docker run --rm -p 27781:27780 \
#     -v /path/to/my.toml:/config/server.toml:ro \
#     provide-uterm-server-go
#
# Wire-compatible with the Python reference server on the multi-backend contract.
#

ARG GO_IMAGE=golang:1.26-bookworm

# ---- build -----------------------------------------------------------------
FROM ${GO_IMAGE} AS build

WORKDIR /src

# Module root lives under packages/provide-uterm-go (own go.mod).
COPY packages/provide-uterm-go/go.mod packages/provide-uterm-go/go.sum ./
RUN go mod download

COPY packages/provide-uterm-go/ ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/uterm ./cmd/uterm

# ---- runtime ---------------------------------------------------------------
FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=build /out/uterm /uterm
# Fail-closed placeholder config (JWT placeholders rejected until replaced).
# Prefer mounting a real file at /etc/uterm/server.toml (directory mount works
# more reliably across Docker Desktop share roots than file-on-file binds).
COPY docker/server.toml /etc/uterm/server.toml

EXPOSE 27780
USER nonroot:nonroot

ENTRYPOINT ["/uterm", "server", "--config", "/etc/uterm/server.toml", "--host", "0.0.0.0", "--port", "27780"]
