"""Consistent, bounded SQL read models; never return raw prompts or lease inputs."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from ..storage.relational import require
from ..models import PolicyError


@contextmanager
def read(database):
    # mode=ro avoids accidentally creating a second empty project at a typo path.
    db = sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro', uri=True,
                         isolation_level=None, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        require(db)
        yield db
    finally:
        db.close()


def dashboard(database):
    with read(database) as db:
        project = dict(db.execute('SELECT project,paused,spent_cents,budget_exceeded FROM projects').fetchone())
        count = db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        if count > 2000:
            raise PolicyError('Dashboard graph exceeds 2000 tasks; use a scoped operational view')
        tasks = [dict(row) for row in db.execute('SELECT id,role,status,attempts,reason FROM tasks ORDER BY id')]
        edges = [dict(row) for row in db.execute('SELECT parent_id,task_id FROM task_dependencies ORDER BY task_id,ordinal')]
        holds = {r[0] for r in db.execute('WITH RECURSIVE held(id) AS (SELECT task_id FROM human_holds UNION SELECT d.task_id FROM task_dependencies d JOIN held h ON d.parent_id=h.id) SELECT id FROM held')}
        has_projection=bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='task_integrations'").fetchone())
        integrated={r[0]:r[1] for r in db.execute('SELECT task_id,commit_oid FROM task_integrations')} if has_projection else {}
        for task in tasks:
            task['held'] = task['id'] in holds
            task['stage'] = ('human_hold' if task['held'] else
                {'pending':'backlog','running':'active','reviewing':'active',
                 'review_ready':'review','accepted':'accepted_integration_unknown'}.get(task['status'],task['status']))
            if task['status']=='accepted' and not task['held']:
                task['stage']='integration_recorded' if task['id'] in integrated else ('awaiting_merge' if has_projection else 'accepted_integration_unknown')
                task['integrated_commit']=integrated.get(task['id'])
        return {'project':project,'tasks':tasks,'edges':edges}


def inbox(database):
    with read(database) as db:
        return [json.loads(r[0]) for r in db.execute(
            "SELECT document FROM human_requests WHERE status IN ('pending','reject','return') ORDER BY id LIMIT 1000")]


def ledger(database, day=None):
    day = day or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    try:valid=len(day)==10 and datetime.strptime(day,'%Y-%m-%d').strftime('%Y-%m-%d')==day
    except ValueError:valid=False
    if not valid:
        raise PolicyError('Expected UTC day YYYY-MM-DD')
    with read(database) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='token_limits'").fetchone():
            return {'day':day,'configured':False,'currency':'USD','units_per_usd':1000000,'roles':[]}
        # Reservations from previous days still represent unresolved obligations.
        rows = db.execute('''SELECT r.id AS role, l.amount AS daily_limit,
            COALESCE(c.spent,0) AS spent, COALESCE(c.reserved,0) AS reserved,
            COALESCE(c.input_count,0) AS input_count, COALESCE(c.output_count,0) AS output_count
            FROM roles r LEFT JOIN token_limits l ON l.role=r.id LEFT JOIN (
              SELECT c.role,
                SUM(CASE WHEN c.day=? AND c.status='settled' THEN c.actual ELSE 0 END) AS spent,
                SUM(CASE WHEN c.status='reserved' THEN c.reserved ELSE 0 END) AS reserved,
                SUM(CASE WHEN c.day=? THEN COALESCE(u.input_tokens,0) ELSE 0 END) AS input_count,
                SUM(CASE WHEN c.day=? THEN COALESCE(u.output_tokens,0) ELSE 0 END) AS output_count
              FROM token_calls c LEFT JOIN token_usage u ON u.call_id=c.id GROUP BY c.role
            ) c ON c.role=r.id ORDER BY r.id''',(day,day,day))
        return {'day':day,'configured':True,'currency':'USD','units_per_usd':1000000,
                'roles':[dict(r) for r in rows]}
