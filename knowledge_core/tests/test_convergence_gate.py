import importlib.util
from pathlib import Path


def test_missing_phase_evidence_cannot_pass(tmp_path):
    script = Path(__file__).parents[1] / "scripts/convergence_gate.py"
    import sys
    sys.path.insert(0, str(script.parent))
    try:
        spec = importlib.util.spec_from_file_location("convergence_gate", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.evaluate(tmp_path)
        assert result["status"] == "not_accepted"
        assert len(result["gates"]) == 7
        assert all(row["status"] == "not_accepted" for row in result["gates"].values())
    finally:
        sys.path.pop(0)
