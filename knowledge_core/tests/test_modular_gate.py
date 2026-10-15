import importlib.util
from pathlib import Path
import json
import subprocess
import sys

spec = importlib.util.spec_from_file_location('modular_gate', Path(__file__).parents[1] / 'scripts/modular_gate.py')
gate = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
try:
    spec.loader.exec_module(gate)
finally:
    sys.path.pop(0)


def test_missing_evidence_and_older_phase_do_not_complete_modular_acceptance(tmp_path):
    (tmp_path / 'acceptance.json').write_text(json.dumps({'status': 'complete'}))
    result = gate.evaluate(tmp_path)
    assert result['status'] == 'not_accepted'
    assert all(row['status'] == 'not_accepted' for row in result['gates'].values())


def test_declared_pass_with_missing_files_is_rejected(tmp_path):
    folder = tmp_path / 'composition-closeout'
    folder.mkdir()
    (folder / 'full.json').write_text(json.dumps({'status': 'passed', 'violations': [],
        'configuration': {'preset': 'full'}, 'tables': ['km_documents'], 'imports': []}))
    result = gate.evaluate(tmp_path)
    assert result['gates']['full_storage']['status'] == 'not_accepted'
    assert 'missing' in result['gates']['full_storage']['reason']


def test_full_composition_cannot_load_historical_runtime(tmp_path):
    folder = tmp_path / 'composition-closeout'
    folder.mkdir()
    (folder / 'full.json').write_text(json.dumps({'status': 'passed', 'violations': [],
        'configuration': {'preset': 'full'}, 'tables': ['km_documents'], 'imports': ['knowledge_core.models']}))
    result = gate.evaluate(tmp_path)
    assert result['gates']['full_storage']['status'] == 'not_accepted'
    assert 'Legacy runtime loaded' in result['gates']['full_storage']['reason']


def test_recursive_matrix_does_not_accept_a_declared_pass_with_differences(tmp_path):
    folder = tmp_path / 'compatibility-final'
    folder.mkdir()
    (folder / 'matrix.json').write_text(json.dumps({'status': 'passed',
        'baseline': '77c219737a9e8e24d6df39ba73212c60f5851b90', 'http_call_count': 1,
        'cases': [{'status': 'passed', 'differences': [{'path': '$.error.code'}]}] * 189}))
    result = gate.evaluate(tmp_path)
    assert result['gates']['full_compatibility']['status'] == 'not_accepted'
    assert 'Field differences' in result['gates']['full_compatibility']['reason']


def test_optimized_python_still_rejects_missing_artifacts(tmp_path):
    result = subprocess.run([sys.executable, '-O', str(Path(__file__).parents[1] / 'scripts/modular_gate.py'),
        '--evidence-dir', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)['status'] == 'not_accepted'


def test_single_browser_composition_cannot_complete_gate(tmp_path):
    (tmp_path / 'one.json').write_text('{}')
    (tmp_path / 'combination_browser.json').write_text(json.dumps({'status': 'passed', 'checks': [
        {'name': 'minimal', 'status': 'passed', 'evidence': ['one.json']},
    ]}))
    result = gate.evaluate(tmp_path)
    assert result['gates']['combination_browser']['status'] == 'not_accepted'


def test_browser_summary_keeps_previous_compositions_and_reports_missing(tmp_path):
    module_spec = importlib.util.spec_from_file_location('combination_browser', Path(__file__).parents[1] / 'scripts/combination_browser.py')
    browser = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(browser)
    for name in ('minimal', 'catalog_versions'):
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'report.json').write_text(json.dumps({'status': 'passed'}))
        (folder / 'minimal-browser.json').write_text(json.dumps({'status': 'passed', 'errors': []}))
    report = browser.summarize(tmp_path)
    statuses = {row['name']: row['status'] for row in report['checks']}
    assert statuses['minimal'] == statuses['catalog_versions'] == 'passed'
    assert statuses['authentication'] == 'not_executed'
    assert report['status'] == 'not_accepted'
