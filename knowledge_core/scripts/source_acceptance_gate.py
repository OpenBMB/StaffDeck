"""Require fresh evidence for source deployment and both real Agent workflows."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def evaluate(root):
    gates = {}
    def load(path):
        return json.loads((root / path).read_text())
    def require(condition, message):
        if not condition: raise ValueError(message)
    def passed(path):
        report = load(path)
        require(report.get('status') == 'passed', 'Unsuccessful evidence: ' + path)
        return report
    def check(name, callback):
        try:
            callback(); gates[name] = {'status': 'passed'}
        except (OSError, ValueError, KeyError, TypeError, AssertionError, ET.ParseError) as error:
            gates[name] = {'status': 'not_accepted', 'reason': str(error)}
    def source():
        browser = passed('source-browser/minimal-browser.json')
        required = {'upload', 'task_terminal', 'edit', 'bucket_edit', 'chunk_edit', 'query', 'citation', 'graph_click', 'wiki_edit', 'delete', 'historical_citation', 'version_publish_rollback'}
        require(required <= set(browser['steps']), 'Source workflow incomplete')
        require(not browser['errors'], 'Browser errors')
        passed('source-browser/deletion.json')
        model = passed('source-browser/browser-model.json')
        trace = model['ingestion'] + model['query']['route_trace']
        require(all(not any(word in row['phase'] for word in ('fallback', 'failed', 'invalid')) for row in trace), 'Model fallback')
        for phase in ('bucket_model', 'document_route_model', 'bucket_route_model'):
            require(any(row['phase'] == phase and row.get('call_id') for row in trace), 'Missing inference correlation: ' + phase)
        deployment = passed('deployment/report.json')
        require(not deployment['imports']['app'] and not deployment['imports']['staffdeck_harness'], 'Host imports')
        require(all(name.startswith('km_') or name == 'sqlite_sequence' for name in deployment['tables']), 'Host tables')
        for path in ('source-browser/minimal-desktop.png', 'source-browser/minimal-mobile.png', 'deployment/service.log'):
            require((root / path).is_file(), 'Missing evidence: ' + path)
    def agents():
        sd = passed('sd-agent-management/report.json')
        require(len(sd['turns']) >= 7 and len(sd['tool_evidence']) >= 12, 'Missing SD real tools')
        require(sd['readbacks'][-1]['value']['status'] == 'archived', 'SD deletion not persisted')
        require('Application shutdown complete' in (root / 'sd-agent-management/staffdeck.log').read_text(), 'SD did not shut down')
        pd = passed('pd-agent-management/report.json')
        require(pd['readbacks'] and len(pd['turns']) >= 6, 'Missing PD readbacks')
        for turn in pd['turns']:
            events = turn['events']
            for name in turn['expected']:
                require(any(done['type'] == 'tool_call_finished' and done.get('ok') and any(start['type'] == 'tool_call_started' and start.get('name') == name and start.get('toolCallId') == done.get('toolCallId') for start in events) for done in events), 'Missing PD successful tool: ' + name)
        hosted = passed('pd-plugin/report.json')
        require(hosted['isolation']['staffdeck_service'] is False and hosted['isolation']['staffdeck_source'] is False, 'PD depends on SD')
        require(hosted['correlatedModelCalls'] and hosted['agentTool'], 'Missing PD real model evidence')
        passed('authorization/report.json')
    def browser():
        passed('full-browser/report.json')
        full = passed('iframe-upgrade/report.json')
        require(full['startup'] == 'formal_app_main_with_harness_v3', 'Trimmed lifecycle')
        require(full['upgrade']['status'] == 'passed', 'Upgrade failed')
        require(len(full['coverage']) == 4 and all(value.startswith('real_') for value in full['coverage'].values()), 'Cancel/discovery missing')
        resources = full['staffdeck']['persistence']['hostResources']
        require(resources['tools'] and resources['skills'] and resources['receipts'], 'Missing actual external publication')
        require(not any(value.get('forced') for value in full['lifecycle'].values()), 'Forced exit')
        for surface in ('staffdeck', 'pilotdeck'):
            for viewport in ('desktop', 'mobile'):
                require((root / f'iframe-upgrade/{surface}-main-{viewport}.png').is_file(), 'Missing host screenshot')
    def regressions():
        passed('regression/report.json'); passed('host-regression/report.json')
        for path in ('regression/core.xml', 'regression/compatibility.xml', 'host-regression/sd.xml'):
            suites = list(ET.parse(root / path).getroot().iter('testsuite'))
            require(suites and all(not int(suite.get(name, 0)) for suite in suites for name in ('errors', 'failures', 'skipped')), 'Failed/skipped tests: ' + path)
        pdlog = (root / 'host-regression/pd.log').read_text()
        require('# fail 0' in pdlog and '# skipped 0' in pdlog, 'PD regression missing')
        matrix = passed('recursive-matrix/matrix.json')
        require(matrix['baseline'].startswith('77c21973') and len(matrix['cases']) == 189 and matrix['http_call_count'] == 843, 'Wrong matrix scope')
        require(all(row['status'] == 'passed' and not row['differences'] for row in matrix['cases']), 'Compatibility differences')
        artifact = passed('latest-artifact/isolated-artifact.json')
        require(not artifact['host_source_on_pythonpath'], 'Artifact host dependency')
        passed('latest-artifact/minimal-browser.json')
        lifecycle = load('task-lifecycle/report.json')
        require(lifecycle['ok'] and lifecycle['terminal_payload_cleared'] and lifecycle['partial_documents'] == 0, 'Task lifecycle failed')
    check('source_deployment_and_model', source)
    check('real_agents_and_authorization', agents)
    check('host_browser_publication_upgrade', browser)
    check('regression_compatibility_artifact_lifecycle', regressions)
    return {'status': 'complete' if all(row['status'] == 'passed' for row in gates.values()) else 'not_accepted', 'gates': gates}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    root = parser.parse_args().evidence_dir.resolve()
    report = evaluate(root)
    (root / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    raise SystemExit(0 if report['status'] == 'complete' else 1)
