#!/bin/sh
# Plugin shim for the @APP@ CLI: fetches the binary the server serves under /cli/, verifies its
# sha256, caches it per version (two kept) and runs it. "--warm" only fetches. Exit 3: no server, no cache.
APP="@APP@"
PREFIX=$(printf '%s' "$APP" | tr 'a-z-.' 'A-Z__')
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/$APP/cli"
CHECK_SECONDS=3600
RETRY_SECONDS=300

warm=0
[ "${1:-}" = "--warm" ] && warm=1

fail() {
  printf '%s: %s\n' "$APP" "$2" >&2
  [ "$warm" = 1 ] && exit 0
  exit "$1"
}

# fetch url file [max seconds]: a short connect timeout so an unreachable server falls back to the cache
# quickly; redirects from https never leave https.
fetch() {
  f_url=$1
  f_out=$2
  f_secs=${3:-10}
  if command -v curl >/dev/null 2>&1; then
    set -- -fsSL --connect-timeout 5 --max-time "$f_secs"
    case "$f_url" in https://*) set -- "$@" --proto-redir =https ;; esac
    curl "$@" -o "$f_out" "$f_url"
  else
    wget -q --max-redirect=0 -T "$f_secs" -O "$f_out" "$f_url"
  fi
}

# check_url refuses a server URL that is not https (http only for localhost/127.0.0.1) or carries credentials.
check_url() {
  rest=${1#*://}
  authority=${rest%%/*}
  case "$authority" in
    *@*) fail 1 "the server URL must not carry credentials: $1" ;;
  esac
  case "$1" in
    https://?*) ;;
    http://localhost | http://localhost[:/]* | http://127.0.0.1 | http://127.0.0.1[:/]*) ;;
    *) fail 1 "the server URL must use https (http only for localhost): $1" ;;
  esac
}

# login_url_arg prints the URL of a "login --url URL" call, so a first login works without <APP>_URL.
login_url_arg() {
  [ "${1:-}" = login ] || return 0
  shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --url=* | -url=*)
        printf '%s' "${1#*=}"
        return 0
        ;;
      --url | -url)
        printf '%s' "${2:-}"
        return 0
        ;;
    esac
    shift
  done
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

server_url() { # login URL argument
  url=$1
  [ -n "$url" ] || eval "url=\${${PREFIX}_URL:-}"
  if [ -z "$url" ]; then
    for f in "${XDG_CONFIG_HOME:-$HOME/.config}/$APP/config.json" \
      "$HOME/Library/Application Support/$APP/config.json" \
      "${APPDATA:-/nonexistent}/$APP/config.json"; do
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
url=$(server_url "$(login_url_arg "$@")")
[ -z "$url" ] || check_url "$url"
now=$(date +%s)
last=0
failed=0
[ -r "$CACHE/failed" ] && failed=$(cat "$CACHE/failed" 2>/dev/null)
case "$failed" in '' | *[!0-9]*) failed=0 ;; esac
[ -r "$CACHE/checked" ] && last=$(cat "$CACHE/checked" 2>/dev/null)
case "$last" in '' | *[!0-9]*) last=0 ;; esac

version=""
if [ -n "$url" ]; then
  if [ $((now - last)) -lt "$CHECK_SECONDS" ] && [ -r "$CACHE/latest" ]; then
    version=$(cat "$CACHE/latest")
  elif [ $((now - failed)) -ge "$RETRY_SECONDS" ]; then
    vtmp=$(mktemp "$CACHE/version.XXXXXX") || fail 3 "cannot create temp file"
    if fetch "$url/cli/version" "$vtmp"; then
      version=$(tr -d ' \t\r\n' <"$vtmp")
      case "$version" in '' | *[!0-9A-Za-z._-]*) version="" ;; esac
      if [ -n "$version" ]; then
        printf '%s' "$version" >"$CACHE/latest"
        printf '%s' "$now" >"$CACHE/checked"
        rm -f "$CACHE/failed"
      fi
    else
      printf '%s' "$now" >"$CACHE/failed"
    fi
    rm -f "$vtmp"
  fi
fi
case "$version" in '' | *[!0-9A-Za-z._-]*) version="" ;; esac
[ -z "$version" ] && version=$(newest_cached)
[ -z "$version" ] && fail 3 "no server reachable and no cached CLI (set ${PREFIX}_URL)"

download() { # fills the cache for $version; returns 2 when the server cannot be reached
  tmp=$(mktemp -d "$CACHE/dl.XXXXXX") || fail 3 "cannot create temp dir"
  trap 'rm -rf "$tmp"' EXIT
  name="$os-$arch"
  fetch "$url/cli/$name" "$tmp/bin.gz" 120 && fetch "$url/cli/$name.sha256" "$tmp/bin.sha256" || {
    rm -rf "$tmp"
    trap - EXIT
    return 2
  }
  want=$(cut -d' ' -f1 <"$tmp/bin.sha256" | tr -d ' \t\r\n')
  [ -n "$want" ] && [ "$(sha256_of "$tmp/bin.gz")" = "$want" ] ||
    fail 1 "sha256 mismatch for $name, refusing to run it"
  gzip -dc "$tmp/bin.gz" >"$tmp/$bin" || fail 1 "cannot unpack $name"
  chmod 755 "$tmp/$bin"
  mkdir -p "$CACHE/$version"
  mv -f "$tmp/$bin" "$CACHE/$version/$bin"
  rm -rf "$tmp"
  trap - EXIT
}

if [ ! -x "$CACHE/$version/$bin" ]; then
  [ -n "$url" ] || fail 3 "CLI $version not cached and no server URL"
  download
  if [ $? -eq 2 ]; then
    version=$(newest_cached)
    [ -n "$version" ] || fail 3 "download of the CLI failed and no cached CLI"
  fi
fi
touch "$CACHE/$version"

# keep the two newest versions
for d in $(ls -t "$CACHE" | grep -v -e '^latest$' -e '^checked$' -e '^failed$' -e '^dl\.' -e '^version\.' | tail -n +3); do
  [ -d "$CACHE/$d" ] && [ "$d" != "$version" ] && rm -rf "${CACHE:?}/$d"
done

[ "$warm" = 1 ] && exit 0
exec "$CACHE/$version/$bin" "$@"
