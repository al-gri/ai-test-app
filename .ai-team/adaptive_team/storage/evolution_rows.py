"""Role-local learning rows. Never a source of operational generation authority."""
import json
from ..models import PolicyError, canonical
from .relational import upsert

DDL='''
CREATE TABLE IF NOT EXISTS evolution_policy(id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_roles(id TEXT PRIMARY KEY, bundle TEXT);
CREATE TABLE IF NOT EXISTS evolution_candidates(id TEXT PRIMARY KEY, role_id TEXT NOT NULL REFERENCES learning_roles(id), status TEXT NOT NULL, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evolution_observations(id TEXT PRIMARY KEY, role_id TEXT NOT NULL REFERENCES learning_roles(id), sequence INTEGER NOT NULL UNIQUE, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evolution_audits(role_id TEXT PRIMARY KEY REFERENCES learning_roles(id), document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evolution_history(sequence INTEGER PRIMARY KEY, document TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS candidate_role ON evolution_candidates(role_id,status);
CREATE INDEX IF NOT EXISTS observation_role ON evolution_observations(role_id,sequence);
'''

def load(db,policy):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evolution'").fetchone():
        raise PolicyError('Legacy evolution state requires a new Stage 2 database')
    for statement in DDL.split(';'):
        if statement.strip():db.execute(statement)
    row=db.execute('SELECT document FROM evolution_policy WHERE id=1').fetchone()
    if row and json.loads(row[0])!=policy:raise PolicyError('Evaluation policy drift requires a separately reviewed migration')
    if not row:db.execute('INSERT INTO evolution_policy VALUES(1,?)',(canonical(policy),))
    return {'schema_version':2,'policy':policy,
        'active':{r['id']:json.loads(r['bundle']) for r in db.execute('SELECT * FROM learning_roles WHERE bundle IS NOT NULL')},
        'candidates':{r['id']:json.loads(r['document']) for r in db.execute('SELECT * FROM evolution_candidates')},
        'observations':{r['id']:json.loads(r['document']) for r in db.execute('SELECT * FROM evolution_observations')},
        'audits':{r['role_id']:json.loads(r['document']) for r in db.execute('SELECT * FROM evolution_audits')},
        'history':[json.loads(r[0]) for r in db.execute('SELECT document FROM evolution_history ORDER BY sequence')]}

def save(db,before,after):
    for key,bundle in after['active'].items():
        if before['active'].get(key)!=bundle:upsert(db,'learning_roles',('id',),{'id':key,'bundle':canonical(bundle)})
    for key,value in after['candidates'].items():
        # New-role proposals need a stable parent even before first activation.
        db.execute('INSERT INTO learning_roles(id,bundle) VALUES(?,NULL) ON CONFLICT(id) DO NOTHING',(value['role_id'],))
        if before['candidates'].get(key)!=value:
            upsert(db,'evolution_candidates',('id',),{'id':key,'role_id':value['role_id'],'status':value['status'],'document':canonical(value)})
    for key,value in after['observations'].items():
        if before['observations'].get(key)!=value:upsert(db,'evolution_observations',('id',),{'id':key,'role_id':value['role_id'],'sequence':value['sequence'],'document':canonical(value)})
    for key,value in after['audits'].items():
        if before['audits'].get(key)!=value:upsert(db,'evolution_audits',('role_id',),{'role_id':key,'document':canonical(value)})
    if after['history'][:len(before['history'])]!=before['history']:raise PolicyError('Evolution history must append')
    for i,value in enumerate(after['history'][len(before['history']):],len(before['history'])):
        db.execute('INSERT INTO evolution_history VALUES(?,?)',(i,canonical(value)))
