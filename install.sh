#!/bin/sh
# terminal-ct installer for Linux, macOS and WSL: the tool, what it needs (after one confirmation), the command on
# the PATH, and Claude Code's MCP server and skill when Claude Code is there. No root for the tool itself.
#   curl -fsSL https://raw.githubusercontent.com/superkaliiii/terminal-conteneur/main/install.sh | sh
#   sh install.sh [--yes] [--no-deps] [--no-mcp] [--prefix DIR] [--update] [--uninstall]
set -eu
TOOL=terminal-ct
FILE=terminal_ct.py
SKILL=skills/terminal-ct/SKILL.md
STOREVAR=TERMINAL_CT_HOME
ARCHIVE=${TERMINAL_CT_ARCHIVE:-https://github.com/superkaliiii/terminal-conteneur/archive/refs/heads/main.tar.gz}
SUBDIR=             # where FILE sits in that archive (its root)
PYMIN=9             # Python 3.PYMIN or later
needs() {           # the packages this tool needs that are missing here, for package manager $1
  missing=""
  command -v podman >/dev/null 2>&1 || command -v docker >/dev/null 2>&1 || missing="podman"
  # under WSL terminal-ct gives Podman the iptables firewall driver: Alpine's podman does not bring iptables
  if grep -qi microsoft /proc/version 2>/dev/null && ! command -v iptables >/dev/null 2>&1; then missing="$missing iptables"; fi
  command -v openssl >/dev/null 2>&1 || missing="$missing openssl"
  echo $missing
}
after_deps() { :; }  # nothing more for containers (rootless Podman needs no group)

# ===== common to terminal-ct and terminal-vm's install.sh =====
YES=0 DEPS=1 MCP=1 MODE=install PREFIX="$HOME/.local"
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --no-deps) DEPS=0 ;;
    --no-mcp) MCP=0 ;;
    --update) MODE=install ;;
    --uninstall) MODE=uninstall ;;
    --prefix) [ $# -ge 2 ] || { echo "--prefix needs a folder" >&2; exit 2; }; PREFIX="$2"; shift ;;
    -h|--help) sed -n '2,5p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $1 (--yes --no-deps --no-mcp --prefix DIR --update --uninstall)" >&2; exit 2 ;;
  esac
  shift
done
case "$PREFIX" in /*) ;; *) echo "--prefix must be an absolute folder" >&2; exit 2 ;; esac
LIB="$PREFIX/lib/$TOOL" BIN="$PREFIX/bin/$TOOL" MARK="# added by $TOOL's installer"
say() { printf '%s\n' "$*"; }
ask() {  # ask "question": yes with --yes; no answer possible (no terminal): no
  [ "$YES" = 1 ] && return 0
  [ -r /dev/tty ] || { say "$1 [skipped: no terminal to ask; run again with --yes]"; return 1; }
  printf '%s [Y/n] ' "$1" > /dev/tty
  read -r a < /dev/tty || return 1
  case "$a" in ""|y|Y|yes|o|O|oui) return 0 ;; *) return 1 ;; esac
}
sudo_() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }
pkg_install() {  # pkg_install PACKAGES...: with this system's package manager
  if command -v apt-get >/dev/null 2>&1; then sudo_ apt-get update -qq && sudo_ apt-get install -y "$@"
  elif command -v dnf >/dev/null 2>&1; then sudo_ dnf install -y "$@"
  elif command -v zypper >/dev/null 2>&1; then sudo_ zypper install -y "$@"
  elif command -v pacman >/dev/null 2>&1; then sudo_ pacman -S --noconfirm "$@"
  elif command -v apk >/dev/null 2>&1; then sudo_ apk add --no-cache "$@"  # a fresh Alpine has no index yet
  elif command -v brew >/dev/null 2>&1; then brew install "$@"
  else say "no package manager found: install $* yourself"; return 1
  fi
}
manager() { for m in apt-get dnf zypper pacman apk brew; do command -v $m >/dev/null 2>&1 && { echo $m; return; }; done; }
profiles() { for f in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do if [ -f "$f" ]; then echo "$f"; fi; done; }

if [ "$MODE" = uninstall ]; then
  if [ "$MCP" = 1 ] && command -v claude >/dev/null 2>&1; then
    claude mcp remove "$TOOL" -s user >/dev/null 2>&1 && say "MCP server $TOOL removed from Claude Code" || true
    [ -d "$HOME/.claude/skills/$TOOL" ] && rm -rf "$HOME/.claude/skills/$TOOL" && say "skill removed"
  fi
  rm -rf "$LIB"; rm -f "$BIN"
  for f in $(profiles); do
    if grep -qF "$MARK" "$f"; then grep -vF "$MARK" "$f" > "$f.tmp-$$" && cat "$f.tmp-$$" > "$f"; rm -f "$f.tmp-$$"; fi
  done
  say "$TOOL removed. What it made stays (remove it first with $TOOL itself if you want it gone), and so does"
  say "its data folder: \$$STOREVAR, by default ~/.local/share/$TOOL."
  exit 0
fi

# 1. what it needs
py_ok() { command -v python3 >/dev/null 2>&1 && python3 -c "import sys; sys.exit(sys.version_info < (3, $PYMIN))"; }
if [ "$DEPS" = 1 ]; then
  m=$(manager)
  want=$(needs "$m")
  py_ok || want="python3 $want"
  if [ -n "$want" ]; then
    if [ -z "$m" ]; then say "missing: $want (no package manager found: install them, then run this again)"
    elif ask "$TOOL needs: $want. Install them now ($m, with administrator rights)?"; then pkg_install $want || true
    else say "not installed: $want ($TOOL doctor says what each is for)"
    fi
  fi
  after_deps
fi
py_ok || { say "Python 3.$PYMIN or later is required"; exit 1; }

# 2. the tool: from this folder when it holds it, else downloaded
SRC=$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")
TMP=""
if [ -z "$SRC" ] || [ ! -f "$SRC/$FILE" ]; then
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  say "downloading $TOOL..."
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$ARCHIVE" -o "$TMP/a.tgz"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$TMP/a.tgz" "$ARCHIVE"
  else python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$ARCHIVE" "$TMP/a.tgz"
  fi
  tar -xzf "$TMP/a.tgz" -C "$TMP"
  SRC=$(dirname "$(find "$TMP" -path "*$SUBDIR/$FILE" | head -1)")
  [ -f "$SRC/$FILE" ] || { say "the download holds no $FILE"; exit 1; }
fi
mkdir -p "$LIB/$(dirname "$SKILL")" "$PREFIX/bin"
cp "$SRC/$FILE" "$LIB/$FILE.new" && mv "$LIB/$FILE.new" "$LIB/$FILE"
cp "$SRC/$SKILL" "$LIB/$SKILL"
printf '#!/bin/sh\nexec python3 "%s/%s" "$@"\n' "$LIB" "$FILE" > "$BIN.new" && chmod 755 "$BIN.new" && mv "$BIN.new" "$BIN"
say "installed: $BIN ($("$BIN" version 2>/dev/null || echo "?"))"

# 3. on the PATH
case ":$PATH:" in
  *":$PREFIX/bin:"*) ;;
  *) line="export PATH=\"$PREFIX/bin:\$PATH\"  $MARK"
     files=$(profiles); [ -n "$files" ] || files="$HOME/.profile"
     for f in $files; do grep -qF "$MARK" "$f" 2>/dev/null || printf '\n%s\n' "$line" >> "$f"; done
     say "added $PREFIX/bin to your PATH: open a new terminal (or run: export PATH=\"$PREFIX/bin:\$PATH\")" ;;
esac

# 4. the agent: Claude Code's MCP server and skill
if [ "$MCP" = 1 ] && command -v claude >/dev/null 2>&1; then
  claude mcp remove "$TOOL" -s user >/dev/null 2>&1 || true  # an update: the path may have changed
  if claude mcp add -s user "$TOOL" -- "$BIN" mcp >/dev/null 2>&1; then say "MCP server $TOOL declared in Claude Code"
  else say "could not declare the MCP server: claude mcp add -s user $TOOL -- $BIN mcp"; fi
  "$BIN" skills install >/dev/null 2>&1 && say "skill installed in ~/.claude/skills/$TOOL" || true
else
  say "for an agent: MCP server = $BIN mcp   (skill: $BIN skills path)"
fi

# 5. what is left to fix, if anything
say ""
"$BIN" doctor || say "doctor found something to fix: each line above gives the command"
