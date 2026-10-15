"""This phase requires its own evidence; earlier phase reports cannot complete it."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from acceptance_gate import WORKFLOW

REQUIRED = ('module_compositions', 'combination_browser', 'downgrade', 'docker')
BROWSER_COMPOSITIONS = {'minimal', 'authentication', 'catalog', 'catalog_versions', 'concepts', 'discovery', 'concepts_discovery', 'spaces_catalog_versions'}
BROWSER_STEPS = {'upload', 'task_terminal', 'edit', 'reload', 'query', 'citation', 'bucket_edit', 'chunk_edit', 'desktop', 'mobile', 'no_disabled_requests'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def evaluate(root):
    gates = {}
    def check(name, action):
        try:
            action()
            gates[name] = {'status': 'passed'}
        except (OSError, ValueError, KeyError, TypeError, ET.ParseError, AssertionError, StopIteration, IndexError) as error:
            gates[name] = {'status': 'not_accepted', 'reason': str(error)}
    def tests(path):
        value = ET.parse(root / path).getroot()
        require(list(value.iter('testcase')), 'No executed tests')
        require(not any(list(value.iter(name)) for name in ('failure', 'error', 'skipped')), 'Failed or skipped tests')
    def read(path):
        return json.loads((root / path).read_text())
    def files(folder, names):
        for name in names:
            require((root / folder / name).is_file(), 'Evidence file missing: ' + folder + '/' + name)
    def report(path):
        value = json.loads((root / path).read_text())
        require(value['status'] == 'passed', value.get('reason', 'Report did not pass'))
        require(value.get('checks'), 'Required checks missing')
        if path == 'combination_browser.json':
            require({row.get('name') for row in value['checks']} == BROWSER_COMPOSITIONS, 'Browser compositions missing')
        for row in value['checks']:
            require(row['status'] == 'passed', row.get('reason', 'Check did not pass'))
            files = row.get('evidence', [])
            require(files, 'Check has no evidence')
            require(all((root / file).is_file() for file in files), 'Evidence file missing')
            if path == 'combination_browser.json':
                browser_files = [root / file for file in files if file.endswith('/minimal-browser.json')]
                require(len(browser_files) == 1, 'Browser workflow evidence missing')
                browser = json.loads(browser_files[0].read_text())
                require(browser['status'] == 'passed' and browser.get('errors') == [], 'Browser workflow failed')
                require(BROWSER_STEPS <= set(browser.get('steps', [])), 'Browser workflow steps missing')
                for file in ('minimal-desktop.png', 'minimal-mobile.png', 'service.log'):
                    require((browser_files[0].parent / file).is_file(), 'Browser composition evidence missing')
    def artifact(name, model=False):
        folder = root / name
        value = json.loads((folder / 'isolated-artifact.json').read_text())
        require(value['status'] == 'passed' and value['host_source_on_pythonpath'] is False, 'Artifact is not isolated or did not pass')
        require(Path(value['wheel']).is_file(), 'Wheel missing')
        loaded = json.loads((folder / 'runtime-imports.json').read_text())
        require(loaded and not any(name.startswith(('app.', 'staffdeck.', 'staffdeck_harness.', 'harness.', 'knowledge_core.modules.', 'knowledge_core.full_', 'knowledge_core.service', 'knowledge_core.models', 'knowledge_core.database', 'knowledge_core.migration')) for name in loaded), 'Minimal process loaded host or disabled implementation')
        with ZipFile(value['wheel']) as wheel:
            forbidden = {'service', 'full_runtime', 'full_api', 'okf', 'versions', 'publication'}
            require(not any('knowledge_core/' + name + '.py' in wheel.namelist() for name in forbidden), 'Legacy runtime in product wheel')
        browser = json.loads((folder / 'minimal-browser.json').read_text())
        require(browser['status'] == 'passed' and not browser['errors'], 'Browser did not pass')
        require(BROWSER_STEPS <= set(browser['steps']), 'Browser workflow steps missing')
        for file in ('minimal-desktop.png', 'minimal-mobile.png', 'build.log', 'install.log', 'service.log'):
            require((folder / file).is_file(), 'Artifact evidence missing')
        if model:
            result = json.loads((folder / 'browser-model.json').read_text())
            require(result['status'] == 'passed', 'Real model did not pass')
            for trace in (result['ingestion'], result['query']['route_trace']):
                require(any(row.get('call_id') and row.get('model') for row in trace), 'Model result missing')
                require(not any('fallback' in row['phase'] for row in trace), 'Model fell back')
            require(any(row.get('phase') == 'bucket_model' and row.get('valid_bucket_count', 0) > 0 for row in result['ingestion']), 'Model ingestion result missing')
            for phase in ('document_route_model', 'bucket_route_model'):
                require(any(row.get('phase') == phase and row.get('selected_count', 0) > 0 and row.get('call_id') for row in result['query']['route_trace']), 'Model routing result missing: ' + phase)
    def regression():
        value = read('regression-final/report.json')
        require(value['status'] == 'passed', 'Regression failed')
        for name in ('core', 'compatibility'):
            require(value['groups'][name]['status'] == 'passed' and value['groups'][name]['exit_code'] == value['groups'][name]['collection_exit_code'] == 0, 'Regression command failed')
            tests('regression-final/' + name + '.xml')
            files('regression-final', [name + '.log', name + '-collected.log'])
    def full_artifact():
        folder = 'full-artifact'
        value = read(folder + '/report.json')
        require(value['status'] == 'passed', 'Full artifact failed')
        require(WORKFLOW <= set(value['browser']['workflow']) and value['browser']['pageErrors'] == [], 'Full artifact browser incomplete')
        require(not value['isolation']['host_imports'] and all(name.startswith('km_') for name in value['isolation']['tables']), 'Full artifact storage is not independent')
        require(read(folder + '/runtime-imports.json') == [], 'Full artifact loaded host code')
        files(folder, ['installation.log', 'service.log', 'browser.log', 'independent-desktop.png', 'independent-mobile.png'])
        require('Application shutdown complete' in (root / folder / 'service.log').read_text(), 'Full artifact shutdown missing')
    def storage():
        value = read('composition-closeout/full.json')
        require(value['status'] == 'passed' and not value['violations'], 'Full storage failed')
        require(value['configuration']['preset'] == 'full', 'Full composition missing')
        require(all(name.startswith('km_') for name in value['tables']), 'Legacy tables remain')
        require(not {'knowledge_core.service', 'knowledge_core.models', 'knowledge_core.full_runtime', 'knowledge_core.database', 'knowledge_core.migration'} & set(value['imports']), 'Legacy runtime loaded')
        required = {'km_documents', 'km_catalogs', 'km_versions', 'km_branches', 'km_spaces', 'km_concepts', 'km_suggestions', 'km_publications'}
        require(required <= set(value['tables']), 'Full module storage missing')
    def compatibility():
        value = read('compatibility-final/matrix.json')
        require(value['status'] == 'passed' and value['baseline'] == '77c219737a9e8e24d6df39ba73212c60f5851b90', 'Wrong baseline or failed matrix')
        require(len(value['cases']) >= 189 and value['http_call_count'] > 0, 'Matrix incomplete')
        require(not any(value.get(name) for name in ('gaps', 'missing_required_cases', 'terminal_failures', 'unsuccessful_required_cases')), 'Matrix required cases missing')
        require(all(row['status'] == 'passed' and not row['differences'] for row in value['cases']), 'Field differences remain')
        files('compatibility-final', ['baseline.json', 'current.json', 'baseline.log', 'current.log', 'km-service.log'])
    def browser():
        folder = 'full-browser-current'
        value = read(folder + '/report.json')
        require(value['status'] == 'passed' and not value.get('error') and not value.get('requiredFailures'), 'Full browser failed')
        require(value['startup'] == 'formal_app_main_with_harness_v3', 'Full lifespan missing')
        for host in ('staffdeck', 'pilotdeck'):
            require(WORKFLOW <= set(value[host]['mainWorkflow']['workflow']) and value[host]['requests'], 'Host workflow incomplete')
            files(folder, [host + '-main-desktop.png', host + '-main-mobile.png'])
        require(len(value['coverage']) == 4 and all(row.startswith('real_') for row in value['coverage'].values()), 'Cancel/discovery coverage missing')
        require(all(value['staffdeck']['persistence']['hostResources'].get(name) for name in ('tools', 'skills', 'receipts')), 'Actual Skill/Tool publication missing')
        require(not any(row['forced'] for row in value['lifecycle'].values()), 'Forced host shutdown')
        files(folder, ['staffService.log', 'pilotService.log', 'kmOwnerService.log'])
        require('Application shutdown complete' in (root / folder / 'staffService.log').read_text(), 'Normal StaffDeck shutdown missing')
    def pd_model():
        folder = 'pd-model'
        value = read(folder + '/report.json')
        require(value['status'] == 'passed' and not value.get('pageErrors'), 'PD model failed')
        require(not any(row['forced'] for row in value['lifecycle']), 'PD model forced shutdown')
        require(value['isolation']['staffdeck_source'] is False and value['isolation']['staffdeck_service'] is False and value['isolation']['pilotdeck_from_build'] and value['isolation']['km_from_wheel'], 'PD isolation missing')
        require(WORKFLOW <= set(value['browser']['workflow']), 'PD browser incomplete')
        calls = value['correlatedModelCalls']
        require(calls and len(calls) == len(value['modelCalls']), 'Model correlation missing')
        require(all(row['config_id'] == value['selectedModel']['id'] and row['provider'] and row['model'] and row['result_keys'] for row in calls), 'Actual provider/model result missing')
        finished = next(row for row in value['agentEvents'] if row['type'] == 'tool_call_finished' and row.get('toolName') == 'knowledge_query')
        require(finished['ok'] and finished['data']['evidence_pack'], 'Agent query failed')
        require(not any(any(word in row['phase'] for word in ('fallback', 'failed', 'invalid', 'lexical')) for row in finished['data']['route_trace']), 'Agent model fell back')
        require(any(row['operation'] == 'get_job' and row['result']['status'] == 'succeeded' for row in value['runtime']), 'PD task terminal missing')
        require(len({row['operation'] for row in value['runtime']}) >= 30, 'PD runtime workflow incomplete')
        files(folder, ['pilotdeck-desktop.png', 'pilotdeck-mobile.png', 'km.log', 'pilotdeck.log', 'gateway.log'])
    def upgrade():
        folder = 'iframe-upgrade'
        value = read(folder + '/report.json')
        result = value['upgrade']
        require(value['status'] == result['status'] == 'passed', 'UI upgrade failed')
        require(result['before'] == result['after'] and result['before'], 'Host artifacts changed or evidence missing')
        for host in ('sd', 'pd'):
            require(result['initialVersions'][host]['version'] == 'upgrade-before' and result['afterVersions'][host]['version'] == 'upgrade-after', 'UI version evidence missing')
            require(WORKFLOW <= set(result[host]['workflow']), 'Upgraded workflow incomplete')
            files(folder, [host + '-upgraded-desktop.png', host + '-upgraded-mobile.png'])
    def cleanup():
        value = read('cleanup.json')
        require(value['status'] == 'passed' and not value['violations'], 'Host cleanup failed')
        require(value['host_tables'], 'Host storage evidence missing')
        files('', ['cleanup-schema.log'])
    def task_process():
        value = read('tasks-closeout/report.json')
        require(value['ok'] and value['running_cancel'] and value['hard_restart_interrupted'], 'Actual cancellation/restart missing')
        require(value['partial_documents'] == 0 and value['terminal_payload_cleared'], 'Restart left partial content')
    def publication():
        value = read('publication.json')
        require(value['status'] == 'passed' and len(value['checks']) == 5, 'Publication matrix missing')
    check('core_and_compatibility_regression', regression)
    check('isolated_full_artifact', full_artifact)
    check('pd_runtime', lambda: tests('pd-runtime.xml'))
    check('isolated_lexical', lambda: artifact('isolated-final'))
    check('isolated_direct_model', lambda: artifact('isolated-model-final', True))
    for name in REQUIRED: check(name, lambda name=name: report(name + '.json'))
    for name, action in (('full_storage', storage), ('full_compatibility', compatibility), ('full_lifespan_browser', browser), ('pd_model', pd_model), ('iframe_upgrade', upgrade), ('host_cleanup', cleanup), ('process_task_recovery', task_process), ('publication', publication)):
        check(name, action)
    return {'status': 'complete' if all(row['status'] == 'passed' for row in gates.values()) else 'not_accepted', 'gates': gates}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.evidence_dir)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    (args.evidence_dir / 'acceptance.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    raise SystemExit(0 if result['status'] == 'complete' else 1)
