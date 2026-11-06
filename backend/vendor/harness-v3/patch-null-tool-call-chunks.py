#!/usr/bin/env python3
"""Patch the upstream deepseek-harness checkout so tool-call continuation chunks with ``id:null`` /
``name:null`` (what OpenAI-compatible gateways stream) do not overwrite the accumulated call
identity. Idempotent. Applies to both the TypeScript source and the built ``lib`` (the runtime
loads ``lib``), so it works whether or not ``pnpm run build:lib`` is re-run afterwards.

Usage: python3 patch-null-tool-call-chunks.py <HARNESS_V3_ROOT>
"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
RULES = [
    # (unpatched pattern, patched replacement) — the patched form never matches the unpatched pattern.
    (re.compile(r"(?<!null\) )block\.callId = call\.id(;?)"), r"if (call.id != null) block.callId = call.id\1"),
    (re.compile(r"(?<!null\) )block\.name = call\.function\.name(;?)"), r"if (call.function?.name != null) block.name = call.function.name\1"),
]


def patch(path: pathlib.Path) -> bool:
    s = path.read_text(encoding="utf-8")
    o = s
    for rx, rep in RULES:
        s = rx.sub(rep, s)
    if s != o:
        path.write_text(s, encoding="utf-8")
        return True
    return False


changed = [str(p) for p in (root / "packages/llm/llm-deepseek/src/translate.ts", root / "packages/llm/llm-deepseek/lib/index.js") if p.exists() and patch(p)]
print(f"patched {len(changed)} file(s)" + (": " + ", ".join(changed) if changed else " (already patched)"))
