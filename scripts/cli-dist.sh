#!/bin/sh
# Usage: cli-dist.sh APP ENV_PREFIX SCHEMA OUT VERSION
# Builds the CLI for linux/darwin/windows on amd64/arm64 into OUT as gzip files
# <os>-<arch>[.sha256] plus a "version" file. Runs in golang:1.26.9-alpine.
# The published version is VERSION-<8 hex of sha256(agentkit version + schema bytes)>.
set -eu
[ $# -eq 5 ] || { echo "usage: $0 APP ENV_PREFIX SCHEMA OUT VERSION" >&2; exit 2; }
app=$1 prefix=$2 schema=$3 out=$4 version=$5
root=$(cd "$(dirname "$0")/.." && pwd)
schema=$(cd "$(dirname "$schema")" && pwd)/$(basename "$schema")
kit=$(sed -n 's/^var Version = "\(.*\)"$/\1/p' "$root/cli/internal/version/version.go")
[ -n "$kit" ] || { echo "cannot read the agentkit version" >&2; exit 1; }
hash=$({ printf '%s' "$kit"; cat "$schema"; } | sha256sum | cut -c1-8)
version=$version-$hash
mkdir -p "$out"
out=$(cd "$out" && pwd)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -R "$root/cli" "$work/cli"
cp "$schema" "$work/cli/cmd/agentcli/tools.json"

cd "$work/cli"
for os in linux darwin windows; do
  for arch in amd64 arm64; do
    name="$os-$arch"
    bin="$work/$name"
    CGO_ENABLED=0 GOOS=$os GOARCH=$arch go build -trimpath \
      -ldflags "-s -w -X main.app=$app -X main.envPrefix=$prefix -X github.com/dasganni/agentkit/cli/internal/version.Version=$version" -o "$bin" ./cmd/agentcli
    gzip -9 -n -c "$bin" >"$out/$name"
    (cd "$out" && sha256sum "$name" >"$name.sha256")
  done
done
printf '%s\n' "$version" >"$out/version"
