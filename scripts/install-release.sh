#!/bin/sh
# Bootstrap an authenticated, SHA256-pinned Linux ZIP; no system Python needed.
set -eu
if [ "$#" -lt 2 ]; then
  printf '%s\n' 'Usage: install-release.sh ARTIFACT.zip SHA256 [--no-start] [distribution install options...]' >&2
  exit 2
fi
archive=$1
expected=$2
shift 2
start=yes
if [ "${1-}" = --no-start ]; then start=no; shift; fi
install_dir="$HOME/.local/share/remote-mng"
previous=''
for argument do
  if [ "$previous" = --install-dir ]; then install_dir=$argument; fi
  previous=$argument
done
case "$archive" in /*) ;; *) archive="$PWD/$archive";; esac
case "$expected" in *[!0-9a-fA-F]*|'') printf '%s\n' 'Expected a full SHA256' >&2; exit 2;; esac
[ "${#expected}" -eq 64 ] || exit 2
command -v sha256sum >/dev/null || { printf '%s\n' 'sha256sum is required to verify this release' >&2; exit 1; }
command -v unzip >/dev/null || { printf '%s\n' 'Info-ZIP unzip is required to unpack this release; install it or use an existing rmg distribution install' >&2; exit 1; }
actual=$(sha256sum -- "$archive")
actual=${actual%% *}
[ "$actual" = "$(printf '%s' "$expected" | tr A-F a-f)" ] || { printf '%s\n' 'Checksum mismatch; no artifact code executed' >&2; exit 1; }
stage=$(mktemp -d "${TMPDIR:-/tmp}/rmg-bootstrap.XXXXXXXX")
trap 'case "$stage" in "${TMPDIR:-/tmp}"/rmg-bootstrap.*) rm -rf -- "$stage";; esac' EXIT HUP INT TERM
# The checksum above must come from the selected authenticated release. Refuse
# path traversal before unzip; the embedded installer verifies the entire tree.
unzip -Z1 "$archive" > "$stage/entries"
while IFS= read -r entry; do
  case "$entry" in /*|*\\*|*../*|../*|*/..|..|*:*) printf '%s\n' 'Unsafe archive path' >&2; exit 1;; esac
done < "$stage/entries"
if unzip -Z -l "$archive" | grep '^l' >/dev/null; then
  printf '%s\n' 'Archive links are forbidden' >&2
  exit 1
fi
unzip -q "$archive" -d "$stage/payload"
[ -f "$stage/payload/rmg" ] || { printf '%s\n' 'Expected rmg at archive root' >&2; exit 1; }
chmod u+x "$stage/payload/rmg"
"$stage/payload/rmg" --json distribution install "$archive" --sha256 "$expected" "$@"
if [ "$start" = yes ]; then
  "$install_dir/bin/start-manager.sh" || {
    printf '%s\n' "Tool and Skill installed, but manager startup failed. Run $install_dir/bin/open-console.sh outside the Agent sandbox." >&2
    exit 1
  }
fi
