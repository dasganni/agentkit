#!/bin/sh
# Plugin shim for the @APP@ CLI: fetches the binary the server serves under /cli/, verifies its
# sha256, caches it per version (two kept) and runs it. "--warm" only fetches. Exit 3: no server, no cache.
APP="@APP@"
PREFIX=$(printf '%s' "$APP" | tr 'a-z-.' 'A-Z__')
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/$APP/cli"
CHECK_SECONDS=3600

warm=0
[ "${1:-}" = "--warm" ] && warm=1

fail() {
  printf '%s: %s\n' "$APP" "$2" >&2
  [ "$warm" = 1 ] && exit 0
  exit "$1"
}

fetch() { # url, output file
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --max-time 120 -o "$2" "$1"
  else
    wget -q -T 120 -O "$2" "$1"
  fi
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

server_url() {
  eval "url=\${${PREFIX}_URL:-}"
  if [ -z "$url" ]; then
    for f in "${XDG_CONFIG_HOME:-$HOME/.config}/$APP/config.json" \
      "$HOME/Library/Application Support/$APP/config.json"; do
      [ -r "$f" ] || continue
      url=$(sed -n 's/.*"url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$f" | head -n 1)
      [ -n "$url" ] && break
    done
  fi
  printf '%s' "${url%/}"
}

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  MINGW* | MSYS* | CYGWIN*) os=windows ;;
  *) fail 1 "unsupported system $(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  arm64 | aarch64) arch=arm64 ;;
  *) fail 1 "unsupported architecture $(uname -m)" ;;
esac
bin="$APP"
[ "$os" = windows ] && bin="$APP.exe"

newest_cached() {
  for d in $(ls -t "$CACHE" 2>/dev/null); do
    if [ -x "$CACHE/$d/$bin" ]; then
      printf '%s' "$d"
      return
    fi
  done
}

mkdir -p "$CACHE" || fail 3 "cannot create $CACHE"
url=$(server_url)
now=$(date +%s)
last=0
[ -r "$CACHE/checked" ] && last=$(cat "$CACHE/checked" 2>/dev/null)
case "$last" in '' | *[!0-9]*) last=0 ;; esac

version=""
if [ -n "$url" ]; then
  if [ $((now - last)) -lt "$CHECK_SECONDS" ] && [ -r "$CACHE/latest" ]; then
    version=$(cat "$CACHE/latest")
  elif fetch "$url/cli/version" "$CACHE/version.tmp"; then
    version=$(tr -d ' \t\r\n' <"$CACHE/version.tmp")
    rm -f "$CACHE/version.tmp"
    case "$version" in '' | *[!0-9A-Za-z._-]*) version="" ;; esac
    if [ -n "$version" ]; then
      printf '%s' "$version" >"$CACHE/latest"
      printf '%s' "$now" >"$CACHE/checked"
    fi
  fi
fi
case "$version" in '' | *[!0-9A-Za-z._-]*) version="" ;; esac
[ -z "$version" ] && version=$(newest_cached)
[ -z "$version" ] && fail 3 "no server reachable and no cached CLI (set ${PREFIX}_URL)"

if [ ! -x "$CACHE/$version/$bin" ]; then
  [ -n "$url" ] || fail 3 "CLI $version not cached and no server URL"
  tmp=$(mktemp -d "$CACHE/dl.XXXXXX") || fail 3 "cannot create temp dir"
  trap 'rm -rf "$tmp"' EXIT
  name="$os-$arch"
  fetch "$url/cli/$name" "$tmp/bin.gz" && fetch "$url/cli/$name.sha256" "$tmp/bin.sha256" ||
    fail 3 "download of $name failed"
  want=$(cut -d' ' -f1 <"$tmp/bin.sha256" | tr -d ' \t\r\n')
  [ -n "$want" ] && [ "$(sha256_of "$tmp/bin.gz")" = "$want" ] ||
    fail 1 "sha256 mismatch for $name, refusing to run it"
  gzip -dc "$tmp/bin.gz" >"$tmp/$bin" || fail 1 "cannot unpack $name"
  chmod 755 "$tmp/$bin"
  mkdir -p "$CACHE/$version"
  mv -f "$tmp/$bin" "$CACHE/$version/$bin"
  rm -rf "$tmp"
  trap - EXIT
fi
touch "$CACHE/$version"

# keep the two newest versions
for d in $(ls -t "$CACHE" | grep -v -e '^latest$' -e '^checked$' -e '^dl\.' | tail -n +3); do
  [ -d "$CACHE/$d" ] && [ "$d" != "$version" ] && rm -rf "${CACHE:?}/$d"
done

[ "$warm" = 1 ] && exit 0
exec "$CACHE/$version/$bin" "$@"
