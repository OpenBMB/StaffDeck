#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

VERSION="${VERSION:-0.1.0}"
DEB_VERSION="${VERSION#v}"
ARCH="$(uname -m)"

if [[ "$ARCH" != "x86_64" ]]; then
  echo "Unsupported Linux architecture: $ARCH (expected x86_64)" >&2
  exit 2
fi

TOOLCHAIN="$REPO/packaging/toolchain"
NODE_VERSION="${NODE_VERSION:-20.19.5}"
NODE_HOME="$TOOLCHAIN/node-v${NODE_VERSION}-linux-x64"
NODE_TARBALL="$TOOLCHAIN/node-v${NODE_VERSION}-linux-x64.tar.xz"
mkdir -p "$TOOLCHAIN"

if [[ ! -x "$NODE_HOME/bin/node" ]]; then
  echo "==> [1/8] Downloading portable Node.js ${NODE_VERSION}"
  python3 - "$NODE_TARBALL" "$NODE_VERSION" <<'PY'
import sys
import urllib.request

target, version = sys.argv[1:]
url = f"https://nodejs.org/dist/v{version}/node-v{version}-linux-x64.tar.xz"
urllib.request.urlretrieve(url, target)
PY
  tar -xJf "$NODE_TARBALL" -C "$TOOLCHAIN"
  rm -f "$NODE_TARBALL"
fi
export PATH="$NODE_HOME/bin:$PATH"

BINUTILS_HOME="$TOOLCHAIN/binutils"
PORTABLE_OBJDUMP="$BINUTILS_HOME/root/usr/bin/objdump"
PORTABLE_BINUTILS_LIB="$BINUTILS_HOME/root/usr/lib/x86_64-linux-gnu"
if ! command -v objdump >/dev/null 2>&1 && \
    ! LD_LIBRARY_PATH="$PORTABLE_BINUTILS_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
      "$PORTABLE_OBJDUMP" --version >/dev/null 2>&1; then
  echo "==> [toolchain] Downloading portable Ubuntu binutils"
  rm -rf "$BINUTILS_HOME"
  mkdir -p "$BINUTILS_HOME/packages" "$BINUTILS_HOME/root"
  (
    cd "$BINUTILS_HOME/packages"
    apt-get download \
      binutils binutils-common binutils-x86-64-linux-gnu \
      libbinutils libctf0 libctf-nobfd0
    for package in ./*.deb; do
      dpkg-deb -x "$package" "$BINUTILS_HOME/root"
    done
  )
  rm -rf "$BINUTILS_HOME/packages"
fi
if [[ -d "$BINUTILS_HOME/root/usr/bin" ]]; then
  export PATH="$BINUTILS_HOME/root/usr/bin:$PATH"
fi
if [[ -d "$PORTABLE_BINUTILS_LIB" ]]; then
  export LD_LIBRARY_PATH="$PORTABLE_BINUTILS_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
command -v objdump >/dev/null 2>&1 || {
  echo "objdump is unavailable after preparing binutils" >&2
  exit 3
}

echo "==> [2/8] Installing and building frontend"
npm --prefix frontend-enterprise ci --no-audit --no-fund
npm --prefix frontend-enterprise run build

echo "==> [3/8] Preparing portable Python 3.11 runtime"
BUILD_PY="$REPO/packaging/runtime_dl/python/bin/python3"
if [[ -x "$BUILD_PY" ]] && "$BUILD_PY" -c "import requests, docx, openpyxl" 2>/dev/null; then
  echo "Reusing verified Python runtime at $BUILD_PY"
else
  python3 packaging/fetch_runtime_python.py packaging/runtime_dl --expect-arch x86_64
  rm -f packaging/runtime_dl/*.tar.gz
fi

echo "==> [4/8] Creating backend build environment"
rm -rf backend/.venv
"$BUILD_PY" -m venv backend/.venv
VENV_PY="$REPO/backend/.venv/bin/python"
"$VENV_PY" -m pip install --disable-pip-version-check --no-cache-dir -U pip
DEPS="$(cd backend && "$BUILD_PY" -c "import pathlib,tomllib; print(' '.join(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']))")"
# shellcheck disable=SC2086
"$VENV_PY" -m pip install --disable-pip-version-check --no-cache-dir $DEPS
"$VENV_PY" -m pip install --disable-pip-version-check --no-cache-dir "pyinstaller>=6.6.0" "certifi>=2024.2.2"

echo "==> [5/8] Building application with PyInstaller"
rm -rf packaging/out packaging/build
(
  cd backend
  .venv/bin/pyinstaller ../packaging/ultrarag.spec --noconfirm \
    --distpath ../packaging/out --workpath ../packaging/build
)
rm -rf packaging/out/staffdeck/runtime
cp -a packaging/runtime_dl/python packaging/out/staffdeck/runtime

echo "==> [6/8] Building Debian package"
STAGE="packaging/out/deb"
rm -rf "$STAGE"
mkdir -p \
  "$STAGE/DEBIAN" \
  "$STAGE/opt/staffdeck" \
  "$STAGE/usr/bin" \
  "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/icons/hicolor/128x128/apps"
cp -a packaging/out/staffdeck/. "$STAGE/opt/staffdeck/"
cp packaging/assets/staffdeck.png "$STAGE/usr/share/icons/hicolor/128x128/apps/staffdeck.png"
cat > "$STAGE/usr/bin/staffdeck" <<'SH'
#!/bin/sh
exec /opt/staffdeck/staffdeck "$@"
SH
chmod 0755 "$STAGE/usr/bin/staffdeck"
cat > "$STAGE/usr/share/applications/staffdeck.desktop" <<'DESK'
[Desktop Entry]
Name=URStaff
Comment=URStaff desktop service
Exec=staffdeck
Icon=staffdeck
Terminal=false
Type=Application
Categories=Utility;
StartupNotify=true
DESK
INSTALLED_SIZE="$(du -sk "$STAGE/opt/staffdeck" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: staffdeck
Version: $DEB_VERSION
Section: utils
Priority: optional
Architecture: amd64
Installed-Size: $INSTALLED_SIZE
Maintainer: OpenBMB <support@openbmb.cn>
Description: URStaff desktop service
 URStaff runs a local service and opens its interface in the default browser.
EOF
cat > "$STAGE/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
exit 0
SH
cat > "$STAGE/DEBIAN/postrm" <<'SH'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
exit 0
SH
chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"
DEB_OUT="packaging/out/URStaff-${VERSION}-linux-x86_64.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$DEB_OUT"

echo "==> [7/8] Building AppImage"
APPDIR="packaging/out/URStaff.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/staffdeck"
cp -a packaging/out/staffdeck/. "$APPDIR/usr/lib/staffdeck/"
cat > "$APPDIR/usr/bin/staffdeck" <<'SH'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/../lib/staffdeck/staffdeck" "$@"
SH
chmod 0755 "$APPDIR/usr/bin/staffdeck"
cat > "$APPDIR/AppRun" <<'SH'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/staffdeck" "$@"
SH
chmod 0755 "$APPDIR/AppRun"
cp "$STAGE/usr/share/applications/staffdeck.desktop" "$APPDIR/staffdeck.desktop"
cp packaging/assets/staffdeck.png "$APPDIR/staffdeck.png"

APPIMAGETOOL="${APPIMAGETOOL:-$TOOLCHAIN/appimagetool-x86_64.AppImage}"
if [[ ! -x "$APPIMAGETOOL" ]]; then
  python3 - "$APPIMAGETOOL" <<'PY'
import sys
import urllib.request

url = "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
urllib.request.urlretrieve(url, sys.argv[1])
PY
  chmod 0755 "$APPIMAGETOOL"
fi
APPIMAGE_OUT="packaging/out/URStaff-${VERSION}-linux-x86_64.AppImage"
ARCH=x86_64 "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$APPIMAGE_OUT"

echo "==> [8/8] Verifying package metadata"
dpkg-deb --info "$DEB_OUT" >/dev/null
chmod 0755 "$APPIMAGE_OUT"
sha256sum "$DEB_OUT" "$APPIMAGE_OUT"
ls -lh "$DEB_OUT" "$APPIMAGE_OUT"
