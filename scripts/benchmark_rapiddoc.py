from __future__ import annotations

import argparse
import json
import site
import sys
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = (
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
)

ROOT_DIR = Path(__file__).resolve().parents[1]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_pdf_path(pdf_path: str | Path, repo_root: Path = ROOT_DIR) -> Path:
    candidate = Path(pdf_path).expanduser().resolve(strict=False)
    if _is_relative_to(candidate, repo_root.resolve()):
        raise ValueError("PDF input must stay outside the repository")
    if not candidate.is_file():
        raise FileNotFoundError(f"PDF input does not exist: {candidate}")
    return candidate


def _read_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"Expected JSON object in {path}")
    return raw


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _current_env_package_bytes() -> int:
    seen: set[Path] = set()
    total = 0
    for site_dir in site.getsitepackages():
        path = Path(site_dir)
        if path in seen:
            continue
        seen.add(path)
        total += _dir_size_bytes(path)
    return total


def _require_non_negative_int(report: dict[str, Any], field: str) -> None:
    value = report.get(field)
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def _require_non_negative_number(report: dict[str, Any], field: str) -> None:
    value = report.get(field)
    if not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{field} must be a non-negative number")


def validate_benchmark_report(report: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in REQUIRED_FIELDS if field not in report]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")

    engine = report["engine"]
    if not isinstance(engine, str) or not engine.strip():
        raise ValueError("engine must be a non-empty string")

    for field in (
        "package_bytes",
        "model_bytes",
        "peak_rss_bytes",
        "page_count",
        "non_empty_pages",
        "text_chars",
        "table_count",
    ):
        _require_non_negative_int(report, field)

    _require_non_negative_number(report, "elapsed_seconds")

    page_count = report["page_count"]
    non_empty_pages = report["non_empty_pages"]
    if non_empty_pages > page_count:
        raise ValueError("non_empty_pages cannot exceed page_count")

    warnings = report["warnings"]
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ValueError("warnings must be a list of strings")

    offline_replay = report["offline_replay"]
    if not isinstance(offline_replay, dict):
        raise TypeError("offline_replay must be an object")

    matched = offline_replay.get("matched")
    if not isinstance(matched, bool):
        raise TypeError("offline_replay.matched must be a boolean")

    source_sha256 = offline_replay.get("source_sha256")
    if not isinstance(source_sha256, str) or not source_sha256.strip():
        raise ValueError("offline_replay.source_sha256 must be a non-empty string")

    page_sha256 = offline_replay.get("page_sha256")
    if not isinstance(page_sha256, list) or not all(
        isinstance(item, str) and item.strip() for item in page_sha256
    ):
        raise ValueError("offline_replay.page_sha256 must be a list of strings")
    if len(page_sha256) != page_count:
        raise ValueError("offline_replay.page_sha256 count must match page_count")

    return report


def build_benchmark_report(
    *,
    replay_payload: dict[str, Any],
    model_dir: Path,
    package_bytes: int | None = None,
) -> dict[str, Any]:
    report = {
        "engine": replay_payload.get("engine"),
        "package_bytes": _current_env_package_bytes() if package_bytes is None else package_bytes,
        "model_bytes": _dir_size_bytes(model_dir),
        "peak_rss_bytes": replay_payload.get("peak_rss_bytes"),
        "elapsed_seconds": replay_payload.get("elapsed_seconds"),
        "page_count": replay_payload.get("page_count"),
        "non_empty_pages": replay_payload.get("non_empty_pages"),
        "text_chars": replay_payload.get("text_chars"),
        "table_count": replay_payload.get("table_count"),
        "offline_replay": {
            "matched": True,
            "source_sha256": replay_payload.get("source_sha256"),
            "page_sha256": replay_payload.get("page_sha256"),
        },
        "warnings": replay_payload.get("warnings", []),
    }
    return validate_benchmark_report(report)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and persist an isolated RapidDoc benchmark")
    parser.add_argument("--pdf", required=True, help="Path to a local PDF outside the repository")
    parser.add_argument("--output", required=True, help="Path to write the benchmark JSON report")
    parser.add_argument("--model-dir", required=True, help="Model directory measured for the probe")
    parser.add_argument(
        "--offline-replay",
        required=True,
        help="JSON payload captured from the isolated offline replay run",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    resolve_pdf_path(args.pdf)
    model_dir = Path(args.model_dir).expanduser().resolve(strict=False)
    output_path = Path(args.output).expanduser().resolve(strict=False)
    replay_path = Path(args.offline_replay).expanduser().resolve(strict=True)

    replay_payload = _read_json(replay_path)
    report = build_benchmark_report(replay_payload=replay_payload, model_dir=model_dir)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
