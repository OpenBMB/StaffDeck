"""桶路由三条确定性路径的命中诊断（只读 + 会写 kroute 缓存）。

回答：为什么某个 query 的桶路由最后走了 LLM（2s+），而不是三条确定性路径之一？

三条路径（app/knowledge/service.py:1010-1130）：
1. ``bucket_route_small_candidate_shortcut``：候选 chunk 总数 ≤
   SMALL_CANDIDATE_CHUNK_LIMIT(50) → 全选，0 次 LLM
2. ``bucket_route_lexical_fast_path``：词法 top-1 ≥ 14 且与 top-2 拉开 ≥ 8，
   且入选桶内 chunk 词法分 ≥ 3.0 → 直接选，0 次 LLM
3. 否则 → Laya / LLM 路由（约 2s）

用法：
    cd backend && .venv/bin/python scripts/probe_bucket_route_paths.py \
        --kb kb_9fa98ee6b2ce4c39 --query "项目有什么娱乐设施"
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import get_session  # noqa: E402
from app.knowledge.schema import KnowledgeSearchRequest  # noqa: E402
from app.knowledge.service import (  # noqa: E402
    LEXICAL_FAST_PATH_CHUNK_FLOOR,
    LEXICAL_FAST_PATH_MIN_GAP,
    LEXICAL_FAST_PATH_MIN_SCORE,
    SMALL_CANDIDATE_CHUNK_LIMIT,
    KnowledgeService,
    _score_buckets_with_scores,
    _score_weighted_text,
)

DEFAULT_TENANT = "tenant_demo"
DEFAULT_KB = "kb_9fa98ee6b2ce4c39"  # Hi Sphere 重庆项目百问百搭（51 chunk）


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    parser.add_argument("--kb", default=DEFAULT_KB)
    parser.add_argument("--query", action="append", default=None)
    parser.add_argument("--max-buckets", type=int, default=4)
    args = parser.parse_args()

    queries = args.query or ["项目有什么娱乐设施"]
    session = next(get_session())
    try:
        service = KnowledgeService(session)
        for query in queries:
            print("=" * 90)
            print(f"query = {query!r}")
            req = KnowledgeSearchRequest(
                tenant_id=args.tenant,
                query=query,
                knowledge_base_ids=[args.kb],
                max_buckets=args.max_buckets,
                max_chunks=8,
            )
            documents = service._load_documents_for_search(req)
            buckets = service._load_buckets_for_search(req, [row.id for row in documents])
            print(f"documents = {len(documents)}  buckets = {len(buckets)}")

            # —— 路径 1：小候选短路 ——
            counts = service._candidate_chunk_counts(
                args.tenant, [row.id for row in buckets]
            )
            chunk_total = sum(counts.values())
            p1 = chunk_total <= SMALL_CANDIDATE_CHUNK_LIMIT
            print(
                f"[路径1 小候选短路] chunk_total={chunk_total} "
                f"阈值={SMALL_CANDIDATE_CHUNK_LIMIT} -> "
                f"{'命中' if p1 else f'不命中（超出 {chunk_total - SMALL_CANDIDATE_CHUNK_LIMIT}）'}"
            )

            # —— 路径 2：词法快速路径 ——
            scored = _score_buckets_with_scores(query, buckets, "answer")
            print(f"[路径2 词法快路径] 有词法分的桶 = {len(scored)}")
            for rank, (score, row) in enumerate(scored[:8], 1):
                print(f"    {rank}. {score:8.2f}  {row.title[:60]}")
            if not scored:
                print("    -> 不命中（没有任何桶达到 SEARCH_MIN_BUCKET_SCORE=2.0）")
            else:
                top = scored[0][0]
                second = scored[1][0] if len(scored) > 1 else 0.0
                gap = top - second
                ok_score = top >= LEXICAL_FAST_PATH_MIN_SCORE
                ok_gap = gap >= LEXICAL_FAST_PATH_MIN_GAP or len(scored) <= 1
                print(
                    f"    top1={top:.2f} (需 ≥{LEXICAL_FAST_PATH_MIN_SCORE}) "
                    f"{'OK' if ok_score else 'FAIL'}"
                    f" | top2={second:.2f} gap={gap:.2f} "
                    f"(需 ≥{LEXICAL_FAST_PATH_MIN_GAP}) {'OK' if ok_gap else 'FAIL'}"
                )
                if ok_score and ok_gap:
                    picked = [row for _s, row in scored[: args.max_buckets]]
                    for row in picked:
                        chunks = service._load_chunks_for_buckets(args.tenant, [row.id], None)
                        best = max(
                            (
                                _score_weighted_text(
                                    query,
                                    (chunk.summary or "", 1.4),
                                    (chunk.content or "", 1.0),
                                )
                                for chunk in chunks
                            ),
                            default=0.0,
                        )
                        verdict = "OK" if best >= LEXICAL_FAST_PATH_CHUNK_FLOOR else "FAIL"
                        print(
                            f"    内容验证 {verdict:4} best_chunk={best:.2f} "
                            f"(需 ≥{LEXICAL_FAST_PATH_CHUNK_FLOOR})  {row.title[:44]}"
                        )
                else:
                    print("    -> 不命中（分数或差距不达标），退回 LLM 路由")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
