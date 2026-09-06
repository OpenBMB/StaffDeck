from __future__ import annotations

import hashlib
import importlib
import json
import tempfile
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]


def _load_adapter_module():
    return importlib.import_module("app.documents.rapiddoc_adapter")


def _workspace_tempdir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=ROOT_DIR)


def _write_ready_manifest(model_dir: Path) -> None:
    payload = b"1234"
    (model_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": "0.9.10",
                "files": ["weights.bin"],
                "sha256": {"weights.bin": hashlib.sha256(payload).hexdigest()},
            }
        ),
        encoding="utf-8",
    )
    (model_dir / "weights.bin").write_bytes(payload)


class _FakePdfReader:
    def __init__(self, *_args, **_kwargs) -> None:
        self.is_encrypted = False
        self.pages = [object(), object()]


class _FakeEngineType(Enum):
    ONNXRUNTIME = "onnxruntime"


def test_adapter_module_import_is_lazy_when_rapid_doc_is_missing() -> None:
    module = _load_adapter_module()

    assert hasattr(module, "RapidDocStructuredPdfAdapter")


def test_adapter_calls_official_rapid_doc_with_cpu_ort_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_adapter_module()
    captured: dict[str, object] = {}
    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)
        monkeypatch.setenv("RAPID_MODELS_DIR", str(model_dir))
        monkeypatch.setattr("pypdf.PdfReader", _FakePdfReader)

        class FakeRapidDoc:
            def __init__(self, **kwargs):
                captured["init"] = kwargs

            def __call__(self, pdf_bytes, **kwargs):
                captured["pdf_bytes"] = pdf_bytes
                captured["call"] = kwargs
                return SimpleNamespace(
                    content_list_json=[
                        {"page_idx": 0, "type": "text", "text": "alpha"},
                        {
                            "page_idx": 0,
                            "type": "table",
                            "table_body": "<table><tr><td>one</td></tr></table>",
                        },
                        {"page_idx": 1, "type": "text", "text": "beta"},
                    ],
                    warnings=["engine warning"],
                )

        def fake_import(name: str):
            if name == "onnxruntime":
                return object()
            if name == "rapid_doc":
                return SimpleNamespace(RapidDoc=FakeRapidDoc)
            if name == "rapidocr":
                return SimpleNamespace(EngineType=_FakeEngineType)
            raise ImportError(name)

        monkeypatch.setattr(module.importlib, "import_module", fake_import)

        result = module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

        init = captured["init"]
        call = captured["call"]
        assert captured["pdf_bytes"] == b"%PDF-1.4"
        assert init["parse_method"] == "auto"
        assert init["formula_enable"] is False
        assert init["table_enable"] is True
        assert init["pdf_pages_batch"] == 64
        assert init["ocr_config"]["engine_type"] is _FakeEngineType.ONNXRUNTIME
        assert call["f_dump_middle_json"] is False
        assert call["f_dump_content_list"] is False
        assert "alpha" in result.text and "beta" in result.text
        assert "<table>" in result.text
        assert result.engine == "rapid-doc-onnxruntime"
        assert result.source_page_count == 2
        assert result.table_count == 1
        assert result.warnings == ["engine warning"]


def test_adapter_raises_dependency_missing_when_runtime_package_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_adapter_module()
    with _workspace_tempdir() as temp_dir:
        model_dir = Path(temp_dir) / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)
        monkeypatch.setenv("RAPID_MODELS_DIR", str(model_dir))
        monkeypatch.setattr("pypdf.PdfReader", _FakePdfReader)
        monkeypatch.setattr(
            module.importlib,
            "import_module",
            lambda _name: (_ for _ in ()).throw(ImportError("missing rapid-doc")),
        )

        with pytest.raises(module.DocumentExtractionError) as exc_info:
            module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

        assert exc_info.value.code == "OCR_DEPENDENCY_MISSING"


def test_adapter_raises_model_missing_when_model_dir_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_adapter_module()
    with _workspace_tempdir() as temp_dir:
        missing_dir = Path(temp_dir) / "missing-models"
        monkeypatch.setenv("RAPID_MODELS_DIR", str(missing_dir))
        monkeypatch.setattr("pypdf.PdfReader", _FakePdfReader)

        with pytest.raises(module.DocumentExtractionError) as exc_info:
            module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

        assert exc_info.value.code == "OCR_MODEL_MISSING"


def test_adapter_maps_empty_ocr_result_to_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_adapter_module()
    with _workspace_tempdir() as temp_dir:
        model_dir = Path(temp_dir) / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)
        monkeypatch.setenv("RAPID_MODELS_DIR", str(model_dir))
        monkeypatch.setattr("pypdf.PdfReader", _FakePdfReader)

        class FakeRapidDoc:
            def __init__(self, **_kwargs):
                pass

            def __call__(self, *_args, **_kwargs):
                return SimpleNamespace(content_list_json=[], warnings=[])

        def fake_import(name: str):
            if name == "onnxruntime":
                return object()
            if name == "rapid_doc":
                return SimpleNamespace(RapidDoc=FakeRapidDoc)
            if name == "rapidocr":
                return SimpleNamespace(EngineType=_FakeEngineType)
            raise ImportError(name)

        monkeypatch.setattr(module.importlib, "import_module", fake_import)

        with pytest.raises(module.DocumentExtractionError) as exc_info:
            module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

        assert exc_info.value.code == "DOCUMENT_EXTRACTION_FAILED"


def test_adapter_maps_timeout_to_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_adapter_module()
    with _workspace_tempdir() as temp_dir:
        model_dir = Path(temp_dir) / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)
        monkeypatch.setenv("RAPID_MODELS_DIR", str(model_dir))
        monkeypatch.setattr("pypdf.PdfReader", _FakePdfReader)

        class FakeRapidDoc:
            def __init__(self, **_kwargs):
                pass

            def __call__(self, *_args, **_kwargs):
                raise TimeoutError("timed out")

        def fake_import(name: str):
            if name == "onnxruntime":
                return object()
            if name == "rapid_doc":
                return SimpleNamespace(RapidDoc=FakeRapidDoc)
            if name == "rapidocr":
                return SimpleNamespace(EngineType=_FakeEngineType)
            raise ImportError(name)

        monkeypatch.setattr(module.importlib, "import_module", fake_import)

        with pytest.raises(module.DocumentExtractionError) as exc_info:
            module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

        assert exc_info.value.code == "OCR_TIMEOUT"


def test_adapter_rejects_encrypted_pdf_before_calling_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_adapter_module()

    class EncryptedReader(_FakePdfReader):
        def __init__(self, *_args, **_kwargs) -> None:
            super().__init__()
            self.is_encrypted = True

    monkeypatch.setattr("pypdf.PdfReader", EncryptedReader)

    with pytest.raises(module.DocumentExtractionError) as exc_info:
        module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

    assert exc_info.value.code == "PDF_ENCRYPTED"


def test_adapter_maps_corrupted_pdf_to_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pypdf.errors import PdfReadError

    module = _load_adapter_module()
    monkeypatch.setattr(
        "pypdf.PdfReader",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfReadError("broken pdf")),
    )

    with pytest.raises(module.DocumentExtractionError) as exc_info:
        module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

    assert exc_info.value.code == "PDF_CORRUPTED"


def test_model_manager_reports_readiness_without_downloading() -> None:
    module = importlib.import_module("app.documents.model_manager")
    with _workspace_tempdir() as temp_dir:
        model_dir = Path(temp_dir) / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)

        readiness = module.RapidDocModelManager(model_dir=model_dir).check_readiness()

        assert readiness.ready is True
        assert readiness.model_dir == model_dir
        assert readiness.version == "0.9.10"


def test_model_manager_rejects_a_corrupted_model_file() -> None:
    module = importlib.import_module("app.documents.model_manager")
    with _workspace_tempdir() as temp_dir:
        model_dir = Path(temp_dir) / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)
        (model_dir / "weights.bin").write_bytes(b"tampered")

        readiness = module.RapidDocModelManager(model_dir=model_dir).check_readiness()

        assert readiness.ready is False
        assert str(model_dir / "weights.bin") in readiness.missing


def test_model_manager_marks_missing_manifest_as_not_ready() -> None:
    module = importlib.import_module("app.documents.model_manager")
    with _workspace_tempdir() as temp_dir:
        model_dir = Path(temp_dir) / "models"
        model_dir.mkdir()

        readiness = module.RapidDocModelManager(model_dir=model_dir).check_readiness()

        assert readiness.ready is False
        assert readiness.version is None
        assert readiness.missing == [str(model_dir / "manifest.json")]


def test_official_artifact_manifest_includes_runtime_loaded_ocr_classifier() -> None:
    module = importlib.import_module("app.documents.model_manager")

    artifacts = module.rapid_doc_artifacts()
    filenames = {artifact.filename for artifact in artifacts}

    assert "pp_doclayoutv3.onnx" in filenames
    assert "ch_ppocr_mobile_v2.0_cls_mobile.onnx" in filenames
    assert module.rapid_doc_bundled_files()


def test_model_manager_prepare_downloads_curated_offline_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("app.documents.model_manager")
    payload = b"1234"
    artifact = module.RapidDocArtifact(
        filename="pp_doclayoutv3.onnx",
        url="https://example.invalid/pp_doclayoutv3.onnx",
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    monkeypatch.setattr(module.importlib, "import_module", lambda name: object() if name == "onnxruntime" else None)
    monkeypatch.setattr(module, "rapid_doc_artifacts", lambda: (artifact,))
    monkeypatch.setattr(module, "rapid_doc_bundled_files", lambda: ())
    monkeypatch.setattr(module, "rapid_doc_package_version", lambda: "0.9.10")

    def fake_download(spec, destination):
        destination.write_bytes(payload)

    monkeypatch.setattr(module, "download_artifact", fake_download)

    with _workspace_tempdir() as temp_dir:
        model_dir = Path(temp_dir) / "models"
        readiness = module.RapidDocModelManager(model_dir=model_dir).prepare()

        assert readiness.ready is True
        manifest = json.loads((model_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["runtime"] == "onnxruntime"
        assert manifest["files"] == ["pp_doclayoutv3.onnx"]


def test_health_reports_structured_pdf_readiness_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("app.main")
    with _workspace_tempdir() as temp_dir:
        model_dir = Path(temp_dir) / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)
        monkeypatch.setattr(
            main_module,
            "settings",
            type(
                "Settings",
                (),
                {
                    "structured_pdf_enabled": True,
                    "structured_pdf_engine": "rapiddoc",
                    "rapid_models_dir": str(model_dir),
                    "structured_pdf_worker_count": 2,
                },
            )(),
        )

        payload = main_module.health()

    structured_pdf = payload["structured_pdf"]
    assert payload["status"] == "ok"
    assert structured_pdf["ready"] is True
    assert structured_pdf["status"] == "ready"
    assert structured_pdf["manifest_exists"] is True
    assert structured_pdf["missing_count"] == 0
    assert structured_pdf["ocr_worker_count"] == 2
    assert structured_pdf["ocr_worker_status"] == "ready"
