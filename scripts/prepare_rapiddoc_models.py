#!/usr/bin/env python3
"""Explicitly check or prepare StaffDeck's offline RapidDoc model artifacts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.documents.extraction import DocumentExtractionError
from app.documents.model_manager import RapidDocModelManager


def _readiness_payload(readiness) -> dict[str, object]:
    return {
        "ready": readiness.ready,
        "model_dir": str(readiness.model_dir),
        "version": readiness.version,
        "manifest_exists": (readiness.model_dir / "manifest.json").is_file(),
        "missing_count": len(readiness.missing),
        "missing": readiness.missing,
        "warnings": readiness.warnings,
    }


def _print_payload(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _dotenv_model_dir() -> str | None:
    path = Path(os.environ["ULTRARAG_DOTENV"]).expanduser() if os.environ.get("ULTRARAG_DOTENV") else BACKEND_DIR / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != "RAPID_MODELS_DIR":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip() or None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--check-only",
        action="store_true",
        help="inspect the local manifest and files without importing RapidDoc",
    )
    modes.add_argument(
        "--prepare",
        action="store_true",
        help="explicitly ask the installed RapidDoc package to prepare/download models",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=None,
        help="override RAPID_MODELS_DIR (default: backend/models/rapiddoc)",
    )
    args = parser.parse_args(argv)
    model_dir = args.model_dir
    if model_dir is None:
        configured_model_dir = os.environ.get("RAPID_MODELS_DIR") or _dotenv_model_dir()
        model_dir = Path(configured_model_dir) if configured_model_dir else None
    manager = RapidDocModelManager(model_dir=model_dir)
    try:
        readiness = manager.check_readiness() if args.check_only else manager.prepare()
    except DocumentExtractionError as exc:
        _print_payload({"ready": False, "error_code": exc.code, "error": exc.message})
        return 1
    _print_payload(_readiness_payload(readiness))
    return 0 if readiness.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
