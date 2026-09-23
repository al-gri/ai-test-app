"""Durable remote Card registrations; share the operational admission transaction."""
import json
from contextlib import closing
from ..models import PolicyError, canonical
from .relational import connect, require

DDL='''CREATE TABLE IF NOT EXISTS remote_agents(
 id TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>=1),
 revoked INTEGER NOT NULL CHECK(revoked IN (0,1)), document TEXT NOT NULL)'''

def encode_entry(entry):
    card,interface,roles,expiry,priority,grants=entry
    return canonical({'card':card.data,'interface':interface,'roles':sorted(roles),'expiry':expiry,
        'priority':priority,'grants':{k:sorted(v) for k,v in grants.items()}})

def decode_entry(text):
    from ..protocols.a2a.cards import AgentCard
    v=json.loads(text)
    return (AgentCard.parse(v['card'],allow_loopback_http=True),v['interface'],frozenset(v['roles']),
        v['expiry'],v['priority'],{k:frozenset(s) for k,s in v['grants'].items()})

def install(path):
    with closing(connect(path)) as db:
        db.execute('BEGIN IMMEDIATE');require(db);db.execute(DDL);db.commit()

def register(path,key,entry):
    with closing(connect(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT * FROM remote_agents WHERE id=?',(key,)).fetchone()
        if old and not old['revoked']:raise PolicyError('Agent already registered; explicitly revoke before replacing its card')
        generation=old['generation']+1 if old else 1
        db.execute('INSERT INTO remote_agents VALUES(?,?,0,?) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,revoked=0,document=excluded.document',
            (key,generation,encode_entry(entry)));db.commit()

def revoke(path,key):
    with closing(connect(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE remote_agents SET revoked=1,generation=generation+1 WHERE id=?',(key,));db.commit()

def bootstrap(path,key,entry):
    """Atomic first-install only: a cached entry can NEVER replace a tombstone."""
    with closing(connect(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO remote_agents VALUES(?,1,0,?) ON CONFLICT(id) DO NOTHING',
            (key,encode_entry(entry)));db.commit()

def load(path,db=None):
    if db is None:
        with closing(connect(path)) as own:return load(path,own)
    rows=db.execute('SELECT * FROM remote_agents WHERE revoked=0').fetchall()
    return {r['id']:decode_entry(r['document']) for r in rows},{r['id']:r['generation'] for r in rows}
