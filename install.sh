#!/bin/sh
# Installs terminal-ct for the current user: runtime in PREFIX/lib/terminal-ct, launcher in PREFIX/bin.
# No root needed. Usage: sh install.sh [--prefix /absolute/path]   (default prefix: ~/.local)
set -eu
PREFIX="$HOME/.local"
case "${1:-}" in
  "") ;;
  --prefix) [ $# -ge 2 ] || { echo "usage: sh install.sh [--prefix /absolute/path]" >&2; exit 1; }; PREFIX="$2" ;;
  *) echo "usage: sh install.sh [--prefix /absolute/path]" >&2; exit 1 ;;
esac
case "$PREFIX" in /*) ;; *) echo "prefix must be absolute" >&2; exit 1 ;; esac
SRC=$(cd "$(dirname "$0")" && pwd)
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
LIB="$PREFIX/lib/terminal-ct"
mkdir -p "$LIB/skills/terminal-ct" "$PREFIX/bin"
cp "$SRC/terminal_ct.py" "$LIB/"
cp "$SRC/skills/terminal-ct/SKILL.md" "$LIB/skills/terminal-ct/"
printf '#!/bin/sh\nexec python3 "%s/terminal_ct.py" "$@"\n' "$LIB" > "$PREFIX/bin/terminal-ct"
chmod 755 "$PREFIX/bin/terminal-ct"
echo "installed: $PREFIX/bin/terminal-ct"
case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) echo "add $PREFIX/bin to your PATH" ;; esac
"$PREFIX/bin/terminal-ct" doctor || true
