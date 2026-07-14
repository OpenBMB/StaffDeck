#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
VERSION="${VERSION:-0.1.0}"
ARCH="$(uname -m)"

echo "==> [1/5] 构建前端"
npm --prefix frontend-enterprise run build

echo "==> [2/5] 后端 venv + 运行依赖 + 打包工具"
cd backend
if [ ! -x ".venv/bin/pyinstaller" ]; then
  # 若无 venv（如 CI 全新 checkout），自建并装运行依赖
  if [ ! -x ".venv/bin/python" ]; then
    python3 -m venv .venv
    .venv/bin/python -m ensurepip --upgrade 2>/dev/null || true
  fi
  # 装运行依赖（从 pyproject 提取；本项目不 editable 安装）
  if .venv/bin/python -m pip --version >/dev/null 2>&1; then
    DEPS="$(.venv/bin/python -c "import tomllib,pathlib; print(' '.join(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']))")"
    .venv/bin/python -m pip install -U pip
    .venv/bin/python -m pip install $DEPS "pyinstaller>=6.6.0" "certifi>=2024.2.2"
  elif command -v uv >/dev/null 2>&1; then
    # 本机 venv 由 uv 管理、无 pip：用 uv pip 补装打包工具（运行依赖已在 venv 中）
    VIRTUAL_ENV="$(pwd)/.venv" uv pip install "pyinstaller>=6.6.0" "certifi>=2024.2.2"
  else
    echo "无法安装打包依赖：venv 既无 pip 也无 uv" >&2
    exit 1
  fi
fi
# macOS Dock 壳依赖 pyobjc（幂等，已装则跳过）
if ! .venv/bin/python -c "import AppKit" >/dev/null 2>&1; then
  if .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m pip install "pyobjc-framework-Cocoa>=10.0"
  elif command -v uv >/dev/null 2>&1; then
    VIRTUAL_ENV="$(pwd)/.venv" uv pip install "pyobjc-framework-Cocoa>=10.0"
  fi
fi

echo "==> [3/5] PyInstaller 打包（spec 在 macOS 下同时产出 URStaff.app）"
.venv/bin/pyinstaller ../packaging/ultrarag.spec --noconfirm \
  --distpath ../packaging/out --workpath ../packaging/build
cd "$REPO"
APP="packaging/out/URStaff.app"
test -d "$APP" || { echo "PyInstaller 未产出 $APP"; exit 1; }

echo "==> [4/5] 附带 python 运行时（放 .app/Contents/Resources/runtime）"
# 注意：runtime 必须放 Resources 而非 MacOS。放 MacOS 时 codesign 会把 runtime 里
# 每个文件都当作需签名的代码，附带 python 有大量脚本/符号链接/畸形目录（如 itcl4.2.2），
# 导致顶层签名失败、密封无效（"a sealed resource is missing or invalid"）→ 无法双击打开。
# 放 Resources 后按数据资源密封，顶层签名可通过，app 能正常启动。
python3 packaging/fetch_runtime_python.py packaging/runtime_dl --expect-arch "$ARCH"
rm -rf "$APP/Contents/Resources/runtime" "$APP/Contents/MacOS/runtime"
cp -R packaging/runtime_dl/python "$APP/Contents/Resources/runtime"

echo "==> [5/5] 签名 + 打 dmg"
# arm64 要求 app 至少有 ad-hoc 签名才能启动。runtime 在 Resources，顶层签名可一次通过。
xattr -cr "$APP" 2>/dev/null || true
find "$APP/Contents/Frameworks" -type f -name "*.dylib" 2>/dev/null \
  -exec codesign --force --timestamp=none --sign - {} \; 2>/dev/null || true
codesign --force --timestamp=none --sign - "$APP/Contents/MacOS/staffdeck" 2>/dev/null || true
codesign --force --timestamp=none --sign - "$APP" 2>/dev/null || echo "顶层签名跳过"

# 验证密封（ad-hoc 未被 Gatekeeper 信任属正常，用户首次右键打开即可；但密封必须有效）
if codesign --verify --strict "$APP" 2>/dev/null; then
  echo "✓ 签名密封验证通过（可正常双击打开；未公证故首次可能需右键→打开）"
else
  echo "警告：密封校验未过，双击可能无法打开"
fi

DMG="packaging/out/URStaff-${VERSION}-macos-${ARCH}.dmg"
DMG_ROOT="packaging/out/dmg-root"
rm -f "$DMG"
rm -f "packaging/out/rw."*"URStaff-${VERSION}-macos-${ARCH}.dmg" 2>/dev/null || true
rm -rf "$DMG_ROOT"
mkdir -p "$DMG_ROOT"
ditto "$APP" "$DMG_ROOT/URStaff.app"
ln -s /Applications "$DMG_ROOT/Applications"

if command -v create-dmg >/dev/null 2>&1; then
  LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 create-dmg --volname "URStaff" --window-size 520 320 \
    --app-drop-link 380 170 --icon "URStaff.app" 140 170 \
    --volicon "packaging/assets/staffdeck.icns" \
    "$DMG" "$DMG_ROOT" \
    || hdiutil create -volname URStaff -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG"
else
  hdiutil create -volname URStaff -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG"
fi
rm -rf "$DMG_ROOT"
rm -f "packaging/out/rw."*"URStaff-${VERSION}-macos-${ARCH}.dmg" 2>/dev/null || true
echo "built $DMG"
ls -lh "$DMG"
