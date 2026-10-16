import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("recursive_contract", Path(__file__).parents[1] / "scripts/recursive_contract.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_recursive_comparison_preserves_null_types_presence_and_order():
    comparison = MODULE.Comparison()
    differences = comparison.compare({"nested": {"null": None, "number": 1}, "items": [1, 2]},
                                     {"nested": {"number": True}, "items": [2, 1]})
    assert {(row["path"], row["reason"]) for row in differences} == {
        ("$.nested.null", "field_presence"), ("$.nested.number", "type"),
        ("$.items[0]", "value"), ("$.items[1]", "value"),
    }


def test_id_mapping_is_bijective_and_persists_across_responses():
    comparison = MODULE.Comparison()
    assert not comparison.compare({"id": "kdoc_aaa", "parent": "kb_aaa"}, {"id": "kdoc_bbb", "parent": "kb_bbb"})
    assert comparison.compare({"citation": "kdoc_aaa"}, {"citation": "kdoc_ccc"})
    assert comparison.compare({"id": "kdoc_ddd"}, {"id": "kdoc_bbb"})


def test_time_normalization_is_field_limited_and_missing_case_fails():
    comparison = MODULE.Comparison()
    assert not comparison.compare({"created_at": "2020-01-01T00:00:00"}, {"created_at": "2026-01-01T00:00:00"})
    assert comparison.compare({"content": "2020-01-01T00:00:00"}, {"content": "2026-01-01T00:00:00"})
    rows = MODULE.compare_reports({"source_commit": "baseline", "cases": [{"case": "required", "response": {}}]}, {"source_commit": "current", "cases": []})
    assert rows[0]["status"] == "failed"
