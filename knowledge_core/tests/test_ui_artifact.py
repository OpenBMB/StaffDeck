import json

import pytest
from fastapi.testclient import TestClient

from knowledge_core.api import create_app
from support import full_runtime as Runtime


def test_ui_can_be_replaced_without_host_or_package_rebuild(tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text('<html><body>Replacement KM UI</body></html>')
    (ui / "version.json").write_text(json.dumps({"protocol": "knowledge-frame/v1", "version": "replacement"}))
    with TestClient(create_app(Runtime("sqlite://"), ui_directory=ui)) as client:
        assert "Replacement KM UI" in client.get("/ui/v1/index.html").text
        assert client.get("/ui/v1/version.json").json()["version"] == "replacement"
        assert client.post("/api/km/v1/knowledge-bases", json={"name": "Replacement"}).status_code == 201
    (ui / "version.json").write_text('{"protocol":"incompatible"}')
    runtime = Runtime("sqlite://")
    try:
        with pytest.raises(ValueError, match="Invalid Knowledge UI"):
            create_app(runtime, ui_directory=ui)
    finally:
        runtime.close()
