import importlib.util
import json
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("acceptance_gate", Path(__file__).parents[1] / "scripts/acceptance_gate.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_missing_evidence_cannot_complete_acceptance(tmp_path):
    result = MODULE.evaluate(tmp_path)
    assert result["status"] == "not_accepted"
    assert all(row["status"] == "not_accepted" for row in result["gates"].values())


def test_matrix_declared_pass_with_field_difference_is_rejected(tmp_path):
    directory = tmp_path / "compatibility"
    directory.mkdir()
    (directory / "matrix.json").write_text(json.dumps({
        "baseline": "77c21973", "status": "passed", "http_call_count": 1,
        "cases": [{"status": "passed", "differences": [{"path": "$.nested.error"}]}],
    }))
    result = MODULE.evaluate(tmp_path)
    assert result["gates"]["recursive_compatibility"]["status"] == "not_accepted"


def test_skipped_regression_is_not_a_pass(tmp_path):
    directory = tmp_path / "regression"
    directory.mkdir()
    (directory / "report.json").write_text(json.dumps({"status": "passed", "groups": {
        "core": {"status": "passed"}, "compatibility": {"status": "passed"},
    }}))
    (directory / "core.xml").write_text('<testsuites><testsuite tests="1" skipped="1"/></testsuites>')
    result = MODULE.evaluate(tmp_path)
    assert result["gates"]["core_and_artifact"]["status"] == "not_accepted"
    assert "Skipped" in result["gates"]["core_and_artifact"]["reason"]
