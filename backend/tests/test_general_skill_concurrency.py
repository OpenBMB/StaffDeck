from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from app.db.models import GeneralSkill
from app.general_skills.runner import GeneralSkillRunner
from app.general_skills.schema import GeneralSkillExecutionPlan


def test_same_runner_keeps_parallel_workspaces_isolated(
    monkeypatch, tmp_path: Path
) -> None:
    barrier = threading.Barrier(2)
    observed: dict[str, dict[str, str]] = {}
    observed_lock = threading.Lock()

    monkeypatch.setattr(
        "app.general_skills.runner.ensure_runtime_python",
        lambda: Path(sys.executable),
    )
    monkeypatch.setattr(
        "app.general_skills.runner.runtime_environment",
        lambda env, *, python_path=None: env,
    )

    def fake_sandboxed_process(**kwargs):
        query = str(kwargs["stdin_json"]["query"])
        workspace = Path(kwargs["workspace"])
        artifact_dir = Path(kwargs["stdin_json"]["artifact_dir"])
        barrier.wait(timeout=5)
        artifact = artifact_dir / f"{query}.txt"
        artifact.write_text(query, encoding="utf-8")
        with observed_lock:
            observed[query] = {
                "workspace": str(workspace),
                "cwd": str(kwargs["cwd"]),
                "artifact_dir": str(artifact_dir),
                "env_artifact_dir": str(kwargs["env"]["ARTIFACT_DIR"]),
            }
        payload = {"success": True, "artifacts": [{"path": artifact.name}]}
        encoded = json.dumps(payload).encode()
        return SimpleNamespace(
            returncode=0,
            stdout=encoded,
            stderr=b"",
            timed_out=False,
        )

    monkeypatch.setattr(
        "app.general_skills.runner.run_sandboxed_process",
        fake_sandboxed_process,
    )
    runner = GeneralSkillRunner()
    skill = GeneralSkill(
        tenant_id="tenant-demo",
        slug="parallel",
        name="Parallel",
        skill_markdown="# Parallel",
        status="published",
    )
    plan = GeneralSkillExecutionPlan(
        runtime="python",
        code="print('unused')",
    )
    roots = {name: tmp_path / name for name in ("task-a", "task-b")}

    def execute(name: str):
        return runner._execute_plan(
            skill,
            name,
            plan,
            "user-demo",
            [],
            workspace_root=roots[name],
        )[2]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(zip(roots, pool.map(execute, roots), strict=True))

    assert set(observed) == set(roots)
    for name, root in roots.items():
        data = observed[name]
        workspace = Path(data["workspace"])
        assert workspace.parent == root
        assert Path(data["cwd"]).is_relative_to(workspace)
        assert Path(data["artifact_dir"]).is_relative_to(workspace)
        assert data["env_artifact_dir"] == data["artifact_dir"]
        assert results[name]["artifacts"] == [
            {"path": f"{workspace.name}/artifacts/{name}.txt"}
        ]
        assert (Path(data["artifact_dir"]) / f"{name}.txt").read_text() == name
