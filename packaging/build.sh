#!/bin/sh
# System packages of terminal-ct: a .deb (dpkg-deb), a .rpm (rpmbuild, when installed) and a Homebrew formula.
#   sh packaging/build.sh [OUTDIR]                    the .deb and .rpm of this checkout
#   sh packaging/build.sh --brew TAG_ARCHIVE_URL      the formula for a published release (its sha256 from the URL)
# The tool is one Python file: the packages hold it, its skill and a launcher, and declare what it needs.
set -eu
TOOL=terminal-ct
CLASS=TerminalCt                                # its Homebrew formula class
FILE=terminal_ct.py
SKILL=skills/terminal-ct/SKILL.md
SUMMARY="Containers with a desktop and a shell, for people and computer-use agents (CLI + MCP server)"
HOMEPAGE=https://github.com/superkaliiii/terminal-conteneur
LICENSE_ID=MIT
DEB_DEPENDS="python3 (>= 3.9), podman | docker.io"
DEB_RECOMMENDS="openssl, iptables"
RPM_REQUIRES="python3 >= 3.9, podman"  # given a choice dnf takes Docker, whose daemon may not run
RPM_RECOMMENDS="openssl"
BREW_DEPENDS='depends_on "python@3.12"
  depends_on "podman"'
ROOT=$(cd "$(dirname "$0")/.." && pwd)        # where FILE is

# ===== common to terminal-ct and terminal-vm's packaging/build.sh =====
VERSION=$(tr -d '\r' < "$ROOT/$FILE" | sed -n 's/^VERSION = "\(.*\)"$/\1/p')  # a CRLF checkout too
[ -n "$VERSION" ] || { echo "no VERSION in $FILE" >&2; exit 1; }
if [ "${1:-}" = "--brew" ]; then
  [ $# -ge 2 ] || { echo "--brew needs the release archive's URL" >&2; exit 2; }
  url=$2 tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$url" -o "$tmp/a"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$tmp/a" "$url"
  else python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$url" "$tmp/a"
  fi
  if command -v sha256sum >/dev/null 2>&1; then sha=$(sha256sum "$tmp/a" | cut -d' ' -f1)
  else sha=$(shasum -a 256 "$tmp/a" | cut -d' ' -f1); fi
  cat <<EOF
class $CLASS < Formula
  desc "$SUMMARY"
  homepage "$HOMEPAGE"
  url "$url"
  sha256 "$sha"
  version "$VERSION"
$( [ -n "$LICENSE_ID" ] && echo "  license \"$LICENSE_ID\"" )
  $BREW_DEPENDS

  def install
    libexec.install Dir["**/$FILE"].first => "$FILE"
    (libexec/"$(dirname "$SKILL")").install Dir["**/$SKILL"].first
    (bin/"$TOOL").write <<~SH
      #!/bin/sh
      exec "#{Formula["python@3.12"].opt_bin}/python3" "#{libexec}/$FILE" "\$@"
    SH
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/$TOOL version")
  end
end
EOF
  exit 0
fi
OUT=$(cd "${1:-$ROOT/dist}" 2>/dev/null && pwd || { mkdir -p "${1:-$ROOT/dist}" && cd "${1:-$ROOT/dist}" && pwd; })
stage() {  # stage DIR: the files at their system places
  mkdir -p "$1/usr/lib/$TOOL/$(dirname "$SKILL")" "$1/usr/bin"
  cp "$ROOT/$FILE" "$1/usr/lib/$TOOL/"
  cp "$ROOT/$SKILL" "$1/usr/lib/$TOOL/$SKILL"
  printf '#!/bin/sh\nexec python3 /usr/lib/%s/%s "$@"\n' "$TOOL" "$FILE" > "$1/usr/bin/$TOOL"
  chmod 755 "$1/usr/bin/$TOOL"; chmod 644 "$1/usr/lib/$TOOL/$FILE" "$1/usr/lib/$TOOL/$SKILL"
}
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
if command -v dpkg-deb >/dev/null 2>&1; then
  stage "$W/deb"
  mkdir -p "$W/deb/DEBIAN"
  cat > "$W/deb/DEBIAN/control" <<EOF
Package: $TOOL
Version: $VERSION
Architecture: all
Maintainer: superkaliiii <193693917+superkaliiii@users.noreply.github.com>
Depends: $DEB_DEPENDS
Recommends: $DEB_RECOMMENDS
Homepage: $HOMEPAGE
Description: $SUMMARY
EOF
  dpkg-deb --root-owner-group --build "$W/deb" "$OUT/${TOOL}_${VERSION}_all.deb" >/dev/null
  echo "$OUT/${TOOL}_${VERSION}_all.deb"
else
  echo "no dpkg-deb: no .deb (Debian/Ubuntu: it comes with dpkg)" >&2
fi
if command -v rpmbuild >/dev/null 2>&1; then
  stage "$W/rpmroot"
  mkdir -p "$W/rpm/SPECS"
  cat > "$W/rpm/SPECS/$TOOL.spec" <<EOF
Name: $TOOL
Version: $VERSION
Release: 1
Summary: $SUMMARY
License: ${LICENSE_ID:-unspecified}
URL: $HOMEPAGE
BuildArch: noarch
Requires: $RPM_REQUIRES
Recommends: $RPM_RECOMMENDS
%description
$SUMMARY.
%install
cp -a $W/rpmroot/. %{buildroot}/
%files
/usr/bin/$TOOL
/usr/lib/$TOOL
EOF
  rpmbuild --quiet -bb --define "_topdir $W/rpm" "$W/rpm/SPECS/$TOOL.spec"
  cp "$W"/rpm/RPMS/noarch/*.rpm "$OUT/"
  ls "$OUT"/*.rpm
else
  echo "no rpmbuild: no .rpm (Debian/Ubuntu: apt install rpm)" >&2
fi
