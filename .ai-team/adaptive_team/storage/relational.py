"""Normalized persistence with delta writes and compatible public snapshots.

Domain transitions still use a dictionary unit of work. That dictionary is an
ephemeral projection of rows, not an independently writable JSON database. Cold
documents are updated only when changed; heartbeats never rewrite task specs or
historical lease inputs. Admission has a separate indexed SQL fast path.
"""
import json
import sqlite3
from pathlib import Path
from ..models import PolicyError, canonical

VERSION=3
# Read packaged DDL once, before any caller starts an operational transaction.
SCHEMA_STATEMENTS=Path(__file__).with_name('schema.sql').read_text(encoding='utf-8').split(';')
HOT=('schema_version','created_at','last_time','paused','pause_reason','spent_cents',
     'budget_exceeded','plan_digest','policy_digest','catalog_digest','action_generation')
TASK_HOT=('status','attempts','created_at','ready_after','reason','blocker')
LEASE_HOT=('task_id','role','generation','actor_id','phase','status','expires_at','reserved_cents','receipt_digest','result_status')
COLLECTIONS={'tasks','leases','historical_catalog','role_registry','human_requests','human_holds','summary'}

def connect(path):
    db=sqlite3.connect(path,timeout=30,isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('PRAGMA synchronous=FULL')
    return db

def install(db):
    # No executescript: sqlite3.executescript would implicitly commit the caller's
    # transaction and expose a half-created schema after a process crash.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state'").fetchone():
        raise PolicyError('Legacy JSON state requires a new Stage 2 database')
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone():
        row=db.execute("SELECT version FROM schema_version WHERE component='team'").fetchone()
        if row and row[0]!=VERSION:raise PolicyError('Unsupported operational schema')
    for statement in SCHEMA_STATEMENTS:
        if statement.strip():db.execute(statement)
    db.execute("INSERT INTO schema_version VALUES('team',?) ON CONFLICT(component) DO NOTHING",(VERSION,))

def require(db):
    try:row=db.execute("SELECT version FROM schema_version WHERE component='team'").fetchone()
    except sqlite3.OperationalError:raise PolicyError('Initialize a new Stage 2 project first') from None
    if not row or row[0]!=VERSION:raise PolicyError('Unsupported operational schema')

def upsert(db,table,keys,values):
    # Identifiers originate exclusively in this module, not user input.
    cols=tuple(values); nonkeys=[k for k in cols if k not in keys]
    action='DO UPDATE SET '+','.join(k+'=excluded.'+k for k in nonkeys) if nonkeys else 'DO NOTHING'
    db.execute('INSERT INTO '+table+'('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+') ON CONFLICT('+','.join(keys)+') '+action,tuple(values.values()))

def seed_roles(db,roles,bundles):
    from ..models import digest
    for key,card in roles.items():
        db.execute('INSERT INTO roles VALUES(?,?,?,?,0)',(key,1,card['version'],card['status']))
        db.execute('INSERT INTO role_versions VALUES(?,?,?,?,?,?,?)',
            (key,1,card['version'],digest(bundles[key]),canonical(card),canonical(bundles[key]),'initial owner seed'))

def role_registry(db):
    return {r['id']:{'generation':r['generation'],'version':r['version'],'status':r['status'],
        'revoked':bool(r['revoked']),'prompt_digest':r['prompt_digest'],
        'card':json.loads(r['card']),'bundle':json.loads(r['bundle'])}
        for r in db.execute('SELECT r.*,v.prompt_digest,v.card,v.bundle FROM roles r JOIN role_versions v ON v.role_id=r.id AND v.generation=r.generation')}

def lease(db,key):
    row=db.execute('SELECT l.*,i.document FROM leases l JOIN lease_inputs i ON i.lease_id=l.id WHERE l.id=?',(key,)).fetchone()
    if not row:return None
    return _lease_value(row)


def _lease_value(row):
    """One decoder for indexed admission and bulk historical reconstruction."""
    value=json.loads(row['document'])
    for k in LEASE_HOT:
        if k!='result_status' or row[k] is not None:value[k]=row[k]
    value['id']=row['id']
    return value

def load(db):
    require(db)
    row=db.execute('SELECT * FROM projects WHERE id=1').fetchone()
    if not row:raise PolicyError('Initialize the project first')
    state={k:row[k] for k in ('project',*HOT)}
    state['paused']=bool(state['paused']);state['budget_exceeded']=bool(state['budget_exceeded'])
    state.update({r['name']:json.loads(r['document']) for r in db.execute('SELECT * FROM project_documents')})
    state['tasks']={}
    for r in db.execute('SELECT t.*,s.document FROM tasks t JOIN task_specs s ON s.task_id=t.id'):
        spec=json.loads(r['document']);spec.update(id=r['id'],role=r['role'],reviewer_role=r['reviewer_role'],dependencies=[])
        state['tasks'][r['id']]={**{k:r[k] for k in TASK_HOT},'spec':spec,'result':None,'history':[],'unresolved_findings':[]}
    for r in db.execute('SELECT * FROM task_dependencies ORDER BY task_id,ordinal'):state['tasks'][r['task_id']]['spec']['dependencies'].append(r['parent_id'])
    for r in db.execute('SELECT * FROM task_results'):state['tasks'][r['task_id']]['result']=json.loads(r['document'])
    for table,field in [('task_history','history'),('task_findings','unresolved_findings')]:
        for r in db.execute('SELECT * FROM '+table+' ORDER BY task_id,ordinal'):state['tasks'][r['task_id']][field].append(json.loads(r['document']))
    state['leases']={r['id']:_lease_value(r) for r in db.execute(
        'SELECT l.*,i.document FROM leases l JOIN lease_inputs i ON i.lease_id=l.id')}
    state['role_registry']=role_registry(db)
    state['historical_catalog']={r['role_id']:json.loads(r['card']) for r in db.execute('SELECT * FROM role_versions WHERE generation=1')}
    state['human_requests']={r['id']:json.loads(r['document']) for r in db.execute('SELECT * FROM human_requests')}
    state['human_holds']={r['task_id']:r['request_id'] for r in db.execute('SELECT * FROM human_holds')}
    return state

def save(db,before,after):
    before=before or {}
    for key in before.keys()-after.keys()-COLLECTIONS-set(HOT)-{'project'}:
        db.execute('DELETE FROM project_documents WHERE project=? AND name=?',(before['project'],key))
    hot={'id':1,'project':after['project'],**{k:after.get(k,0) for k in HOT}}
    if any(before.get(k)!=after.get(k) for k in ('project',*HOT)):
        upsert(db,'projects',('id',),hot)
    for key,value in after.items():
        if key in COLLECTIONS or key in HOT or key=='project':continue
        if key not in before or before[key]!=value:
            upsert(db,'project_documents',('project','name'),{'project':after['project'],'name':key,'document':canonical(value)})
    # Insert all task parents before edge rows, including forward references.
    for key,task in after['tasks'].items():
        old=before.get('tasks',{}).get(key)
        values={'id':key,'project':after['project'],'role':task['spec']['role'],'reviewer_role':task['spec']['reviewer_role'],**{k:task[k] for k in TASK_HOT}}
        if old is None or any(old[k]!=task[k] for k in TASK_HOT):upsert(db,'tasks',('id',),values)
        if old is None:
            cold={k:v for k,v in task['spec'].items() if k not in {'id','role','reviewer_role','dependencies'}}
            db.execute('INSERT INTO task_specs VALUES(?,?)',(key,canonical(cold)))
        elif old['spec']!=task['spec']:raise PolicyError('Task specification is immutable')
        if old is None or old['result']!=task['result']:
            if task['result'] is None:db.execute('DELETE FROM task_results WHERE task_id=?',(key,))
            else:upsert(db,'task_results',('task_id',),{'task_id':key,'document':canonical(task['result'])})
        for table,field in [('task_history','history'),('task_findings','unresolved_findings')]:
            previous=old[field] if old else []
            if previous==task[field]:continue
            if field=='history' and task[field][:len(previous)]!=previous:raise PolicyError('History must append')
            if field=='unresolved_findings':db.execute('DELETE FROM '+table+' WHERE task_id=?',(key,));previous=[]
            for i,value in enumerate(task[field][len(previous):],len(previous)):
                db.execute('INSERT INTO '+table+' VALUES(?,?,?)',(key,i,canonical(value)))
    for key,task in after['tasks'].items():
        if key not in before.get('tasks',{}):
            for i,parent in enumerate(task['spec']['dependencies']):db.execute('INSERT INTO task_dependencies VALUES(?,?,?)',(key,parent,i))
    for key,value in after['leases'].items():
        old=before.get('leases',{}).get(key)
        if old==value:continue
        upsert(db,'leases',('id',),{'id':key,**{k:value.get(k) for k in LEASE_HOT}})
        cold={k:v for k,v in value.items() if k not in LEASE_HOT and k!='id'}
        if old is None:db.execute('INSERT INTO lease_inputs VALUES(?,?)',(key,canonical(cold)))
        elif cold!={k:v for k,v in old.items() if k not in LEASE_HOT and k!='id'}:raise PolicyError('Lease inputs are immutable')
    for key,value in after.get('human_requests',{}).items():
        if before.get('human_requests',{}).get(key)!=value:
            upsert(db,'human_requests',('id',),{'id':key,'task_id':value['task_id'],'status':value['status'],'document':canonical(value)})
    oldholds=before.get('human_holds',{});newholds=after.get('human_holds',{})
    for key in oldholds.keys()-newholds.keys():db.execute('DELETE FROM human_holds WHERE task_id=?',(key,))
    for key,value in newholds.items():
        if oldholds.get(key)!=value:upsert(db,'human_holds',('task_id',),{'task_id':key,'request_id':value})

def held(db,task_id):
    return bool(db.execute('''WITH RECURSIVE ancestors(id) AS (
        SELECT ? UNION SELECT d.parent_id FROM task_dependencies d JOIN ancestors a ON d.task_id=a.id)
        SELECT 1 FROM human_holds h JOIN ancestors a ON h.task_id=a.id LIMIT 1''',(task_id,)).fetchone())

def fence(db,ticket):
    row=db.execute('SELECT r.generation,r.revoked,r.status,v.prompt_digest FROM roles r JOIN role_versions v ON v.role_id=r.id AND v.generation=r.generation WHERE r.id=?',(ticket['role'],)).fetchone()
    if (not row or row['revoked'] or row['status'] not in ('active','trial')
            or type(ticket.get('generation')) is not int or row['generation']!=ticket['generation']
            or ticket['inputs']['prompt_digest']!=row['prompt_digest']):
        raise PolicyError('Stale or revoked role generation')
