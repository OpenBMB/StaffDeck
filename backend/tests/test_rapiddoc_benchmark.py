from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

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


def _valid_report() -> dict[str, object]:
    return {
        "engine": "rapiddoc-ort",
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
        },
        "warnings": [],
    }


def test_validate_benchmark_report_accepts_required_schema() -> None:
    benchmark = _load_script("benchmark_rapiddoc")

    report = benchmark.validate_benchmark_report(_valid_report())

    assert report["engine"] == "rapiddoc-ort"
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


def test_main_writes_validated_report_from_explicit_paths() -> None:
    benchmark = _load_script("benchmark_rapiddoc")
    with tempfile.TemporaryDirectory(dir=ROOT_DIR.parent) as temp_dir:
        tmp_path = Path(temp_dir)
        pdf_path = tmp_path / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n")
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        (model_dir / "weights.bin").write_bytes(b"1234")
        replay_path = tmp_path / "offline-replay.json"
        replay_path.write_text(
            json.dumps(
                {
                    "engine": "rapiddoc-ort",
                    "peak_rss_bytes": 512,
                    "elapsed_seconds": 1.25,
                    "page_count": 2,
                    "non_empty_pages": 2,
                    "text_chars": 64,
                    "table_count": 0,
                    "warnings": ["offline replay matched"],
                    "source_sha256": "doc-sha256",
                    "page_sha256": ["page-1", "page-2"],
                }
            ),
            encoding="utf-8",
        )
        output_path = tmp_path / "benchmark.json"

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
        report = json.loads(output_path.read_text(encoding="utf-8"))
        assert report["model_bytes"] == 4
        assert report["page_count"] == 2
        assert report["offline_replay"]["page_sha256"] == ["page-1", "page-2"]
