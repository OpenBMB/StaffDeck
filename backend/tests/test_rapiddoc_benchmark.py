from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _workspace_tempdir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=ROOT_DIR.parent)


def _valid_report() -> dict[str, object]:
    return {
        "engine": "rapid-doc-onnxruntime",
        "package_bytes": 1,
        "model_bytes": 2,
        "peak_rss_bytes": 3,
        "elapsed_seconds": 4.5,
        "page_count": 2,
        "non_empty_pages": 2,
        "text_chars": 120,
        "table_count": 1,
        "offline_replay": {
            "matched": True,
            "source_sha256": "doc-sha256",
            "page_sha256": ["page-1", "page-2"],
            "char_count": 120,
            "table_count": 1,
            "mismatch_fields": [],
        },
        "warnings": [],
    }


def _raw_probe_result(*, page_sha256: list[str], text_chars: int = 120, table_count: int = 1) -> dict[str, object]:
    return {
        "engine": "rapid-doc-onnxruntime",
        "peak_rss_bytes": 2048,
        "elapsed_seconds": 1.5,
        "page_count": len(page_sha256),
        "non_empty_pages": len(page_sha256),
        "text_chars": text_chars,
        "table_count": table_count,
        "warnings": [],
        "page_sha256": page_sha256,
    }


def test_validate_benchmark_report_accepts_required_schema() -> None:
    benchmark = _load_script("benchmark_rapiddoc")

    report = benchmark.validate_benchmark_report(_valid_report())

    assert report["engine"] == "rapid-doc-onnxruntime"
    assert report["offline_replay"]["matched"] is True


@pytest.mark.parametrize(
    "field",
    [
        "engine",
        "package_bytes",
        "model_bytes",
        "peak_rss_bytes",
        "elapsed_seconds",
        "page_count",
        "non_empty_pages",
        "text_chars",
        "table_count",
        "offline_replay",
        "warnings",
    ],
)
def test_validate_benchmark_report_rejects_missing_required_fields(field: str) -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    report = _valid_report()
    report.pop(field)

    with pytest.raises(ValueError, match=field):
        benchmark.validate_benchmark_report(report)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("package_bytes", -1),
        ("model_bytes", -1),
        ("peak_rss_bytes", -1),
        ("elapsed_seconds", -0.1),
        ("page_count", -1),
        ("non_empty_pages", -1),
        ("text_chars", -1),
        ("table_count", -1),
    ],
)
def test_validate_benchmark_report_rejects_negative_resource_values(
    field: str, value: float
) -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    report = _valid_report()
    report[field] = value

    with pytest.raises(ValueError, match=field):
        benchmark.validate_benchmark_report(report)


def test_validate_benchmark_report_rejects_page_count_mismatches() -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    report = _valid_report()
    report["non_empty_pages"] = 3

    with pytest.raises(ValueError, match="non_empty_pages"):
        benchmark.validate_benchmark_report(report)

    report = _valid_report()
    report["offline_replay"]["page_sha256"] = ["page-1"]

    with pytest.raises(ValueError, match="page_sha256"):
        benchmark.validate_benchmark_report(report)


def test_resolve_pdf_path_rejects_repository_managed_input() -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    repo_pdf = ROOT_DIR / "backend" / "tests" / "fixtures" / "sensitive.pdf"

    with pytest.raises(ValueError, match="outside the repository"):
        benchmark.resolve_pdf_path(repo_pdf)


@pytest.mark.parametrize(
    ("platform", "expected_prefix"),
    [
        ("win32", ["py", "-3.11", "-m", "venv"]),
        ("linux", ["python3.11", "-m", "venv"]),
    ],
)
def test_probe_creation_command_uses_platform_specific_python(platform: str, expected_prefix: list[str]) -> None:
    benchmark = _load_script("benchmark_rapiddoc")

    command = benchmark.build_probe_create_command(ROOT_DIR / ".rapiddoc-probe", platform=platform)

    assert command[: len(expected_prefix)] == expected_prefix


def test_parse_args_allows_internal_probe_without_report_outputs() -> None:
    benchmark = _load_script("benchmark_rapiddoc")

    args = benchmark.parse_args(
        [
            "--internal-probe",
            "--pdf",
            "C:/outside/sample.pdf",
            "--model-dir",
            "C:/outside/models",
            "--probe-json-output",
            "C:/outside/probe.json",
        ]
    )

    assert args.internal_probe is True
    assert args.output is None
    assert args.offline_replay is None


def test_ensure_probe_environment_creates_probe_venv_when_missing() -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    with _workspace_tempdir() as temp_dir:
        probe_dir = Path(temp_dir) / ".rapiddoc-probe"
        commands: list[list[str]] = []

        def fake_runner(command: list[str]) -> None:
            commands.append(command)
            python_path = benchmark.probe_python_path(probe_dir, platform="win32")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")

        python_path = benchmark.ensure_probe_environment(
            probe_dir,
            platform="win32",
            run_command=fake_runner,
        )

        assert commands == [["py", "-3.11", "-m", "venv", str(probe_dir)]]
        assert python_path == benchmark.probe_python_path(probe_dir, platform="win32")


def test_execute_probe_collects_metrics_from_official_rapid_doc_module(monkeypatch: pytest.MonkeyPatch) -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    with _workspace_tempdir() as temp_dir:
        tmp_path = Path(temp_dir)
        pdf_path = tmp_path / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n")
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        captured: dict[str, object] = {"calls": 0}

        def fake_rapid_doc_init(**kwargs):
            captured["init"] = kwargs

            def analyze(pdf_bytes: bytes, **call_kwargs):
                captured["pdf_bytes"] = pdf_bytes
                captured["call"] = call_kwargs
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

            return analyze

        class FakeRapidDocModule:
            RapidDoc = staticmethod(fake_rapid_doc_init)

        class FakeRapidOcrModule:
            EngineType = SimpleNamespace(ONNXRUNTIME="onnxruntime")

        def fake_import(name: str):
            if name == "onnxruntime":
                return object()
            if name == "rapid_doc":
                return FakeRapidDocModule()
            if name == "rapidocr":
                return FakeRapidOcrModule()
            raise ImportError(name)

        monkeypatch.setattr(benchmark.importlib, "import_module", fake_import)
        monkeypatch.setattr(benchmark, "_pdf_page_count", lambda _path: 2)
        perf_values = iter([10.0, 12.5])
        monkeypatch.setattr(benchmark.time, "perf_counter", lambda: next(perf_values))
        monkeypatch.setattr(benchmark, "_peak_rss_bytes", lambda: 4096)

        result = benchmark.execute_probe(pdf_path=pdf_path, model_dir=model_dir, offline=False)

        assert result["page_count"] == 2
        assert result["non_empty_pages"] == 2
        assert result["text_chars"] == len("alpha\n\n<table><tr><td>one</td></tr></table>") + len("beta")
        assert result["table_count"] == 1
        assert result["warnings"] == ["engine warning"]
        assert captured["pdf_bytes"] == b"%PDF-1.4\n"
        init = captured["init"]
        assert init["parse_method"] == "auto"
        assert init["formula_enable"] is False
        assert init["table_enable"] is True
        assert init["pdf_pages_batch"] == 64
        assert init["ocr_config"]["engine_type"] == "onnxruntime"
        call = captured["call"]
        assert call["f_dump_middle_json"] is False
        assert call["f_dump_content_list"] is False


def test_execute_probe_fails_clearly_when_dependency_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    with _workspace_tempdir() as temp_dir:
        tmp_path = Path(temp_dir)
        pdf_path = tmp_path / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n")
        model_dir = tmp_path / "models"
        model_dir.mkdir()

        def raise_import_error(_name: str):
            raise ImportError("missing rapid-doc")

        monkeypatch.setattr(benchmark.importlib, "import_module", raise_import_error)

        with pytest.raises(benchmark.ProbeExecutionError, match="OCR_DEPENDENCY_MISSING"):
            benchmark.execute_probe(pdf_path=pdf_path, model_dir=model_dir, offline=False)


def test_compare_offline_replay_marks_mismatches() -> None:
    benchmark = _load_script("benchmark_rapiddoc")

    offline_replay, warnings = benchmark.compare_offline_replay(
        source_sha256="source-sha",
        primary_result=_raw_probe_result(page_sha256=["a", "b"], text_chars=10, table_count=1),
        replay_result=_raw_probe_result(page_sha256=["a", "c"], text_chars=9, table_count=2),
    )

    assert offline_replay["matched"] is False
    assert offline_replay["mismatch_fields"] == ["page_sha256", "text_chars", "table_count"]
    assert "OFFLINE_REPLAY_MISMATCH: page_sha256,text_chars,table_count" in warnings


def test_main_runs_live_probe_and_offline_replay_against_same_pdf() -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    with tempfile.TemporaryDirectory(dir=ROOT_DIR.parent) as temp_dir:
        tmp_path = Path(temp_dir)
        pdf_path = tmp_path / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\nhello world\n")
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        (model_dir / "weights.bin").write_bytes(b"1234")
        output_path = tmp_path / "benchmark.json"
        replay_path = tmp_path / "offline-replay.json"
        calls: list[bool] = []

        def fake_ensure_probe_environment(*_args, **_kwargs):
            return tmp_path / ".rapiddoc-probe" / "Scripts" / "python.exe"

        def fake_run_probe(*, offline: bool, **_kwargs):
            calls.append(offline)
            return _raw_probe_result(page_sha256=["page-1", "page-2"])

        benchmark.ensure_probe_environment = fake_ensure_probe_environment
        benchmark.run_probe_subprocess = fake_run_probe
        benchmark._probe_site_packages_bytes = lambda _python: 777

        exit_code = benchmark.main(
            [
                "--pdf",
                str(pdf_path),
                "--output",
                str(output_path),
                "--model-dir",
                str(model_dir),
                "--offline-replay",
                str(replay_path),
            ]
        )

        assert exit_code == 0
        assert calls == [False, True]
        report = json.loads(output_path.read_text(encoding="utf-8"))
        replay_dump = json.loads(replay_path.read_text(encoding="utf-8"))
        assert report["package_bytes"] == 777
        assert report["model_bytes"] == 4
        assert report["page_count"] == 2
        assert report["offline_replay"]["matched"] is True
        assert report["offline_replay"]["source_sha256"] == benchmark.sha256_file(pdf_path)
        assert replay_dump["page_sha256"] == ["page-1", "page-2"]


def test_main_reports_offline_replay_mismatch_in_output() -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    with tempfile.TemporaryDirectory(dir=ROOT_DIR.parent) as temp_dir:
        tmp_path = Path(temp_dir)
        pdf_path = tmp_path / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\nhello world\n")
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        output_path = tmp_path / "benchmark.json"
        replay_path = tmp_path / "offline-replay.json"
        results = iter(
            [
                _raw_probe_result(page_sha256=["page-1", "page-2"], text_chars=8, table_count=1),
                _raw_probe_result(page_sha256=["page-1", "page-X"], text_chars=7, table_count=2),
            ]
        )

        benchmark.ensure_probe_environment = lambda *_args, **_kwargs: tmp_path / "probe-python"
        benchmark.run_probe_subprocess = lambda **_kwargs: next(results)
        benchmark._probe_site_packages_bytes = lambda _python: 0

        benchmark.main(
            [
                "--pdf",
                str(pdf_path),
                "--output",
                str(output_path),
                "--model-dir",
                str(model_dir),
                "--offline-replay",
                str(replay_path),
            ]
        )

        report = json.loads(output_path.read_text(encoding="utf-8"))
        assert report["offline_replay"]["matched"] is False
        assert report["offline_replay"]["mismatch_fields"] == [
            "page_sha256",
            "text_chars",
            "table_count",
        ]
        assert "OFFLINE_REPLAY_MISMATCH: page_sha256,text_chars,table_count" in report["warnings"]


def test_main_fails_clearly_when_model_dir_missing() -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    with tempfile.TemporaryDirectory(dir=ROOT_DIR.parent) as temp_dir:
        tmp_path = Path(temp_dir)
        pdf_path = tmp_path / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n")

        with pytest.raises(benchmark.ProbeExecutionError, match="OCR_MODEL_MISSING"):
            benchmark.main(
                [
                    "--pdf",
                    str(pdf_path),
                    "--output",
                    str(tmp_path / "benchmark.json"),
                    "--model-dir",
                    str(tmp_path / "missing-model-dir"),
                    "--offline-replay",
                    str(tmp_path / "offline-replay.json"),
                ]
            )
