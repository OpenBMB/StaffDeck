#!/usr/bin/env bash
# Fetch, patch and build the Harness v3 engine (upstream deepseek-harness) into a target directory.
# Used by CI's sealed E2E and by developers setting up a dev stack.
#
#   backend/vendor/harness-v3/fetch-and-build.sh <target-dir> [git-ref]
#
# Requires node >= 22.19 and pnpm (corepack enable). Idempotent: skips the clone when the
# target already has apps/cli/lib/bin.js unless FORCE=1.
set -euo pipefail
TARGET="${1:?target dir}"
REF="${2:-v0.1.2-alpha.2}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "$TARGET/apps/cli/lib/bin.js" && "${FORCE:-0}" != "1" ]]; then
  echo "engine already built at $TARGET"; python3 "$HERE/patch-null-tool-call-chunks.py" "$TARGET"; exit 0
fi
mkdir -p "$(dirname "$TARGET")"
if [[ ! -d "$TARGET/.git" && ! -f "$TARGET/package.json" ]]; then
  git clone --depth 1 --branch "$REF" https://github.com/deepseek-ai/deepseek-harness.git "$TARGET"
fi
cd "$TARGET"
corepack enable >/dev/null 2>&1 || true
pnpm install --frozen-lockfile
pnpm run build:lib
# upstream postinstall drops git hooks into the *parent* repo; never keep them
rm -f "$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null || echo /nonexistent)/lefthook.yml" 2>/dev/null || true
python3 "$HERE/patch-null-tool-call-chunks.py" "$TARGET"
node apps/cli/lib/bin.js --version
