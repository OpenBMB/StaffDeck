#!/usr/bin/env python3
"""Translate agency-agents skill descriptions from English to Chinese.

Targets GeneralSkill rows where metadata.seed_source == 'agency_agents_import'.
Uses the tenant's default ModelConfig via direct OpenAI SDK (bypasses LLMClient
to avoid response wrapping and reduce overhead).

Strategy:
- Batch translations: 5 descriptions per LLM call, returned as JSON array
- Concurrent: 4 worker threads for LLM calls (I/O bound)
- Serial DB writes: avoid SQLite multi-thread write contention
- Idempotent: skips rows already translated (metadata.description_zh present)
- Resumable: commits after each batch
- Fallback: if batch fails, retries individual items

Usage:
    cd /root/data-platform/staffdeck/backend
    .venv/bin/python scripts/translate_agency_agents_descriptions.py [--batch-size N] [--workers N] [--limit N] [--force]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Ensure backend/ is on sys.path so `app.*` imports resolve when run as a script.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from openai import OpenAI  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.database import engine  # noqa: E402
from app.db.models import GeneralSkill, ModelConfig, utc_now  # noqa: E402
from app.security.encryption import decrypt_secret  # noqa: E402


TENANT_ID = "tenant_demo"
SEED_SOURCE = "agency_agents_import"

TRANSLATE_SYSTEM_PROMPT = (
    "You are a professional translator. The user message is a JSON object with an 'items' array. "
    "Each item has 'id' (integer) and 'text' (English). Translate each 'text' to natural, "
    "fluent Simplified Chinese suitable for a software product UI. "
    "Preserve well-known technical terms (API, LLM, SQL, React, etc.) in English. "
    "Do not translate personal/brand names. "
    "Reply with ONLY a JSON object {\"items\":[{\"id\":<int>,\"zh\":\"<chinese>\"}]}. "
    "No markdown, no code fences, no explanations."
)

SINGLE_TRANSLATE_SYSTEM_PROMPT = (
    "You are a professional translator. Translate the user-provided English text into natural, "
    "fluent Simplified Chinese suitable for a software product UI. "
    "Preserve well-known technical terms (API, LLM, SQL, React, etc.) in English. "
    "Do not translate personal/brand names. "
    "Output ONLY the Chinese translation, nothing else."
)


# === LLM client factory ===================================================

def make_client(session: Session) -> tuple[OpenAI, str]:
    """Build a fresh OpenAI client from the default ModelConfig."""
    cfg = session.exec(
        select(ModelConfig).where(ModelConfig.is_default == True, ModelConfig.enabled == True)
    ).first()
    if not cfg:
        raise RuntimeError("No enabled default ModelConfig found")
    api_key = decrypt_secret(cfg.api_key_encrypted)
    return OpenAI(api_key=api_key, base_url=cfg.base_url), cfg.model


# === Batch translation ====================================================

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _strip_code_fence(content: str) -> str:
    match = _CODE_FENCE_RE.search(content)
    return match.group(1) if match else content


def translate_batch(client: OpenAI, model: str, items: list[dict[str, Any]]) -> dict[int, str]:
    """Translate a batch of {id, text} items. Returns {id: chinese_text}.

    Raises on hard failure. Caller should catch and retry individually.
    """
    if not items:
        return {}
    payload = {"items": [{"id": item["id"], "text": item["text"]} for item in items]}
    user_msg = json.dumps(payload, ensure_ascii=False)

    # Estimate max_tokens: ~1.5x input chars in Chinese tokens, plus JSON overhead
    estimated_output = sum(len(item["text"]) for item in items) * 2 + 500
    max_tokens = min(max(2000, estimated_output), 8000)

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": TRANSLATE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=max_tokens,
    )
    content = resp.choices[0].message.content or ""
    content = _strip_code_fence(content).strip()

    parsed = json.loads(content)
    if not isinstance(parsed, dict) or "items" not in parsed:
        raise ValueError(f"Unexpected response shape: keys={list(parsed.keys()) if isinstance(parsed, dict) else type(parsed)}")

    result: dict[int, str] = {}
    for item in parsed["items"]:
        if "id" not in item or "zh" not in item:
            continue
        result[int(item["id"])] = str(item["zh"]).strip()
    return result


def translate_single(client: OpenAI, model: str, text: str) -> str | None:
    """Translate a single text. Returns None on failure."""
    if not text or not text.strip():
        return text
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SINGLE_TRANSLATE_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            max_tokens=2000,
        )
        content = (resp.choices[0].message.content or "").strip().strip('"').strip("'").strip()
        return content or None
    except Exception as exc:
        print(f"  single-translate error ({type(exc).__name__}): {exc}", file=sys.stderr)
        return None


# === Main =================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-size", type=int, default=5, help="Items per LLM call (default 5)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent LLM workers (default 4)")
    parser.add_argument("--limit", type=int, default=0, help="Translate at most N rows (0 = all)")
    parser.add_argument("--force", action="store_true", help="Re-translate even already-translated rows")
    args = parser.parse_args()

    print(f"Mode: {'FORCE' if args.force else 'IDEMPOTENT'} | batch={args.batch_size} | workers={args.workers} | limit={args.limit or 'ALL'}")
    print()

    # Load rows to translate
    with Session(engine) as session:
        rows = session.exec(
            select(GeneralSkill).where(GeneralSkill.tenant_id == TENANT_ID)
        ).all()
        ours = [r for r in rows if (r.metadata_json or {}).get("seed_source") == SEED_SOURCE]
        print(f"Found {len(ours)} agency-agents GeneralSkill rows")

        todo: list[GeneralSkill] = []
        skipped = 0
        for r in ours:
            meta = r.metadata_json or {}
            already = meta.get("description_zh") is not None
            if already and not args.force:
                skipped += 1
            else:
                todo.append(r)
        print(f"  Already translated (skip): {skipped}")
        print(f"  To translate: {len(todo)}")
        if args.limit > 0:
            todo = todo[: args.limit]
            print(f"  Limited to: {len(todo)}")
        print()

        if not todo:
            print("Nothing to do.")
            return 0

        # Build batches (each batch is list of (row_id, original_text))
        batches: list[list[tuple[int, str, str]]] = []  # (row.id, slug, original_text)
        for i in range(0, len(todo), args.batch_size):
            chunk = todo[i : i + args.batch_size]
            batch = []
            for r in chunk:
                meta = r.metadata_json or {}
                original = meta.get("description_en") if args.force else None
                if not original:
                    original = r.description or ""
                batch.append((r.id, r.slug, original))
            batches.append(batch)

        print(f"Built {len(batches)} batches of ~{args.batch_size} items each")
        print()

        # Build shared LLM client (thread-safe per OpenAI SDK; reuse connection pool)
        client, model_name = make_client(session)
        print(f"Using model: {model_name}")
        print()

        # Submit all batches concurrently
        start_time = time.time()
        success_count = 0
        fail_count = 0
        fallback_count = 0
        total_items = sum(len(b) for b in batches)

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(translate_batch, client, model_name, [
                    {"id": i, "text": text} for i, (_, _, text) in enumerate(batch)
                ]): batch
                for batch in batches
            }

            for future_idx, future in enumerate(as_completed(futures), start=1):
                batch = futures[future]
                batch_slug_summary = ", ".join(slug for _, slug, _ in batch[:2])
                if len(batch) > 2:
                    batch_slug_summary += f", ... ({len(batch)} items)"

                try:
                    translations = future.result()
                except Exception as exc:
                    print(f"[batch {future_idx}/{len(batches)}] FAILED: {type(exc).__name__}: {exc}")
                    print(f"  slugs: {batch_slug_summary}")
                    print(f"  → falling back to single-item translation")
                    # Fallback: translate each item individually
                    translations = {}
                    for idx, (row_id, slug, text) in enumerate(batch):
                        single = translate_single(client, model_name, text)
                        if single is not None:
                            translations[idx] = single
                            fallback_count += 1
                        else:
                            fail_count += 1
                            print(f"    single-translate failed: slug={slug}")

                # Apply translations to DB (serial writes)
                with Session(engine) as write_session:
                    for idx, (row_id, slug, original_text) in enumerate(batch):
                        zh = translations.get(idx)
                        if not zh:
                            fail_count += 1
                            print(f"  missing translation for id={idx} slug={slug}")
                            continue
                        row = write_session.get(GeneralSkill, row_id)
                        if row is None:
                            fail_count += 1
                            continue
                        meta = dict(row.metadata_json or {})
                        if "description_en" not in meta:
                            meta["description_en"] = original_text
                        meta["description_zh"] = zh
                        meta["description_translated_at"] = utc_now().isoformat()
                        row.description = zh
                        row.metadata_json = meta
                        row.updated_at = utc_now()
                        write_session.add(row)
                        success_count += 1
                    write_session.commit()

                elapsed = time.time() - start_time
                done_items = success_count + fail_count
                rate = done_items / elapsed if elapsed > 0 else 0
                eta = (total_items - done_items) / rate if rate > 0 else 0
                print(
                    f"[batch {future_idx}/{len(batches)}] ok={len(translations)}/{len(batch)} | "
                    f"total {success_count}/{total_items} | elapsed={elapsed:.0f}s eta={eta:.0f}s | "
                    f"sample: {batch[0][1]} → {translations.get(0, '')[:50]}"
                )

        elapsed = time.time() - start_time
        print()
        print("=== Summary ===")
        print(f"  Total items:    {total_items}")
        print(f"  Success:        {success_count}")
        print(f"  Failed:         {fail_count}")
        print(f"  Fallback used:  {fallback_count}")
        print(f"  Elapsed:        {elapsed:.1f}s ({elapsed/max(total_items, 1):.2f}s/row)")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
