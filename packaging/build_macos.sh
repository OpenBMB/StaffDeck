#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
VERSION="${VERSION:-$(cat "$REPO/backend/VERSION" 2>/dev/null || echo 0.0.0-dev)}"
HOST_ARCH="$(uname -m)"
TARGET_ARCH="${STAFFDECK_MACOS_ARCH:-$HOST_ARCH}"
MAC_SIGN_ID="${MAC_SIGN_ID:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"
NOTARY_KEYCHAIN="${NOTARY_KEYCHAIN:-}"

normalize_arch() {
  case "$1" in
    arm64|aarch64) echo "arm64" ;;
    x86_64|amd64|x64) echo "x86_64" ;;
    *) echo "$1" ;;
  esac
}

HOST_ARCH="$(normalize_arch "$HOST_ARCH")"
ARCH="$(normalize_arch "$TARGET_ARCH")"
case "$ARCH" in
  arm64|x86_64) ;;
  *)
    echo "不支持的 macOS 架构: $ARCH（仅支持 arm64 和 x86_64）" >&2
    exit 1
    ;;
esac
if [ "$ARCH" != "$HOST_ARCH" ]; then
  echo "目标架构 $ARCH 与 runner 架构 $HOST_ARCH 不一致；请在原生 runner 上构建" >&2
  exit 1
fi

verify_bundle_arch() {
  local app="$1"
  local checked=0
  local item
  local item_arches
  while IFS= read -r -d '' item; do
    if ! file -b "$item" | grep -q "Mach-O"; then
      continue
    fi
    item_arches="$(lipo -archs "$item")"
    case " $item_arches " in
      *" $ARCH "*) ;;
      *)
        echo "架构校验失败: $item 仅包含 [$item_arches]，期望 $ARCH" >&2
        return 1
        ;;
    esac
    checked=$((checked + 1))
  done < <(find "$app" -type f -print0)
  if [ "$checked" -eq 0 ]; then
    echo "架构校验失败: $app 中未找到 Mach-O 文件" >&2
    return 1
  fi
  echo "✓ 架构校验通过: $checked 个 Mach-O 文件均支持 $ARCH"
}

sign_code() {
  local target="$1"
  if [ -n "$MAC_SIGN_ID" ]; then
    codesign --force --timestamp --options runtime --sign "$MAC_SIGN_ID" "$target"
  else
    codesign --force --timestamp=none --sign - "$target" 2>/dev/null || true
  fi
}

sign_app_bundle() {
  local app="$1"
  xattr -cr "$app" 2>/dev/null || true
  if [ -n "$MAC_SIGN_ID" ]; then
    echo "使用 Developer ID 签名"
    python3 - "$app" <<'PY' | while IFS= read -r item; do
import subprocess
import sys
from pathlib import Path

app = Path(sys.argv[1])
items = []
for path in app.rglob("*"):
    if not path.is_file() or path.is_symlink():
        continue
    try:
        desc = subprocess.check_output(["file", str(path)], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        continue
    if "Mach-O" in desc:
        items.append(path)

for path in sorted(items, key=lambda p: len(p.parts), reverse=True):
    print(path)
PY
      sign_code "$item"
    done
  else
    echo "未配置 MAC_SIGN_ID，使用 ad-hoc 签名"
    find "$app/Contents/Frameworks" -type f -name "*.dylib" 2>/dev/null \
      -exec codesign --force --timestamp=none --sign - {} \; 2>/dev/null || true
    codesign --force --timestamp=none --sign - "$app/Contents/MacOS/staffdeck" 2>/dev/null || true
  fi
  sign_code "$app"
}

echo "==> [1/5] 构建前端"
npm --prefix frontend-enterprise run build

echo "==> [2/5] 后端 venv + 运行依赖 + 打包工具"
cd backend
if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
  .venv/bin/python -m ensurepip --upgrade 2>/dev/null || true
fi
PYTHON_ARCH="$(normalize_arch "$(.venv/bin/python -c 'import platform; print(platform.machine())')")"
if [ "$PYTHON_ARCH" != "$ARCH" ]; then
  echo "backend/.venv 架构为 $PYTHON_ARCH，但本次目标为 $ARCH；请删除该 venv 后重试" >&2
  exit 1
fi
# 每次打包都重新对齐 pyproject 约束。仅在 pyinstaller 缺失时安装会让旧
# .venv 绕过 cryptography/OpenSSL 兼容修复，产出不可重现的发布包。
DEPS="$(.venv/bin/python -c "import tomllib,pathlib; print(' '.join(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']))")"
if .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m pip install -U pip
  # DEPS 由 pyproject 的依赖数组生成，需要按 shell 参数拆分。
  # shellcheck disable=SC2086
  .venv/bin/python -m pip install $DEPS "pyinstaller>=6.6.0" "certifi>=2024.2.2"
elif command -v uv >/dev/null 2>&1; then
  # shellcheck disable=SC2086
  VIRTUAL_ENV="$(pwd)/.venv" uv pip install $DEPS "pyinstaller>=6.6.0" "certifi>=2024.2.2"
else
  echo "无法安装打包依赖：venv 既无 pip 也无 uv" >&2
  exit 1
fi
# macOS Dock 壳和内嵌 UI 依赖 pyobjc（幂等，已装则跳过）
if ! .venv/bin/python -c "import AppKit, WebKit" >/dev/null 2>&1; then
  if .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m pip install \
      "pyobjc-framework-Cocoa>=10.0" \
      "pyobjc-framework-WebKit>=10.0"
  elif command -v uv >/dev/null 2>&1; then
    VIRTUAL_ENV="$(pwd)/.venv" uv pip install \
      "pyobjc-framework-Cocoa>=10.0" \
      "pyobjc-framework-WebKit>=10.0"
  fi
fi

echo "==> [3/5] PyInstaller 打包（spec 在 macOS 下同时产出 StaffDeck.app）"
.venv/bin/pyinstaller ../packaging/ultrarag.spec --noconfirm \
  --distpath ../packaging/out --workpath ../packaging/build
cd "$REPO"
APP="packaging/out/StaffDeck.app"
test -d "$APP" || { echo "PyInstaller 未产出 $APP"; exit 1; }
"$APP/Contents/MacOS/staffdeck" --packaging-smoke

echo "==> [4/5] 附带 python 运行时（放 .app/Contents/Resources/runtime）"
# 注意：runtime 必须放 Resources 而非 MacOS。放 MacOS 时 codesign 会把 runtime 里
# 每个文件都当作需签名的代码，附带 python 有大量脚本/符号链接/畸形目录（如 itcl4.2.2），
# 导致顶层签名失败、密封无效（"a sealed resource is missing or invalid"）→ 无法双击打开。
# 放 Resources 后按数据资源密封，顶层签名可通过，app 能正常启动。
python3 packaging/fetch_runtime_python.py packaging/runtime_dl --expect-arch "$ARCH"
rm -rf "$APP/Contents/Resources/runtime" "$APP/Contents/MacOS/runtime"
cp -R packaging/runtime_dl/python "$APP/Contents/Resources/runtime"

echo "==> [4b/5] 附带 SRT + Node 运行时"
rm -rf packaging/sandbox_runtime "$APP/Contents/Resources/sandbox"
python3 packaging/fetch_sandbox_runtime.py packaging/sandbox_runtime
cp -R packaging/sandbox_runtime "$APP/Contents/Resources/sandbox"
python3 packaging/smoke_sandbox_bundle.py "$APP/Contents/Resources/sandbox"

echo "==> [5/5] 签名 + 打 dmg"
verify_bundle_arch "$APP"
sign_app_bundle "$APP"

if codesign --verify --deep --strict "$APP" 2>/dev/null; then
  echo "✓ 签名密封验证通过"
else
  echo "警告：密封校验未过，双击可能无法打开"
fi
backend/.venv/bin/python packaging/smoke_sandbox_runtime.py "$APP/Contents/Resources/sandbox"

# 在当前 runner 的原生架构上真正启动 PyInstaller App。Intel CI 会在这里
# 捕获 cryptography/OpenSSL ABI 错配，而不是到用户机器上才发现。
bash packaging/smoke_macos_app.sh "$APP"

DMG="packaging/out/StaffDeck-macos-${ARCH}.dmg"
DMG_ROOT="packaging/out/dmg-root"
DMG_BACKGROUND="packaging/build/staffdeck-dmg-background.png"
rm -f "$DMG"
rm -f "packaging/out/rw."*"StaffDeck-macos-${ARCH}.dmg" 2>/dev/null || true
rm -rf "$DMG_ROOT"
mkdir -p "$DMG_ROOT"
ditto "$APP" "$DMG_ROOT/StaffDeck.app"
python3 packaging/make_dmg_background.py "$DMG_BACKGROUND"

if command -v create-dmg >/dev/null 2>&1; then
  LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 create-dmg --volname "StaffDeck" \
    --window-pos 120 100 --window-size 840 360 \
    --background "$DMG_BACKGROUND" \
    --icon-size 96 --text-size 13 \
    --icon "StaffDeck.app" 230 180 \
    --hide-extension "StaffDeck.app" \
    --app-drop-link 610 175 \
    --app-drop-link-name "Applications" \
    --volicon "packaging/assets/staffdeck.icns" \
    --no-internet-enable --overwrite \
    "$DMG" "$DMG_ROOT" \
    || { ln -s /Applications "$DMG_ROOT/Applications"; hdiutil create -volname StaffDeck -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG"; }
else
  ln -s /Applications "$DMG_ROOT/Applications"
  hdiutil create -volname StaffDeck -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG"
fi
rm -rf "$DMG_ROOT"
rm -f "packaging/out/rw."*"StaffDeck-macos-${ARCH}.dmg" 2>/dev/null || true

if [ -n "$MAC_SIGN_ID" ]; then
  codesign --force --timestamp --sign "$MAC_SIGN_ID" "$DMG"
  codesign --verify --strict "$DMG"
fi

if [ -n "$NOTARY_PROFILE" ]; then
  if [ -z "$MAC_SIGN_ID" ]; then
    echo "配置 NOTARY_PROFILE 时也必须配置 MAC_SIGN_ID" >&2
    exit 1
  fi
  NOTARY_ARGS=(--keychain-profile "$NOTARY_PROFILE")
  if [ -n "$NOTARY_KEYCHAIN" ]; then
    NOTARY_ARGS+=(--keychain "$NOTARY_KEYCHAIN")
  fi
  xcrun notarytool submit "$DMG" "${NOTARY_ARGS[@]}" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
  spctl -a -vvv -t open --context context:primary-signature "$DMG"
fi

echo "built $DMG"
ls -lh "$DMG"
