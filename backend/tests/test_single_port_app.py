import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import single_port_app


def test_javascript_assets_override_broken_windows_mime_mapping(tmp_path: Path) -> None:
    original_media_type = mimetypes.guess_type("bundle.js")[0]
    mimetypes.add_type("text/plain", ".js", strict=True)

    try:
        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        (asset_dir / "bundle.js").write_text("export const ready = true;", encoding="utf-8")
        app = FastAPI()
        app.mount("/assets", single_port_app.FrontendStaticFiles(directory=asset_dir))

        response = TestClient(app).head("/assets/bundle.js")

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    finally:
        mimetypes.add_type(original_media_type or "text/javascript", ".js", strict=True)


def test_valid_application_javascript_mapping_is_not_reported_as_correction(
    caplog,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setitem(mimetypes.types_map, ".js", "application/javascript")
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    (asset_dir / "bundle.js").write_text("export const ready = true;", encoding="utf-8")
    app = FastAPI()
    app.mount("/assets", single_port_app.FrontendStaticFiles(directory=asset_dir))

    with caplog.at_level("INFO", logger="staffdeck.static"):
        response = TestClient(app).head("/assets/bundle.js")

    assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    assert "Corrected frontend MIME" not in caplog.text
    assert "Frontend module MIME" not in caplog.text


def test_mime_diagnostic_does_not_record_requested_asset_name(
    caplog,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    sensitive_name = "customer-secret-name.js"
    (asset_dir / sensitive_name).write_text("export {};", encoding="utf-8")
    app = FastAPI()
    app.mount("/assets", single_port_app.FrontendStaticFiles(directory=asset_dir))

    single_port_app.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level("WARNING", logger="staffdeck.static"):
            response = TestClient(app).head(f"/assets/{sensitive_name}")
    finally:
        single_port_app.logger.removeHandler(caplog.handler)

    assert response.status_code == 200
    assert "Corrected frontend MIME suffix=.js" in caplog.text
    assert sensitive_name not in caplog.text
