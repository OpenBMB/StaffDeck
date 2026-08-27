# Task 2 Report

Date: 2026-08-27

Status: completed

## Modified files

- `backend/app/knowledge/parser.py`
- `backend/app/knowledge/service.py`
- `backend/app/knowledge/schema.py`
- `backend/app/knowledge/okf.py`
- `backend/tests/test_knowledge_base.py`
- `backend/tests/test_knowledge_document_extraction.py`

## Red command

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py tests\test_knowledge_document_extraction.py -q
```

## Red failure evidence

Initial Red run failed with 3 targeted gaps:

1. `tests/test_knowledge_base.py::test_native_pdf_ingest_preserves_legacy_text_and_file_type`
   - `KeyError: 'raw_text'`
2. `tests/test_knowledge_document_extraction.py::test_scanned_pdf_ingest_persists_full_text_page_refs_and_provenance`
   - `KeyError: 'raw_text'`
3. `tests/test_knowledge_document_extraction.py::test_failed_ingest_preserves_blob_and_duplicate_source_sha_is_idempotent`
   - `KeyError: 'content_base64'`

These failures confirmed that knowledge ingestion had not yet persisted full extracted text, structured extraction provenance, or failure blob retention.

## Implementation summary

- Kept the legacy `extract_text(filename, content) -> tuple[str, str]` contract intact.
- Added a structured `extract_document(...)` path in `parser.py` so knowledge ingestion can consume full `DocumentExtractionResult` without rewriting Task 1 adapters/contracts.
- Routed knowledge ingestion through structured extraction metadata in `service.py`.
- Persisted full normalized body in `KnowledgeDocument.metadata.raw_text`.
- Persisted extraction provenance in `KnowledgeDocument.metadata.extraction` and source hashes/page refs/version metadata in `KnowledgeDocument.metadata.source`.
- Annotated section tree and chunks with `page_refs`, and carried page refs into evidence-pack citation payloads.
- Reused an existing ready document when both `source_sha256` and `text_sha256` match, preventing duplicate documents, duplicate chunks, and duplicate visible lexical/vector retrieval inputs for repeated uploads.
- Preserved failed job blobs by keeping `content_base64` on failed jobs; only successful or cancelled jobs clear embedded content.
- Updated OKF source metadata serialization so `Source Document`, `Source Section`, and bucket concepts all carry readbackable provenance.
- Documented the readback surface in `schema.py` via existing `metadata` fields rather than inventing a separate extraction model.

## Green / regression / ruff commands

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py tests\test_knowledge_document_extraction.py -q
```

Output:

```text
.............................................                            [100%]
45 passed in 18.86s
```

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m ruff check app\knowledge tests\test_knowledge_document_extraction.py
```

Output:

```text
All checks passed!
```

## Validation details

### Full text persistence

- Verified native PDF ingestion still stores the same legacy parser body and `file_type == "pdf"`.
- Verified structured PDF ingestion stores the entire extracted text in `document.metadata_json["raw_text"]`.
- Verified the stored body is not truncated by preview limits by asserting a sentinel tail string survives end-to-end.

### Page refs

- Verified structured extraction stores `page_refs` on:
  - `KnowledgeDocument.metadata_json["extraction"]`
  - `KnowledgeDocument.metadata_json["source"]`
  - section tree nodes
  - chunk metadata
  - evidence pack payloads
- Verified chunks for page 2 retain `["page:2"]` and keep page-identifiable `source_ref` strings.

### Provenance

- Verified source metadata stores:
  - `source_sha256`
  - `text_sha256`
  - `knowledge_base_version_id`
  - `source_page_count`
  - `page_refs`
- Verified OKF concept `source_refs` can read back the same provenance from existing metadata/source fields.

### Failure retention

- Verified OCR failure leaves job status as `failed`.
- Verified failed job retains `metadata_json["content_base64"]`.
- Verified failure does not fabricate a partial knowledge document when extraction fails before document creation.

### Idempotency

- Verified retrying the same failed job can succeed later without creating duplicate documents.
- Verified re-uploading the same PDF with matching `source_sha256` and `text_sha256` reuses the existing ready document.
- Verified the repeated upload does not increase visible document count or chunk count.

## Unfinished items and risks

- Real RapidDoc installation, model artifact readiness, and benchmark validation remain out of scope for Task 2 and were already noted as unfinished from Task 1.
- The duplicate short-circuit intentionally keys on both `source_sha256` and `text_sha256`; if future requirements need different versioning semantics for same-source/same-text retries, that policy should be made explicit before Task 3 or later ingestion work.

---

## Review fix follow-up (2026-08-27)

Status: completed

### Findings addressed

- Failed ingestion after partial persistence no longer leaves active concept routing artifacts behind.
- Failed ingestion now cleans derived rows (`KnowledgeDiscoverySuggestion`, `KnowledgeConcept`, `KnowledgeChunk`, `KnowledgeBucket`, and any chunk embeddings tied to the failed document) while preserving the failed document row, its stable error, and the original uploaded blob on the job.
- Concept routing now additionally requires either no backing document or a backing `KnowledgeDocument.status == "ready"`, so failed documents are invisible from active concept routing even if a stale row somehow survived.
- Reverted the unrelated `backend/app/knowledge/okf.py` frontmatter/scalar parser behavior changes and kept only the Task 2 provenance/source metadata serialization additions.

### Review-fix Red command

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py tests\test_knowledge_document_extraction.py -q
```

### Review-fix Red failure evidence

The new partial-persistence regression test failed before the fix:

- `tests/test_knowledge_document_extraction.py::test_failed_ingest_after_partial_persistence_cleans_derived_rows_and_hides_concepts`
  - failure: `assert failed_document.bucket_count == 0`
  - actual: `AssertionError: assert 1 == 0`

This proved a later ingestion failure could leave already-persisted derived rows reachable from the failed document window.

### Review-fix implementation summary

- Added a regression path that forces failure after concepts/chunks exist and after a synthetic discovery suggestion is committed.
- Added failure cleanup in `KnowledgeService` so later ingest exceptions delete derived rows but keep the failed document itself for retry/debug visibility.
- Reset failed-document counters/derived metadata after cleanup so the failed row does not advertise ghost buckets/chunks.
- Added a ready-document gate to concept loading for search.
- Restored legacy OKF parser fallback behavior while retaining the Task 2 provenance serialization changes.

### Review-fix verification commands

Focused regression:

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_document_extraction.py -q -k partial_persistence
```

Output:

```text
.                                                                        [100%]
1 passed, 2 deselected in 8.61s
```

Prior knowledge regression:

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py tests\test_knowledge_document_extraction.py -q
```

Output:

```text
..............................................                           [100%]
46 passed in 18.37s
```

Ruff:

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m ruff check app\knowledge tests\test_knowledge_document_extraction.py
```

Output:

```text
All checks passed!
```

### Review-fix validation details

- Full text retention still verified via `document.metadata_json["raw_text"]`.
- Page refs and provenance readback are still verified on document metadata, chunk metadata, and OKF concept `source_refs`.
- Failed partial-persistence ingestion now verifies all derived rows are removed:
  - `KnowledgeDiscoverySuggestion`
  - `KnowledgeConcept`
  - `KnowledgeBucket`
  - `KnowledgeChunk`
- Failed partial-persistence ingestion now verifies failed-document counters are reset to zero and active concept loading returns no rows for that failed document.
- Original blob retention and the earlier idempotency assertions remain in place and still pass.

### Remaining risks

- This fix makes failure atomic from the retrieval/database perspective for the document’s derived rows. If a future implementation moves vector queueing earlier or makes queue dispatch externally transactional, that boundary should be reviewed again so the same atomicity guarantee holds across process boundaries.
