"""Real StaffDeck chat turns with full app.main lifecycle and isolated authenticated KM."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import httpx


def verify_events(report):
    events = report['events_response']
    assert events['status'] == 200
    successful = [row for row in events['body'] if row.get('event_type') == 'capability_invoked' and row.get('data', {}).get('status') == 'completed']
    report['tool_evidence'] = successful
    expected = ['import_document', 'get_job', 'get_document', 'update_document', 'list_document_buckets', 'list_document_chunks', 'update_bucket', 'update_chunk', 'get_concept', 'upsert_concept', 'delete_document', 'resolve_citation']
    for name in expected:
        assert any(row['data'].get('operation') == 'knowledge.' + name + '/v1' for row in successful), 'Missing successful tool event: ' + name
    assert any(row.get('event_type') == 'llm_call_finished' and row.get('data', {}).get('status') == 'success' for row in events['body'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--verify-existing', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output_dir.resolve(); output.mkdir(parents=True, exist_ok=True)
    if args.verify_existing:
        report = json.loads((output / 'report.json').read_text())
        assert report.get('error') == 'AssertionError: Missing successful tool event: import_document'
        verify_events(report)
        report['verification_repair'] = 'Revalidated saved raw events using ledger terminal status completed.'
        report.pop('error'); report['status'] = 'passed'
        (output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        print(json.dumps({'status': 'passed', 'successful_tools': len(report['tool_evidence'])}))
        return 0
    report = {'status': 'not_accepted', 'lifespan': 'app.main:app', 'turns': [], 'readbacks': []}
    credential = json.loads(args.model_config.read_text())['api_key']
    def redact(value):
        if isinstance(value, dict): return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list): return [redact(item) for item in value]
        if isinstance(value, str): return value.replace(credential, '[credential redacted]')
        return value
    processes = []
    with tempfile.TemporaryDirectory(prefix='km-sd-agent-') as temporary:
        directory = Path(temporary)
        env = {key: value for key, value in os.environ.items() if not key.startswith(('KM_', 'STAFFDECK_')) and key != 'PYTHONPATH'}
        env.update(PYTHONPATH=f'{root}/backend:{root}/backend/src:{root}/portable_sop/src:{root}/knowledge_core/src',
            DATABASE_URL=f'sqlite:///{directory}/host.sqlite', APP_SECRET='km-acceptance-only', DEMO_SEED_ENABLED='false',
            STARTUP_ORPHAN_CLEANUP_ENABLED='false', ULTRARAG_DOTENV='/nonexistent-km-acceptance.env',
            HARNESS_V3_ENABLED='true', HARNESS_ADMIN_API_ENABLED='true',
            HARNESS_V3_ROOT='/Users/a1/Desktop/claw/openbmb/deepseek-harness-dsh-v0.1.2-alpha.2',
            HARNESS_V3_HOME=str(directory / 'harness'), HARNESS_RUNTIME_CONFIG_PATH=str(directory / 'harness.json'),
            STAFFDECK_KM_URL='http://127.0.0.1:18896', STAFFDECK_KM_BEARER='isolated-owner',
            KNOWLEDGE_AGENT_METHODS='query,list_documents,get_document,import_document,update_document,delete_document,list_document_buckets,list_document_chunks,get_bucket,get_chunk,update_bucket,update_chunk,get_job,cancel_job,list_concepts,get_concept,upsert_concept,resolve_citation',
            PATH='/Users/a1/.nvm/versions/node/v22.23.1/bin:' + env.get('PATH', ''), NODE_OPTIONS='')
        identity = directory / 'identity.json'
        identity.write_text(json.dumps({'isolated-owner': {'id': 'host', 'tenant_id': 'tenant_demo', 'spaces': ['*'], 'permissions': ['read', 'write', 'history:write']}}))
        config = directory / 'full.json'; config.write_text(json.dumps({'preset': 'full', 'modules': {'authentication': True}}))
        def start(name, command):
            log = (output / (name + '.log')).open('w')
            child = subprocess.Popen(command, env=env, cwd=directory, stdout=log, stderr=subprocess.STDOUT)
            processes.append((child, log)); return child
        def ready(url, child):
            for _ in range(600):
                if child.poll() is not None: raise RuntimeError('Service exited during startup')
                try:
                    if httpx.get(url, timeout=1).is_success: return
                except httpx.HTTPError: pass
                time.sleep(.1)
            raise RuntimeError('Readiness timeout')
        try:
            km = start('km', [sys.executable, '-m', 'knowledge_core.cli', '--config', str(config), '--identity-file', str(identity), '--database-url', f'sqlite:///{directory}/km.sqlite', '--port', '18896'])
            ready('http://127.0.0.1:18896/health/ready', km)
            seed = '''import json,sys
from sqlmodel import Session
from app.db import init_db,engine
from app.db.models import Tenant,User,AgentProfile,ModelConfig,AgentModelBinding
from app.security.auth import hash_password
from app.security.encryption import encrypt_secret
init_db()
model=json.load(open(sys.argv[1]))
with Session(engine) as db:
 db.add(Tenant(id='tenant_demo',name='Acceptance'))
 db.add(User(id='owner',tenant_id='tenant_demo',username='owner',role='admin',password_hash=hash_password('temporary-acceptance')))
 db.add(AgentProfile(id='employee',tenant_id='tenant_demo',name='KM Acceptance',is_overall=False,harness_max_actions=60,metadata_json={'owner_user_id':'owner'}))
 db.add(ModelConfig(id='acceptance',tenant_id='tenant_demo',name='Real acceptance',provider='openai_compatible',base_url=model['base_url'],api_key_encrypted=encrypt_secret(model['api_key']),model=model['model'],trust_status='legacy_trusted',is_default=True,max_output_tokens=8192))
 db.add(AgentModelBinding(tenant_id='tenant_demo',agent_id='employee',model_config_id='acceptance'))
 db.commit()
'''
            subprocess.run([sys.executable, '-c', seed, str(args.model_config.resolve())], env=env, cwd=directory, check=True, capture_output=True)
            headers = {'Authorization': 'Bearer isolated-owner'}
            with httpx.Client(base_url=env['STAFFDECK_KM_URL'] + '/api/km/v1', headers=headers, timeout=180) as client:
                space = 'sd:tenant_demo:employee'
                client.post('/spaces', json={'id': space, 'name': 'SD Agent'}).raise_for_status()
                base = client.post('/knowledge-bases', json={'space_id': space, 'name': 'SD Agent management'}).json()['id']
                binding = '''from sqlmodel import Session
from app.db import engine
from app.db.models import AgentResourceBinding
import sys
with Session(engine) as db:
 db.add(AgentResourceBinding(tenant_id='tenant_demo',agent_id='employee',resource_type='knowledge_base',resource_id=sys.argv[1]));db.commit()
'''
                subprocess.run([sys.executable, '-c', binding, base], env=env, cwd=directory, check=True, capture_output=True)
                sd = start('staffdeck', [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '18897'])
                ready('http://127.0.0.1:18897/api/health', sd)
                with httpx.Client(base_url='http://127.0.0.1:18897', timeout=600) as host:
                    login = host.post('/api/auth/login', json={'tenant_id': 'tenant_demo', 'username': 'owner', 'password': 'temporary-acceptance'})
                    login.raise_for_status(); host.headers['Authorization'] = 'Bearer ' + login.json()['token']
                    session = None
                    def turn(message):
                        nonlocal session
                        response = host.post('/api/chat/turn', json={'tenant_id': 'tenant_demo', 'agent_id': 'employee', 'session_id': session, 'message': message})
                        if not response.is_success:
                            report['turns'].append({'message': message, 'status': response.status_code, 'response': response.json()})
                        response.raise_for_status(); result = redact(response.json()); session = result['session_id']
                        report['turns'].append({'message': message, 'response': result})
                        (output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
                        if result.get('runtime_error_code'): raise RuntimeError(result['runtime_error_code'])
                    scope = {'space_id': space, 'knowledge_base_id': base}
                    def read(path):
                        response = client.get(path, params=scope); response.raise_for_status()
                        report['readbacks'].append({'path': path, 'value': response.json()}); return response.json()
                    turn(f'请使用知识管理工具在知识库 {base} 新增文档，标题 Agent receipts，Markdown：# Agent receipts\n\nSDAGENT731 requires original receipts within 30 days. 等待知识入库任务 succeeded 后再报告完成。')
                    documents = read('/documents'); assert len(documents) == 1, documents
                    document = documents[0]; document_id = document['id']
                    turn(f'查询知识库 {base} 的 SDAGENT731 规则，解析返回的引用来源后回答。')
                    turn(f'读取知识库 {base} 的文档 {document_id} 当前修订，再将正文改为 # Agent receipts\n\nSDAGENT731 requires original receipts within 45 days. 必须使用实际读到的修订。')
                    assert '45 days' in read('/documents/' + document_id)['metadata']['raw_text']
                    turn(f'再次查询知识库 {base} 的 SDAGENT731，确认45天规则。')
                    turn(f'在知识库 {base} 读取文档 {document_id} 的 Bucket 和 Chunk 列表，分别读取第一个对象的实际修订，再编辑 Bucket 正文为 SDAGENT731 bucket verified 45 days，编辑 Chunk 正文为 SDAGENT731 chunk verified 45 days。必须实际调用两个修改工具。')
                    assert 'bucket verified' in read('/documents/' + document_id + '/buckets')[0]['metadata']['content']
                    assert 'chunk verified' in read('/documents/' + document_id + '/chunks')[0]['content']
                    turn(f'为知识库 {base} 文档 {document_id} 新增 Wiki 页 agent/sdagent731，正文 # Agent Wiki\n\nSDAGENT731 45 days. 然后读取页面实际修订，再编辑成 # Agent Wiki\n\nSDAGENT731 verified Wiki 45 days.')
                    assert any(row['concept_id'] == 'agent/sdagent731' and 'verified Wiki' in row['content_md'] for row in read('/knowledge-bases/' + base + '/concepts'))
                    turn(f'读取知识库 {base} 文档 {document_id} 的最新修订，然后删除此临时文档。')
                    assert read('/documents/' + document_id)['status'] == 'archived'
                    query_response = client.post('/query', json={'space_id': space, 'knowledge_base_ids': [base], 'query': 'SDAGENT731', 'use_model': False})
                    query_response.raise_for_status(); query = query_response.json()
                    assert not query['chunks'] and not query.get('selected_concepts')
                    events = host.get('/api/chat/sessions/' + session + '/events', params={'tenant_id': 'tenant_demo'})
                    report['events_response'] = {'status': events.status_code, 'body': events.json()}
                    events.raise_for_status()
                    verify_events(report)
                    report['status'] = 'passed'
        except Exception as error:
            report['error'] = type(error).__name__ + ': ' + str(error)
        finally:
            report['shutdown'] = []
            for child, log in reversed(processes):
                child.terminate()
                try: child.wait(timeout=30)
                except subprocess.TimeoutExpired: child.kill(); child.wait()
                log.close(); report['shutdown'].append(child.returncode)
            (output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({'status': report['status'], 'error': report.get('error')}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
