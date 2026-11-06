"""Generate data with an old checkout, then verify a non-destructive OSS upgrade."""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

LEGACY = r'''
import json,os,sqlite3
from pathlib import Path
from sqlmodel import Session, select
from app.db import init_db,engine,models as M
from app.db.seed import seed_demo_data
from app.security.auth import hash_password
from app.security.encryption import encrypt_secret
p=Path(os.environ['UPGRADE_FIXTURE']);init_db()
with Session(engine) as db:
 seed_demo_data(db)
 user=db.exec(select(M.User).where(M.User.username=='admin')).first()
 user.password_hash=hash_password('old-account-password');db.add(user)
 db.add(M.AgentProfile(id='agent_upgrade',tenant_id='tenant_demo',name='升级保留员工',metadata_json={'owner_user_id':user.id,'custom_value':'unchanged'}))
 db.add(M.ChatSession(id='session_upgrade',tenant_id='tenant_demo',agent_id='agent_upgrade',user_id=user.id,title='旧会话',slots_json={'amount':80},awaiting_input_json={'slot':'amount'}))
 db.add(M.Message(id='msg_upgrade',tenant_id='tenant_demo',session_id='session_upgrade',role='user',content='旧消息及附件',metadata_json={'file':str(p/'attachment.txt')}))
 db.add(M.ModelConfig(id='model_upgrade',tenant_id='tenant_demo',name='旧模型',api_key_encrypted=encrypt_secret('synthetic-upgrade-key'),model='fixture',base_url='https://example.invalid/v1'))
 db.add(M.ScheduledTask(id='sched_upgrade',tenant_id='tenant_demo',agent_id='agent_upgrade',created_by_user_id=user.id,title='旧任务',prompt='保留配置',status='paused',schedule_json={'hour':9,'minute':30}))
 db.commit()
init_db();init_db()
(p/'attachment.txt').write_text('Synthetic old attachment')
c=sqlite3.connect(p/'old.db');c.row_factory=sqlite3.Row
names=['users','agent_profiles','agent_resource_bindings','skills','skill_versions','agent_skill_branches','knowledge_bases','knowledge_documents','knowledge_chunks','tools','general_skills','model_configs','sessions','messages','scheduled_tasks']
rows={n:[dict(r) for r in c.execute('SELECT * FROM '+n)] for n in names}
(p/'before.json').write_text(json.dumps(rows))
with sqlite3.connect(p/'new.db') as dst:c.backup(dst)
'''
CANDIDATE = r'''
import json,os,sqlite3
from pathlib import Path
from sqlmodel import Session, select
from app.db import init_db,engine,models as M
from app.security.auth import verify_password
from app.security.encryption import decrypt_secret
from staffdeck_harness.distribution.bootstrap import initialize_defaults
p=Path(os.environ['UPGRADE_FIXTURE']);init_db();init_db()
c=sqlite3.connect(p/'new.db');c.row_factory=sqlite3.Row
old=json.loads((p/'before.json').read_text());changes=[]
for table,rows in old.items():
 current={r['id']:dict(r) for r in c.execute('SELECT * FROM '+table)}
 for row in rows:
  assert row['id'] in current, (table,'missing row')
  for key,value in row.items():
   new=current[row['id']][key]
   if isinstance(value,str) and isinstance(new,str):
    try:value,new=json.loads(value),json.loads(new)
    except ValueError:pass
   if value!=new:changes.append((table,key))
assert not changes, changes
with Session(engine) as db:
 initialize_defaults(db);initialize_defaults(db)
 user=db.exec(select(M.User).where(M.User.username=='admin')).first()
 assert verify_password('old-account-password',user.password_hash)
 assert decrypt_secret(db.get(M.ModelConfig,'model_upgrade').api_key_encrypted)=='synthetic-upgrade-key'
 assert db.get(M.DistributionInstallRecord,'tenant_demo').status=='preserved_existing'
 assert len(db.exec(select(M.AgentProfile)).all())==len(old['agent_profiles'])
assert (p/'attachment.txt').read_text()=='Synthetic old attachment'
print(json.dumps({'business_tables':len(old),'preserved_rows':sum(len(x) for x in old.values()),'changed_business_fields':0,'old_password':'verified','old_model_key':'decrypts','attachment':'preserved','defaults':'not overwritten'}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-root', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='staffdeck-upgrade-') as temporary:
        p = Path(temporary)
        env = dict(os.environ, APP_SECRET='synthetic-upgrade-secret', ULTRARAG_DOTENV=os.devnull,
                   UPGRADE_FIXTURE=temporary, STARTUP_ORPHAN_CLEANUP_ENABLED='false')
        for checkout, name, code in ((args.legacy_root.resolve(), 'old', LEGACY),
                                     (root, 'new', CANDIDATE)):
            env.update(PYTHONPATH=os.pathsep.join([str(checkout/'backend'), str(checkout/'backend/src')]),
                       DATABASE_URL='sqlite:///' + str(p / f'{name}.db'),
                       ULTRARAG_DATA_DIR=str(p / f'{name}-data'))
            subprocess.run([sys.executable, '-c', code], cwd=checkout, env=env, check=True)


if __name__ == '__main__':
    main()
