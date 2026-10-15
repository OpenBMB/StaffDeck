import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "compatibility_matrix.py"
_SPEC = importlib.util.spec_from_file_location("km_compatibility_matrix", _SCRIPT)
assert _SPEC and _SPEC.loader
_MATRIX = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MATRIX)

LEGACY_SDK_COMMIT = _MATRIX.LEGACY_SDK_COMMIT
LEGACY_SDK_FIXTURE = _MATRIX.LEGACY_SDK_FIXTURE
LEGACY_SDK_WIRE = _MATRIX.LEGACY_SDK_WIRE
legacy_sdk_response_contract = _MATRIX.legacy_sdk_response_contract
load_legacy_sdk_fixture = _MATRIX.load_legacy_sdk_fixture


def test_historical_sdk_fixture_is_checked_in_and_complete():
    fixture = load_legacy_sdk_fixture()

    assert fixture["source_commit"] == LEGACY_SDK_COMMIT
    assert fixture["method_count"] == len(LEGACY_SDK_WIRE["methods"]) == 13
    assert len(fixture["responses"]) == 18
    assert {row["status"] for row in fixture["responses"]} == {200, 201, 202, 404}
    assert len(fixture["wire_paths_observed"]) >= len(LEGACY_SDK_WIRE["methods"])


def test_historical_sdk_fixture_has_each_method_contract():
    contracts = legacy_sdk_response_contract(load_legacy_sdk_fixture())

    assert set(contracts) == set(LEGACY_SDK_WIRE["methods"])
    assert contracts["create"]["statuses"] == [201]
    assert contracts["upsert_entries"]["statuses"] == [202]
    assert contracts["archive_document"]["statuses"] == [404]
    assert "metadata" in contracts["create"]["body_keys"]
    assert {"data", "next_cursor"}.issubset(contracts["list"]["body_keys"])


def test_historical_sdk_fixture_rejects_source_drift(monkeypatch):
    original = LEGACY_SDK_FIXTURE.read_text(encoding="utf-8")
    payload = json.loads(original)
    payload["source_commit"] = "drifted"

    class FakePath:
        def read_text(self, encoding):
            return json.dumps(payload)

    monkeypatch.setattr(_MATRIX, "LEGACY_SDK_FIXTURE", FakePath())
    with pytest.raises(RuntimeError, match="source commit drifted"):
        load_legacy_sdk_fixture()
