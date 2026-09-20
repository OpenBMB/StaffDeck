"""Run and compare PilotDeck/StaffDeck parity adapters."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import re
from pathlib import Path
from trace import (
    Difference,
    compare_trace_details,
    load_trace,
    validate_trace_expectations,
    write_report,
)
from typing import Any

ROOT = Path(__file__).resolve().parent


SUITES = {"core-regression", "core-resilience", "staffdeck-workflow", "known-gap"}
PAIRS = {"pilotdeck", "staffdeck"}


def _load_scenarios(
    path: Path,
    selected: str,
    *,
    suite: str = "all",
    pair: str = "all",
) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    scenarios = document.get("scenarios") if isinstance(document, dict) else None
    if not isinstance(scenarios, list):
        raise TypeError("scenario file must contain a scenarios list")
    candidates = [item for item in scenarios if isinstance(item, dict)]
    for item in candidates:
        item_suite = item.get("suite")
        item_pairs = item.get("pairs")
        if item_suite not in SUITES:
            raise ValueError(f"scenario {item.get('scenarioId')} has invalid suite: {item_suite}")
        if not isinstance(item_pairs, list) or not item_pairs or not set(item_pairs) <= PAIRS:
            raise ValueError(f"scenario {item.get('scenarioId')} has invalid pairs: {item_pairs}")
    if selected != "all":
        candidates = [item for item in candidates if item.get("scenarioId") == selected]
        if not candidates:
            raise ValueError(f"unknown scenario: {selected}")
    if suite != "all":
        candidates = [item for item in candidates if item.get("suite") == suite]
    if pair != "all":
        candidates = [item for item in candidates if pair in item.get("pairs", [])]
    return candidates


def _known_gap_matches(scenario: dict[str, Any], differences: list[Difference]) -> bool:
    expected = sorted(str(item) for item in scenario.get("expectedDifferencePaths") or [])
    observed = sorted(item.path for item in differences)
    return bool(expected) and observed == expected


def _declared_semantic_difference_matches(scenario: dict[str, Any], differences: list[Difference]) -> bool:
    """Accept only an explicitly enumerated, adapter-oracle-validated difference."""
    expected = sorted(str(item) for item in scenario.get("declaredSemanticDifferencePaths") or [])
    observed = sorted(
        re.sub(r"^trace\[\d+\]\.(outcome|stopReason)$", r"terminal.\1", item.path)
        for item in differences
    )
    return bool(expected) and observed == expected


def _start_mock() -> tuple[subprocess.Popen[str], str]:
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "mock_backend.py"), "--port", "0"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line:
            payload = json.loads(line)
            return process, f"http://127.0.0.1:{int(payload['port'])}"
        if process.poll() is not None:
            break
    process.kill()
    raise RuntimeError("mock backend did not become ready")


def _worktree(root: Path, ref: str, parent: Path) -> tuple[Path, bool]:
    if ref in {"", "HEAD", "working-tree"}:
        return root, False
    target = parent / f"{root.name}-{ref.replace('/', '_')}"
    result = subprocess.run(["git", "worktree", "add", "--detach", str(target), ref], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"cannot materialize {root} at {ref}: {result.stderr.strip()}")
    dependency_links = [
        (root / "node_modules", target / "node_modules"),
        (root / "backend" / ".venv", target / "backend" / ".venv"),
    ]
    for source, destination in dependency_links:
        if source.exists() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(source, target_is_directory=True)
    return target, True


def _remove_worktree(root: Path, target: Path, created: bool) -> None:
    if created:
        subprocess.run(["git", "worktree", "remove", "--force", str(target)], cwd=root, text=True, capture_output=True, check=False)


def _prepare_baseline(path: Path) -> None:
    if (path / "package.json").exists() and not (path / "dist").exists():
        result = subprocess.run(["pnpm", "build"], cwd=path, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"baseline build failed in {path}: {detail}")


def _adapter_environment(
    *,
    scenario: dict[str, Any],
    mode: str,
    source_root: Path,
    source_ref: str,
    mock_url: str,
    output: Path,
    run_key: str | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    backend_root = source_root / "backend"
    if backend_root.is_dir():
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(backend_root.resolve()), existing_pythonpath) if part
        )
    env.update({
        "PARITY_SCENARIO_FILE": str(ROOT / "scenarios.json"),
        "PARITY_SCENARIO_ID": str(scenario["scenarioId"]),
        "PARITY_Q": str(scenario["q"]),
        "PARITY_SCENARIO_JSON": json.dumps(scenario, ensure_ascii=False),
        "PARITY_MOCK_BASE_URL": mock_url,
        "PARITY_TRACE_OUT": str(output),
        "PARITY_SOURCE_ROOT": str(source_root.resolve()),
        "PARITY_SOURCE_REF": source_ref,
        "PARITY_MODE": mode,
        "PARITY_RUN_KEY": run_key or output.stem,
        "PARITY_PILOTDECK_NATIVE_IMPL": os.environ.get("PARITY_PILOTDECK_NATIVE_IMPL", ""),
        "PARITY_PILOTDECK_SIDECAR_IMPL": os.environ.get("PARITY_PILOTDECK_SIDECAR_IMPL", ""),
        "PARITY_STAFFDECK_LEGACY_IMPL": os.environ.get("PARITY_STAFFDECK_LEGACY_IMPL", ""),
        "PARITY_STAFFDECK_PILOTDECK_IMPL": os.environ.get("PARITY_STAFFDECK_PILOTDECK_IMPL", ""),
    })
    if mode in {"native", "sidecar"}:
        env["PARITY_RUNTIME_ROOT"] = str(
            output.parent / ".runtime" / f"pilotdeck-{scenario['scenarioId']}"
        )
    return env


def _run_adapter(command: str | None, *, scenario: dict[str, Any], mode: str, source_root: Path, source_ref: str, mock_url: str, output: Path, pilotdeck_surface: str = "loop", timeout_seconds: float = 30, run_key: str | None = None) -> str:
    pilotdeck_adapter = (
        ROOT / "adapters" / "pilotdeck_gateway_impl.mjs"
        if pilotdeck_surface == "gateway"
        else None
    )
    implementations = {
        "native": f"node {shlex.quote(str(pilotdeck_adapter or ROOT / 'adapters' / 'pilotdeck_native_impl.mjs'))}",
        "sidecar": (
            f"node {shlex.quote(str(pilotdeck_adapter))}"
            if pilotdeck_adapter
            else f"{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'adapters' / 'pilotdeck_sidecar_impl.py'))}"
        ),
        "legacy": f"{shlex.quote(str(source_root / 'backend' / '.venv' / 'bin' / 'python'))} {shlex.quote(str(ROOT / 'adapters' / 'staffdeck_legacy_impl.py'))}",
        "pilotdeck": f"{shlex.quote(str(source_root / 'backend' / '.venv' / 'bin' / 'python'))} {shlex.quote(str(ROOT / 'adapters' / 'staffdeck_pilotdeck_impl.py'))}",
    }
    command = command or implementations[mode]
    env = _adapter_environment(
        scenario=scenario,
        mode=mode,
        source_root=source_root,
        source_ref=source_ref,
        mock_url=mock_url,
        output=output,
        run_key=run_key,
    )
    try:
        completed = subprocess.run(
            shlex.split(command),
            cwd=source_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        captured = error.stderr or error.stdout or ""
        if isinstance(captured, bytes):
            captured = captured.decode(errors="replace")
        detail = captured.strip().splitlines()[-5:]
        suffix = f": {' | '.join(detail)}" if detail else ""
        return f"BLOCKED: adapter exceeded {timeout_seconds:g}s timeout{suffix}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()[-5:]
        return f"BLOCKED: adapter exited {completed.returncode}: {' | '.join(detail)}"
    if not output.exists():
        return "BLOCKED: adapter exited successfully without writing PARITY_TRACE_OUT"
    return "PASS"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilotdeck-root", type=Path, required=True)
    parser.add_argument("--staffdeck-root", type=Path, required=True)
    parser.add_argument("--pilotdeck-baseline", default="origin/main")
    parser.add_argument("--staffdeck-baseline", default="origin/main")
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--pair", choices=("all", "pilotdeck", "staffdeck"), default="all")
    parser.add_argument("--suite", choices=("all", *sorted(SUITES)), default="all")
    parser.add_argument("--comparison", choices=("same-version", "baseline", "both"), default="same-version")
    parser.add_argument("--pilotdeck-surface", choices=("loop", "gateway"), default="loop")
    parser.add_argument("--scenario-file", type=Path, default=ROOT / "scenarios.json")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--pilotdeck-native-cmd")
    parser.add_argument("--pilotdeck-sidecar-cmd")
    parser.add_argument("--staffdeck-legacy-cmd")
    parser.add_argument("--staffdeck-pilotdeck-cmd")
    parser.add_argument("--allow-blocked", action="store_true")
    parser.add_argument("--adapter-timeout-seconds", type=float, default=30)
    args = parser.parse_args()
    scenarios = _load_scenarios(
        args.scenario_file,
        args.scenario,
        suite=args.suite,
        pair=args.pair,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["PARITY_PILOTDECK_ROOT"] = str(args.pilotdeck_root)
    mock, mock_url = _start_mock()
    blocked: list[str] = []
    failed: list[str] = []
    oracle_failures: list[str] = []
    format_warnings: list[str] = []
    known_gaps: list[str] = []
    declared_semantic_differences: list[str] = []
    baseline_differences: list[str] = []
    skipped_not_applicable: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="agent-loop-parity-") as temp:
            temp_root = Path(temp)
            needs_baseline = args.comparison in {"baseline", "both"}
            pilot_baseline, pilot_created = (
                _worktree(args.pilotdeck_root, args.pilotdeck_baseline, temp_root)
                if needs_baseline
                else (args.pilotdeck_root, False)
            )
            staff_baseline, staff_created = (
                _worktree(args.staffdeck_root, args.staffdeck_baseline, temp_root)
                if needs_baseline
                else (args.staffdeck_root, False)
            )
            try:
                if needs_baseline:
                    _prepare_baseline(pilot_baseline)
                for scenario in scenarios:
                    sid = str(scenario["scenarioId"])
                    traces: dict[str, Path] = {}
                    requested_pairs = PAIRS if args.pair == "all" else {args.pair}
                    applicable_pairs = requested_pairs & set(scenario["pairs"])
                    for skipped_pair in sorted(requested_pairs - applicable_pairs):
                        skipped_not_applicable.append(f"{sid}/{skipped_pair}: SKIPPED_NOT_APPLICABLE")
                    jobs: list[tuple[str, str | None, str, Path, str, str]] = []
                    if "pilotdeck" in applicable_pairs:
                        if args.comparison in {"same-version", "both"}:
                            jobs.extend([
                                ("pilotdeck-native", args.pilotdeck_native_cmd, "native", args.pilotdeck_root, "working-tree", "pilotdeck"),
                                ("pilotdeck-sidecar", args.pilotdeck_sidecar_cmd, "sidecar", args.pilotdeck_root, "working-tree", "pilotdeck"),
                            ])
                        if args.comparison in {"baseline", "both"}:
                            jobs.extend([
                                ("pilotdeck-baseline-native", args.pilotdeck_native_cmd, "native", pilot_baseline, args.pilotdeck_baseline, "pilotdeck"),
                                ("pilotdeck-current-native", args.pilotdeck_native_cmd, "native", args.pilotdeck_root, "working-tree", "pilotdeck"),
                            ])
                    if "staffdeck" in applicable_pairs:
                        if args.comparison in {"same-version", "both"}:
                            jobs.extend([
                                ("staffdeck-legacy", args.staffdeck_legacy_cmd, "legacy", args.staffdeck_root, "working-tree", "staffdeck"),
                                ("staffdeck-pilotdeck", args.staffdeck_pilotdeck_cmd, "pilotdeck", args.staffdeck_root, "working-tree", "staffdeck"),
                            ])
                        if args.comparison in {"baseline", "both"}:
                            jobs.extend([
                                ("staffdeck-baseline-legacy", args.staffdeck_legacy_cmd, "legacy", staff_baseline, args.staffdeck_baseline, "staffdeck"),
                                ("staffdeck-current-legacy", args.staffdeck_legacy_cmd, "legacy", args.staffdeck_root, "working-tree", "staffdeck"),
                            ])
                    unique_jobs = {job[0]: job for job in jobs}
                    for name, command, mode, source_root, source_ref, pair_name in unique_jobs.values():
                        trace_path = args.output / f"{sid}.{name}.jsonl"
                        scenario_surface = str(scenario.get("pilotdeckSurface") or args.pilotdeck_surface)
                        status = _run_adapter(
                            command,
                            scenario=scenario,
                            mode=mode,
                            source_root=source_root,
                            source_ref=source_ref,
                            mock_url=mock_url,
                            output=trace_path,
                            pilotdeck_surface=scenario_surface,
                            timeout_seconds=args.adapter_timeout_seconds,
                            run_key=f"{sid}:{name}",
                        )
                        if status != "PASS":
                            blocked.append(f"{sid}/{name}: {status}")
                        else:
                            traces[name] = trace_path
                            if "baseline-" not in name:
                                failures = validate_trace_expectations(
                                    load_trace(trace_path), scenario, pair_name, name
                                )
                                oracle_failures.extend(
                                    f"{sid}/{name}: {failure.path} expected={failure.left!r} actual={failure.right!r}"
                                    for failure in failures
                                )
                    comparisons = [
                        (f"{sid} PilotDeck", "pilotdeck-native", "pilotdeck-sidecar", False),
                        (f"{sid} StaffDeck", "staffdeck-legacy", "staffdeck-pilotdeck", False),
                        (f"{sid} PilotDeck baseline drift", "pilotdeck-baseline-native", "pilotdeck-current-native", True),
                        (f"{sid} StaffDeck baseline drift", "staffdeck-baseline-legacy", "staffdeck-current-legacy", True),
                    ]
                    for comparison_name, left_name, right_name, is_baseline in comparisons:
                        if left_name not in traces or right_name not in traces:
                            continue
                        comparison = compare_trace_details(load_trace(traces[left_name]), load_trace(traces[right_name]))
                        report_path = args.output / (comparison_name.lower().replace(" ", "-") + ".md")
                        write_report(report_path, comparison_name, traces[left_name], traces[right_name], comparison)
                        if comparison.format_warnings:
                            format_warnings.append(f"{comparison_name}: {len(comparison.format_warnings)} warning(s)")
                        if is_baseline:
                            if comparison.semantic:
                                baseline_differences.append(f"{comparison_name}: {len(comparison.semantic)} semantic difference(s)")
                        elif (
                            scenario.get("declaredSemanticDifferencePaths")
                            and comparison_name.split()[-1].lower() in set(scenario.get("declaredSemanticDifferencePairs") or [])
                        ):
                            if _declared_semantic_difference_matches(scenario, comparison.semantic):
                                declared_semantic_differences.append(
                                    f"{comparison_name}: declared {len(comparison.semantic)} exact difference(s)"
                                )
                            else:
                                failed.append(f"{comparison_name}: declared semantic difference did not match contract")
                        elif scenario["suite"] == "known-gap":
                            if _known_gap_matches(scenario, comparison.semantic):
                                known_gaps.append(f"{comparison_name}: reproduced {len(comparison.semantic)} expected difference(s)")
                            else:
                                failed.append(f"{comparison_name}: known-gap difference did not match declaration")
                        elif comparison.semantic:
                            failed.append(f"{comparison_name}: {len(comparison.semantic)} semantic difference(s)")
            finally:
                _remove_worktree(args.pilotdeck_root, pilot_baseline, pilot_created)
                _remove_worktree(args.staffdeck_root, staff_baseline, staff_created)
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=2)
        except subprocess.TimeoutExpired:
            mock.kill()
    summary = {
        "scenarios": len(scenarios),
        "blocked": blocked,
        "failed": failed,
        "oracleFailures": oracle_failures,
        "knownGaps": known_gaps,
        "declaredSemanticDifferences": declared_semantic_differences,
        "baselineDifferences": baseline_differences,
        "skippedNotApplicable": skipped_not_applicable,
        "formatWarnings": format_warnings,
        "output": str(args.output),
    }
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failed or oracle_failures:
        return 1
    if blocked and not args.allow_blocked:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
