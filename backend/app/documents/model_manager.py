from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .extraction import DocumentExtractionError


@dataclass(frozen=True)
class RapidDocModelReadiness:
    ready: bool
    model_dir: Path
    version: str | None
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


class RapidDocModelManager:
    def __init__(self, model_dir: str | Path | None = None) -> None:
        self._configured_model_dir = model_dir

    def resolve_model_dir(self) -> Path:
        configured = self._configured_model_dir or os.environ.get("RAPID_MODELS_DIR") or "models/rapiddoc"
        return Path(configured).expanduser().resolve(strict=False)

    def check_readiness(self) -> RapidDocModelReadiness:
        model_dir = self.resolve_model_dir()
        warnings: list[str] = []
        missing: list[str] = []

        if not model_dir.is_dir():
            missing.append(str(model_dir))
            return RapidDocModelReadiness(
                ready=False,
                model_dir=model_dir,
                version=None,
                warnings=warnings,
                missing=missing,
            )

        manifest_path = model_dir / "manifest.json"
        if not manifest_path.is_file():
            warnings.append(f"missing manifest: {manifest_path}")
            return RapidDocModelReadiness(
                ready=True,
                model_dir=model_dir,
                version=None,
                warnings=warnings,
                missing=missing,
            )

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise DocumentExtractionError(
                "OCR_MODEL_MISSING",
                f"invalid model manifest: {manifest_path}: {exc}",
            ) from exc

        for relative_path in manifest.get("files", []):
            candidate = model_dir / str(relative_path)
            if not candidate.is_file():
                missing.append(str(candidate))

        return RapidDocModelReadiness(
            ready=not missing,
            model_dir=model_dir,
            version=manifest.get("version"),
            warnings=warnings,
            missing=missing,
        )

    def require_ready(self) -> RapidDocModelReadiness:
        readiness = self.check_readiness()
        if readiness.ready:
            return readiness
        missing = ", ".join(readiness.missing) if readiness.missing else str(readiness.model_dir)
        raise DocumentExtractionError("OCR_MODEL_MISSING", f"model artifacts missing: {missing}")

    def prepare(self) -> RapidDocModelReadiness:
        model_dir = self.resolve_model_dir()
        model_dir.mkdir(parents=True, exist_ok=True)

        try:
            importlib.import_module("onnxruntime")
            rapiddoc = importlib.import_module("rapiddoc")
        except ImportError as exc:
            raise DocumentExtractionError("OCR_DEPENDENCY_MISSING", str(exc)) from exc

        prepare_fn = None
        for attr in ("prepare_models", "download_models", "ensure_models"):
            candidate = getattr(rapiddoc, attr, None)
            if callable(candidate):
                prepare_fn = candidate
                break

        if prepare_fn is None:
            raise DocumentExtractionError(
                "DOCUMENT_EXTRACTION_FAILED",
                "RapidDoc model prepare API is unavailable",
            )

        try:
            prepare_fn(model_dir=str(model_dir))
        except TypeError:
            prepare_fn(str(model_dir))
        except Exception as exc:
            raise DocumentExtractionError("DOCUMENT_EXTRACTION_FAILED", str(exc)) from exc

        return self.require_ready()
