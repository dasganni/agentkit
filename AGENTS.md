# agentkit

Shared library for the apps: provider and model catalog, effort mapping, detached runs, history
budget, titles, the claude/codex sidecar, the agent API router, forward-auth, CLI distribution
and the Go CLI. Python package `agentkit` (src layout, Python 3.13, uv) and Go module `cli/`.

## Layout

- `src/agentkit/` Python modules, `tests/` one pytest file per module
- `cli/` Go module `github.com/dasganni/agentkit/cli` (cmd, internal/config|oauth|client|tools|cmd|version)
- `scripts/` gate and CLI distribution, `integrations/` plugin templates

Module interfaces and how an app adopts the kit (routers, Dockerfile Go stage, shim, sidecar
variables, versioning) are in README.md; change it with the code.

## Gate

`./scripts/gate.sh`: ruff check, ruff format --check, mypy src, pytest; then `go vet` and
`go test` in a `golang:1.26.9-alpine` container. Green before every commit.

## Rules

- Conventional Commits, no AI attribution.
- Standard library first, then a library, before hand-written code; no second copy of logic.
- Comments state current behaviour, at most three lines.
- Secrets never in logs, fixtures or integrations; no server details in this repository.
