from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import rps_downloads


def _client(storage_root: Path, monkeypatch) -> TestClient:  # noqa: ANN001
    monkeypatch.setenv("STAFFDECK_RPS_STORAGE_DIR", str(storage_root))
    app = FastAPI()
    app.include_router(rps_downloads.router)
    return TestClient(app)


def _write_token(
    storage_root: Path,
    token: str,
    *,
    key: str,
    expires_at: float,
) -> None:
    signed_directory = storage_root / ".signed"
    signed_directory.mkdir(parents=True, exist_ok=True)
    (signed_directory / f"{token}.json").write_text(
        json.dumps({"key": key, "expires_at": expires_at}),
        encoding="utf-8",
    )


def test_downloads_valid_signed_package(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "rps-storage"
    package = storage_root / "rps" / "packages" / "html-reports" / "report.html"
    package.parent.mkdir(parents=True)
    package.write_text("<!doctype html><title>Report</title>", encoding="utf-8")
    token = "A234567890abcdef"
    _write_token(
        storage_root,
        token,
        key="rps/packages/html-reports/report.html",
        expires_at=time.time() + 3600,
    )

    response = _client(storage_root, monkeypatch).get(f"/api/rps/download/{token}")

    assert response.status_code == 200
    assert response.content == package.read_bytes()
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in response.headers["content-disposition"]


def test_rejects_expired_signed_token(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "rps-storage"
    token = "B234567890abcdef"
    _write_token(
        storage_root,
        token,
        key="rps/packages/report.html",
        expires_at=time.time() - 1,
    )

    response = _client(storage_root, monkeypatch).get(f"/api/rps/download/{token}")

    assert response.status_code == 410


def test_rejects_invalid_signed_token(tmp_path, monkeypatch) -> None:
    response = _client(tmp_path / "rps-storage", monkeypatch).get(
        "/api/rps/download/short"
    )

    assert response.status_code == 404


def test_rejects_object_path_outside_package_prefix(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "rps-storage"
    outside = tmp_path / "outside.html"
    outside.write_text("outside", encoding="utf-8")
    token = "C234567890abcdef"
    _write_token(
        storage_root,
        token,
        key="rps/packages/../../../outside.html",
        expires_at=time.time() + 3600,
    )

    response = _client(storage_root, monkeypatch).get(f"/api/rps/download/{token}")

    assert response.status_code == 404
