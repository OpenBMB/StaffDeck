from __future__ import annotations

import re


def retrieval_terms(
    text: str,
    *,
    cjk_ngram: int = 2,
    preserve_identifiers: bool = True,
    deduplicate: bool = False,
    stopwords: set[str] | None = None,
) -> list[str]:
    normalized = text.lower()
    latin_pattern = r"[a-z0-9][a-z0-9_.\-/]*" if preserve_identifiers else r"[a-z0-9]+"
    latin = re.findall(latin_pattern, normalized)
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", normalized)
    chinese = [run for run in chinese_runs]
    for run in chinese_runs:
        chinese.extend(
            run[index : index + cjk_ngram]
            for index in range(max(0, len(run) - cjk_ngram + 1))
        )
    terms = latin + chinese
    if stopwords:
        terms = [term for term in terms if term not in stopwords]
    if not deduplicate:
        return terms
    return list(dict.fromkeys(terms))
