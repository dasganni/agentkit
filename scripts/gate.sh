#!/usr/bin/env bash
# Python gate on the host, Go gate in a container (no Go toolchain on the host).
set -euo pipefail
cd "$(dirname "$0")/.."
run() { nice -n 15 ionice -c3 "$@"; }

run uv run ruff check
run uv run ruff format --check
run uv run mypy src
run uv run pytest -q

run docker run --rm -u "$(id -u):$(id -g)" \
  -e HOME=/tmp -e GOCACHE=/tmp/gocache -e GOPATH=/tmp/go \
  -v "$PWD/cli":/src -w /src golang:1.26.9-alpine \
  sh -c "go vet ./... && go test ./..."
