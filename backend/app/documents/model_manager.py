from __future__ import annotations

import importlib
import inspect
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
        configured = self._configured_model_dir or os.environ.get("RAPID_MODELS_DIR")
        if not configured:
            configured = str(Path(__file__).resolve().parents[2] / "models" / "rapiddoc")
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
            missing.append(str(manifest_path))
            return RapidDocModelReadiness(
                ready=False,
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

        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            missing.append(str(manifest_path) + ":files")
            files = []
        for relative_path in files:
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
            prepare_call = _resolve_prepare_call(prepare_fn, model_dir)
            prepare_fn(*prepare_call["args"], **prepare_call["kwargs"])
        except Exception as exc:
            raise DocumentExtractionError("DOCUMENT_EXTRACTION_FAILED", str(exc)) from exc

        return self.require_ready()


def _resolve_prepare_call(prepare_fn, model_dir: Path) -> dict[str, tuple[str] | dict[str, str]]:
    try:
        signature = inspect.signature(prepare_fn)
    except (TypeError, ValueError) as exc:
        raise DocumentExtractionError(
            "DOCUMENT_EXTRACTION_FAILED",
            "RapidDoc model prepare API has an unsupported signature",
        ) from exc

    parameters = list(signature.parameters.values())
    if _supports_keyword_model_dir(parameters):
        return {"args": (), "kwargs": {"model_dir": str(model_dir)}}
    if _supports_positional_model_dir(parameters):
        return {"args": (str(model_dir),), "kwargs": {}}
    raise DocumentExtractionError(
        "DOCUMENT_EXTRACTION_FAILED",
        "RapidDoc model prepare API must accept a model_dir argument",
    )


def _supports_keyword_model_dir(parameters: list[inspect.Parameter]) -> bool:
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == "model_dir" and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _supports_positional_model_dir(parameters: list[inspect.Parameter]) -> bool:
    if not parameters:
        return False
    first_parameter = parameters[0]
    return first_parameter.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.VAR_POSITIONAL,
    )
