"""Independent supervised Docker TTL reaper. Never execute from agent context.

The registry is operational metadata, outside learning snapshots/worker mounts.
Intent precedes Docker create. Enumeration reconciles create->ID-ack crashes.
Reaper leaves stopped containers as evidence; owner cleanup removes them later.
Docker outage is an error, never proof of termination. No provider budget refund.
"""
import argparse
import json
import math
import os
import re
import signal
import time
import uuid
from ._store import SecurityStore
from ..models import PolicyError

LABEL = 'adaptive-team.watchdog'
TOKEN = 'adaptive-team.watchdog-token'


class WatchdogRegistry:
    def __init__(self, database):
        self.store = SecurityStore(database)
        with self.store.edit() as db:
            db.execute('CREATE TABLE IF NOT EXISTS watchdog_meta(id INTEGER PRIMARY KEY CHECK(id=1), namespace TEXT, engine TEXT, heartbeat REAL, last_time REAL)')
            db.execute('INSERT OR IGNORE INTO watchdog_meta VALUES(1,?,NULL,0,0)', (uuid.uuid4().hex,))
            db.execute('CREATE TABLE IF NOT EXISTS watched(name TEXT PRIMARY KEY, token TEXT NOT NULL, deadline REAL NOT NULL, container_id TEXT, status TEXT NOT NULL, proof TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS watchdog_incidents(id TEXT PRIMARY KEY, at REAL, kind TEXT, container_id TEXT)')
            self.namespace = db.execute('SELECT namespace FROM watchdog_meta WHERE id=1').fetchone()[0]

    @classmethod
    def from_environment(cls):
        path = os.environ.get('AI_TEAM_WATCHDOG_DB')
        if not path or not os.path.isabs(path):
            raise PolicyError('An independent watchdog registry is required: AI_TEAM_WATCHDOG_DB')
        return cls(path)

    def require_ready(self, engine=None):
        with self.store.edit() as db:
            row = db.execute('SELECT * FROM watchdog_meta WHERE id=1').fetchone()
            now = time.time()
            if not 0 <= now-row['heartbeat'] <= 15 or not row['engine'] or (engine and row['engine'] != engine):
                raise PolicyError('Independent watchdog is unavailable, stale or on another Docker engine')

    def arm(self, name, ttl):
        if not re.fullmatch('ai-team-[0-9a-f]{32}', name) or type(ttl) not in (int,float) or not math.isfinite(ttl) or not 1 <= ttl <= 3900:
            raise PolicyError('Invalid bounded watchdog lease')
        self.require_ready()
        token = uuid.uuid4().hex
        with self.store.edit() as db:
            db.execute("INSERT INTO watched VALUES(?,?,?,NULL,'armed',NULL)", (name, token, time.time()+ttl))
        return {LABEL: self.namespace, TOKEN: token, 'adaptive-team.run': name}

    def get(self, name):
        with self.store.edit() as db:
            row = db.execute('SELECT * FROM watched WHERE name=?', (name,)).fetchone()
            return dict(row) if row else None

    def bind(self, name, container_id):
        if not re.fullmatch('[0-9a-f]{64}', container_id):
            raise PolicyError('Expected exact Docker container ID')
        with self.store.edit() as db:
            row = db.execute('SELECT * FROM watched WHERE name=?', (name,)).fetchone()
            if not row or row['container_id'] not in (None, container_id):
                raise PolicyError('Watchdog container identity changed')
            db.execute('UPDATE watched SET container_id=? WHERE name=?', (container_id,name))

    def stopped(self, name, container_id, state):
        if state.get('Running') is not False or state.get('Status') not in {'created','exited','dead'}:
            raise PolicyError('Watchdog requires observed termination')
        proof = json.dumps({k: state.get(k) for k in ('Running','Status','ExitCode','OOMKilled')})
        with self.store.edit() as db:
            if db.execute("UPDATE watched SET status='stopped',proof=? WHERE name=? AND container_id=?", (proof,name,container_id)).rowcount != 1:
                raise PolicyError('Termination proof identity mismatch')


class Watchdog:
    def __init__(self, docker, registry):
        self.docker, self.registry = docker, registry
        self.deadlines = {}

    def tick(self):
        engine = self.docker.command(['info','--format','{{.ID}}']).strip()
        if not engine: raise PolicyError('Missing Docker engine identity')
        now, mono = time.time(), time.monotonic()
        with self.registry.store.edit() as db:
            meta = db.execute('SELECT * FROM watchdog_meta WHERE id=1').fetchone()
            if meta['engine'] not in (None,engine): raise PolicyError('Watchdog engine changed')
            backwards = now < meta['last_time']
            rows = {row['name']: dict(row) for row in db.execute('SELECT * FROM watched')}
        ids = self.docker.command(['ps','--all','--no-trunc','--filter','label='+LABEL+'='+self.registry.namespace,'--format','{{.ID}}']).splitlines()
        stopped = []
        failures = []
        for cid in ids:
            try:
                if not re.fullmatch('[0-9a-f]{64}', cid): raise PolicyError('Unexpected Docker ID')
                # A concurrently cleaned-up container is not evidence; retry the next
                # sweep instead of treating inspection failure as stopped.
                value = json.loads(self.docker.command(['inspect', cid]))[0]
                labels = value.get('Config',{}).get('Labels',{})
                name = labels.get('adaptive-team.run')
                row = rows.get(name)
                if not row or labels.get(LABEL) != self.registry.namespace or labels.get(TOKEN) != row['token']:
                    continue
                if value.get('Id') != cid or row['container_id'] not in (None,cid):
                    raise PolicyError('Watchdog refuses replacement container')
                self.registry.bind(name,cid)
                # Docker inspect/bind and earlier victims can take seconds. Read
                # both clocks at this container's decision, never reuse sweep time.
                current, mono = time.time(), time.monotonic()
                backwards = backwards or current < max(now, meta['last_time'])
                now = max(now, current)
                limit = self.deadlines.setdefault(name, mono + max(0, row['deadline']-now))
                expired = backwards or current >= row['deadline'] or mono >= limit
                if not expired: continue
                state = value.get('State',{})
                if state.get('Paused'):
                    self.docker.command(['unpause',cid])
                if state.get('Running') is True:
                    self.docker.command(['kill',cid])
                after = json.loads(self.docker.command(['inspect',cid]))[0]
                if after.get('Id') != cid: raise PolicyError('Docker identity mismatch after kill')
                self.registry.stopped(name,cid,after['State'])
                stopped.append(name)
            except Exception as exc:
                # A broken/replaced container must not starve later TTL victims.
                failures.append((type(exc).__name__, cid if re.fullmatch('[0-9a-f]{64}',cid) else 'invalid'))
        with self.registry.store.edit() as db:
            for kind, cid in failures:
                db.execute('INSERT INTO watchdog_incidents VALUES(?,?,?,?)', (uuid.uuid4().hex,time.time(),kind,cid))
            db.execute('UPDATE watchdog_meta SET engine=?,heartbeat=?,last_time=? WHERE id=1',
                (engine, meta['heartbeat'] if failures else time.time(), max(now,meta['last_time'])))
        if failures:
            raise PolicyError('Watchdog sweep incomplete; per-container incidents recorded')
        return stopped


def cleanup_stopped(docker, registry, name):
    """Owner recovery for an orphan, including crashes before sandbox journaling.

    Exact registry proof + immutable ID + labels are required. Never force-remove
    a running container or an in-use volume; retry after dependent runs reconcile.
    """
    if not re.fullmatch('ai-team-[0-9a-f]{32}',name): raise PolicyError('Invalid recovery identity')
    row=registry.get(name)
    if not row or row['status']!='stopped' or not row['container_id'] or not row['proof']:
        raise PolicyError('Verified watchdog stop proof is required before cleanup')
    cid=row['container_id']
    ids=docker.command(['ps','--all','--no-trunc','--filter','id='+cid,'--format','{{.ID}}']).splitlines()
    if ids:
        if ids!=[cid]: raise PolicyError('Recovery identity mismatch')
        value=json.loads(docker.command(['inspect',cid]))[0]
        labels=value.get('Config',{}).get('Labels',{})
        if (value.get('Id')!=cid or labels.get(LABEL)!=registry.namespace or labels.get(TOKEN)!=row['token']
                or labels.get('adaptive-team.run')!=name or value.get('State',{}).get('Running') is not False):
            raise PolicyError('Refusing cleanup without exact stopped ownership')
        docker.command(['rm',cid])
    volumes=docker.command(['volume','ls','--filter','label=adaptive-team.snapshot='+name,'--format','{{.Name}}']).splitlines()
    expected='ai-team-input-'+name.removeprefix('ai-team-')
    for volume in volumes:
        if volume!=expected: raise PolicyError('Unexpected snapshot resource identity')
        item=json.loads(docker.command(['volume','inspect',volume]))[0]
        if (item.get('Labels',{}).get('adaptive-team.snapshot')!=name or item.get('Driver')!='local'
                or item.get('Options',{}).get('type')!='tmpfs'):
            raise PolicyError('Unexpected snapshot volume ownership/type')
        docker.command(['volume','rm',volume])
    return {'name':name,'stopped':True,'cleanup_complete':True}


def main():
    from .sandbox import DockerCLI
    parser = argparse.ArgumentParser(description='Independent sandbox watchdog; run under a separate supervisor')
    parser.add_argument('--database', required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--docker-config', required=True)
    parser.add_argument('--allow-local-tcp', action='store_true')
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--cleanup-stopped', help='Owner recovery of one exact stopped run; do not start service loop')
    args = parser.parse_args()
    if not .1 <= args.interval <= 5: parser.error('interval must be between .1 and 5 seconds')
    registry = WatchdogRegistry(args.database)
    worker = Watchdog(DockerCLI(endpoint=args.endpoint, config_directory=args.docker_config, allow_local_tcp=args.allow_local_tcp), registry)
    if args.cleanup_stopped:
        print(json.dumps(cleanup_stopped(worker.docker,registry,args.cleanup_stopped)))
        return
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # One supervised owner per registry. Different registries never kill each
    # other's containers. The OS releases this mutex after a process crash.
    guard = SecurityStore(str(registry.store.database)+'.watchdog-owner')
    with guard.edit():
        while running:
            try:
                worker.tick()
            except Exception as exc:
                # No container names, secrets or Docker response text in stderr.
                print('watchdog sweep failed: '+type(exc).__name__, flush=True)
            time.sleep(args.interval)


if __name__ == '__main__':
    main()
