from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    script_path = ROOT_DIR / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_adapter_module():
    return importlib.import_module("app.documents.rapiddoc_adapter")


def _workspace_tempdir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=ROOT_DIR)


def _write_ready_manifest(model_dir: Path) -> None:
    (model_dir / "manifest.json").write_text(
        json.dumps({"version": "2026.08.27", "files": ["weights.bin"]}),
        encoding="utf-8",
    )
    (model_dir / "weights.bin").write_bytes(b"1234")


class _FakePdfReader:
    def __init__(self, *_args, **_kwargs) -> None:
        self.is_encrypted = False
        self.pages = [object(), object()]


def test_adapter_module_import_is_lazy_when_rapiddoc_is_missing() -> None:
    module = _load_adapter_module()

    assert hasattr(module, "RapidDocStructuredPdfAdapter")


def test_adapter_calls_doc_analyze_with_expected_cpu_ort_parameters(
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

        def fake_doc_analyze(pdf: str, **kwargs):
            captured["pdf"] = pdf
            captured["kwargs"] = kwargs
            return {
                "pages": [
                    {"text": "alpha", "tables": [{"id": 1}]},
                    {"markdown": "beta", "tables": []},
                ],
                "warnings": ["engine warning"],
            }

        class FakeRapidDocModule:
            doc_analyze = staticmethod(fake_doc_analyze)

        def fake_import(name: str):
            if name == "onnxruntime":
                return object()
            if name == "rapiddoc":
                return FakeRapidDocModule()
            raise ImportError(name)

        monkeypatch.setattr(module.importlib, "import_module", fake_import)

        result = module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

        kwargs = captured["kwargs"]
        assert captured["pdf"].endswith(".pdf")
        assert kwargs["mode"] == "auto"
        assert kwargs["device"] == "cpu"
        assert kwargs["engine"] == "ort"
        assert kwargs["model_dir"] == str(model_dir)
        assert kwargs["table_enable"] is True
        assert kwargs["reading_order"] is True
        assert kwargs["formula_enable"] is False
        assert kwargs["table_formula_enable"] is False
        assert kwargs["extract_images"] is False
        assert kwargs["checkbox_enable"] is False
        assert result.engine == "rapiddoc-ort"
        assert result.table_count == 1
        assert result.warnings == ["engine warning"]


def test_adapter_raises_dependency_missing_when_runtime_package_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_adapter_module()
    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)
        monkeypatch.setenv("RAPID_MODELS_DIR", str(model_dir))
        monkeypatch.setattr("pypdf.PdfReader", _FakePdfReader)

        def raise_import_error(_name: str):
            raise ImportError("missing rapiddoc")

        monkeypatch.setattr(module.importlib, "import_module", raise_import_error)

        with pytest.raises(module.DocumentExtractionError) as exc_info:
            module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

        assert exc_info.value.code == "OCR_DEPENDENCY_MISSING"


def test_adapter_raises_model_missing_when_model_dir_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_adapter_module()
    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        missing_dir = temp_path / "missing-models"
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
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)
        monkeypatch.setenv("RAPID_MODELS_DIR", str(model_dir))
        monkeypatch.setattr("pypdf.PdfReader", _FakePdfReader)

        class FakeRapidDocModule:
            @staticmethod
            def doc_analyze(_pdf: str, **_kwargs):
                return {"pages": [{"text": ""}], "warnings": []}

        def fake_import(name: str):
            if name == "onnxruntime":
                return object()
            if name == "rapiddoc":
                return FakeRapidDocModule()
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
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)
        monkeypatch.setenv("RAPID_MODELS_DIR", str(model_dir))
        monkeypatch.setattr("pypdf.PdfReader", _FakePdfReader)

        class FakeRapidDocModule:
            @staticmethod
            def doc_analyze(_pdf: str, **_kwargs):
                raise TimeoutError("timed out")

        def fake_import(name: str):
            if name == "onnxruntime":
                return object()
            if name == "rapiddoc":
                return FakeRapidDocModule()
            raise ImportError(name)

        monkeypatch.setattr(module.importlib, "import_module", fake_import)

        with pytest.raises(module.DocumentExtractionError) as exc_info:
            module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

        assert exc_info.value.code == "OCR_TIMEOUT"


def test_adapter_rejects_encrypted_pdf_before_calling_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_adapter_module()
    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"
        model_dir.mkdir()
        monkeypatch.setenv("RAPID_MODELS_DIR", str(model_dir))

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
    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"
        model_dir.mkdir()
        monkeypatch.setenv("RAPID_MODELS_DIR", str(model_dir))

        def raise_pdf_read_error(*_args, **_kwargs):
            raise PdfReadError("broken pdf")

        monkeypatch.setattr("pypdf.PdfReader", raise_pdf_read_error)

        with pytest.raises(module.DocumentExtractionError) as exc_info:
            module.RapidDocStructuredPdfAdapter().extract_pdf("scan.pdf", b"%PDF-1.4")

        assert exc_info.value.code == "PDF_CORRUPTED"


def test_model_manager_reports_readiness_without_downloading() -> None:
    module = importlib.import_module("app.documents.model_manager")
    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"
        model_dir.mkdir()
        _write_ready_manifest(model_dir)

        readiness = module.RapidDocModelManager(model_dir=model_dir).check_readiness()

        assert readiness.ready is True
        assert readiness.model_dir == model_dir
        assert readiness.version == "2026.08.27"


def test_model_manager_marks_missing_manifest_as_not_ready() -> None:
    module = importlib.import_module("app.documents.model_manager")
    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"
        model_dir.mkdir()

        readiness = module.RapidDocModelManager(model_dir=model_dir).check_readiness()

        assert readiness.ready is False
        assert readiness.version is None
        assert readiness.missing == [str(model_dir / "manifest.json")]


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


def test_model_manager_prepare_supports_positional_only_prepare_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("app.documents.model_manager")
    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"

        class FakeRapidDocModule:
            @staticmethod
            def prepare_models(model_dir_arg, /):
                target_dir = Path(model_dir_arg)
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "manifest.json").write_text(
                    json.dumps({"version": "2026.08.27", "files": ["weights.bin"]}),
                    encoding="utf-8",
                )
                (target_dir / "weights.bin").write_bytes(b"1234")

        def fake_import(name: str):
            if name == "onnxruntime":
                return object()
            if name == "rapiddoc":
                return FakeRapidDocModule()
            raise ImportError(name)

        monkeypatch.setattr(module.importlib, "import_module", fake_import)

        readiness = module.RapidDocModelManager(model_dir=model_dir).prepare()

        assert readiness.ready is True
        assert readiness.version == "2026.08.27"


def test_model_manager_prepare_does_not_retry_internal_type_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("app.documents.model_manager")
    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"
        calls = 0

        class FakeRapidDocModule:
            @staticmethod
            def prepare_models(*, model_dir: str):
                nonlocal calls
                calls += 1
                raise TypeError("internal bug")

        def fake_import(name: str):
            if name == "onnxruntime":
                return object()
            if name == "rapiddoc":
                return FakeRapidDocModule()
            raise ImportError(name)

        monkeypatch.setattr(module.importlib, "import_module", fake_import)

        with pytest.raises(module.DocumentExtractionError) as exc_info:
            module.RapidDocModelManager(model_dir=model_dir).prepare()

        assert exc_info.value.code == "DOCUMENT_EXTRACTION_FAILED"
        assert calls == 1


def test_prepare_script_check_only_only_checks_without_preparing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script("prepare_rapiddoc_models")
    calls: list[str] = []

    class FakeManager:
        def __init__(self, model_dir: Path | None = None) -> None:
            self.model_dir = model_dir

        def check_readiness(self):
            calls.append("check")
            return type(
                "Readiness",
                (),
                {
                    "ready": True,
                    "model_dir": Path(self.model_dir),
                    "version": "2026.08.27",
                    "warnings": [],
                    "missing": [],
                },
            )()

        def prepare(self):
            calls.append("prepare")
            raise AssertionError("prepare should not run during --check-only")

    monkeypatch.setattr(script, "RapidDocModelManager", FakeManager)

    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        exit_code = script.main(["--check-only", "--model-dir", str(temp_path / "models")])

    assert exit_code == 0
    assert calls == ["check"]


def test_prepare_script_check_only_fails_when_manifest_is_missing() -> None:
    script = _load_script("prepare_rapiddoc_models")
    with _workspace_tempdir() as temp_dir:
        temp_path = Path(temp_dir)
        model_dir = temp_path / "models"
        model_dir.mkdir()

        exit_code = script.main(["--check-only", "--model-dir", str(model_dir)])

    assert exit_code == 1
