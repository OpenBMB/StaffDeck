from __future__ import annotations

import hashlib
import importlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .extraction import DocumentExtractionError


@dataclass(frozen=True)
class RapidDocArtifact:
    filename: str
    url: str
    sha256: str


@dataclass(frozen=True)
class RapidDocModelReadiness:
    ready: bool
    model_dir: Path
    version: str | None
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


_MODEL_CONFIGS = (
    (
        "rapid_doc/model/layout/rapid_layout_self/configs/default_models.yaml",
        "pp_doclayoutv3",
        "model_dir_or_path",
    ),
    ("rapid_doc/model/table/rapid_table_self/default_models.yaml", "paddle_cls", "model_dir_or_path"),
    ("rapid_doc/model/table/rapid_table_self/default_models.yaml", "q_cls", "model_dir_or_path"),
    ("rapid_doc/model/table/rapid_table_self/default_models.yaml", "unet", "model_dir_or_path"),
    ("rapid_doc/model/table/rapid_table_self/default_models.yaml", "slanet_plus", "model_dir_or_path"),
    (
        "rapidocr/default_models.yaml",
        ("onnxruntime", "PP-OCRv4", "cls", "ch_ppocr_mobile_v2.0_cls_mobile"),
        "model_dir",
    ),
)

# RapidDoc 0.9.10 ships these small OCR models in the wheel. They are kept in
# the package manifest rather than downloaded again into the application model
# directory, so preparation remains small and deterministic.
_BUNDLED_FILES = (
    "rapid_doc/resources/PP-OCRv6_det_small.onnx",
    "rapid_doc/resources/PP-OCRv6_rec_small.onnx",
)


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
        if not isinstance(manifest, dict):
            raise DocumentExtractionError(
                "OCR_MODEL_MISSING",
                f"invalid model manifest: {manifest_path}: expected a JSON object",
            )

        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            missing.append(str(manifest_path) + ":files")
            files = []
        checksums = manifest.get("sha256")
        if not isinstance(checksums, dict):
            missing.append(str(manifest_path) + ":sha256")
            checksums = {}
        for relative_path in files:
            candidate = _safe_model_path(model_dir, str(relative_path))
            expected_sha256 = checksums.get(str(relative_path))
            if not isinstance(expected_sha256, str) or not expected_sha256.strip():
                missing.append(str(manifest_path) + f":sha256[{relative_path}]")
            elif candidate is None or not candidate.is_file():
                missing.append(str(model_dir / str(relative_path)))
            elif _sha256_file(candidate).lower() != expected_sha256.lower():
                missing.append(str(candidate))

        bundled_files = manifest.get("bundled_files", [])
        if not isinstance(bundled_files, list):
            missing.append(str(manifest_path) + ":bundled_files")
            bundled_files = []
        if bundled_files:
            try:
                package_root = rapid_doc_package_root()
            except DocumentExtractionError as exc:
                missing.append(exc.message)
            else:
                for relative_path in bundled_files:
                    candidate = _safe_package_path(package_root, str(relative_path))
                    if candidate is None or not candidate.is_file():
                        missing.append(str(relative_path))

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
        except Exception as exc:
            raise DocumentExtractionError(
                "OCR_DEPENDENCY_MISSING",
                f"onnxruntime could not be loaded: {exc}",
            ) from exc

        try:
            artifacts = rapid_doc_artifacts()
            for artifact in artifacts:
                download_artifact(artifact, model_dir / artifact.filename)

            bundled_files = rapid_doc_bundled_files()
            package_root = rapid_doc_package_root()
            missing_bundled = [
                relative_path
                for relative_path in bundled_files
                if _safe_package_path(package_root, relative_path) is None
                or not _safe_package_path(package_root, relative_path).is_file()
            ]
            if missing_bundled:
                raise DocumentExtractionError(
                    "OCR_MODEL_MISSING",
                    "RapidDoc wheel is missing bundled OCR models: " + ", ".join(missing_bundled),
                )

            manifest = {
                "schema_version": 1,
                "runtime": "onnxruntime",
                "package": "rapid-doc",
                "version": rapid_doc_package_version(),
                "files": [artifact.filename for artifact in artifacts],
                "sha256": {artifact.filename: artifact.sha256 for artifact in artifacts},
                "bundled_files": list(bundled_files),
            }
            _write_manifest(model_dir / "manifest.json", manifest)
        except DocumentExtractionError:
            raise
        except Exception as exc:
            raise DocumentExtractionError("DOCUMENT_EXTRACTION_FAILED", str(exc)) from exc

        return self.require_ready()


def rapid_doc_package_root() -> Path:
    try:
        return Path(distribution("rapid-doc").locate_file("rapid_doc"))
    except PackageNotFoundError as exc:
        raise DocumentExtractionError(
            "OCR_DEPENDENCY_MISSING",
            "rapid-doc is not installed; install the project OCR extra first",
        ) from exc


def rapid_doc_package_version() -> str:
    try:
        return version("rapid-doc")
    except PackageNotFoundError:
        return "unknown"


def rapid_doc_artifacts() -> tuple[RapidDocArtifact, ...]:
    package_root = rapid_doc_package_root()
    artifacts: list[RapidDocArtifact] = []
    yaml_module = _yaml_module()
    for relative_config, key, url_key in _MODEL_CONFIGS:
        config_path = package_root.parent / relative_config
        if not config_path.is_file():
            raise DocumentExtractionError(
                "OCR_MODEL_MISSING",
                f"RapidDoc model configuration is missing: {config_path}",
            )
        config = yaml_module.safe_load(config_path.read_text(encoding="utf-8"))
        entry = _config_entry(config, key)
        if not isinstance(entry, dict) or not entry.get(url_key) or not entry.get("SHA256"):
            raise DocumentExtractionError(
                "OCR_MODEL_MISSING",
                f"RapidDoc model configuration entry is invalid: {config_path}#{key}",
            )
        url = str(entry[url_key])
        filename = Path(urlparse(url).path).name
        if not filename:
            raise DocumentExtractionError(
                "OCR_MODEL_MISSING",
                f"RapidDoc model URL has no filename: {url}",
            )
        artifacts.append(
            RapidDocArtifact(
                filename=filename,
                url=url,
                sha256=str(entry["SHA256"]).lower(),
            )
        )
    return tuple(artifacts)


def _config_entry(config: object, key: str | tuple[str, ...]) -> object:
    current = config
    keys = (key,) if isinstance(key, str) else key
    for part in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def rapid_doc_bundled_files() -> tuple[str, ...]:
    return _BUNDLED_FILES


def download_artifact(artifact: RapidDocArtifact, destination: Path) -> None:
    if destination.is_file() and _sha256_file(destination) == artifact.sha256:
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            request = Request(artifact.url, headers={"User-Agent": "StaffDeck/RapidDoc-prep"})
            with urlopen(request, timeout=300) as response:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
        actual_sha256 = _sha256_file(temporary_path)
        if actual_sha256 != artifact.sha256:
            raise DocumentExtractionError(
                "DOCUMENT_EXTRACTION_FAILED",
                f"checksum mismatch for {artifact.filename}: expected {artifact.sha256}, got {actual_sha256}",
            )
        os.replace(temporary_path, destination)
        temporary_path = None
    except DocumentExtractionError:
        raise
    except Exception as exc:
        raise DocumentExtractionError(
            "DOCUMENT_EXTRACTION_FAILED",
            f"failed to download {artifact.filename}: {exc}",
        ) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _yaml_module():
    try:
        return importlib.import_module("yaml")
    except ImportError as exc:
        raise DocumentExtractionError(
            "OCR_DEPENDENCY_MISSING",
            "PyYAML is required to read RapidDoc model metadata",
        ) from exc


def _safe_model_path(model_dir: Path, relative_path: str) -> Path | None:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved = (model_dir / candidate).resolve(strict=False)
    return resolved if resolved.is_relative_to(model_dir.resolve(strict=False)) else None


def _safe_package_path(package_root: Path, relative_path: str) -> Path | None:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    package_dir = package_root.parent.resolve(strict=False)
    resolved = (package_dir / candidate).resolve(strict=False)
    return resolved if resolved.is_relative_to(package_dir) else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.",
            suffix=".part",
            dir=path.parent,
            mode="w",
            encoding="utf-8",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
