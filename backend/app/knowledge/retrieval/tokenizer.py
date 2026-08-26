from __future__ import annotations

import re


def retrieval_terms(text: str) -> list[str]:
    normalized = text.lower()
    latin = re.findall(r"[a-z0-9][a-z0-9_.\-/]*", normalized)
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", normalized)
    chinese = [run for run in chinese_runs]
    for run in chinese_runs:
        chinese.extend(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return latin + chinese
