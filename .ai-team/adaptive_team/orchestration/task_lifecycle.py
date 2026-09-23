"""Immutable Git assignment contracts and a durable operation journal.

Team remains the sole authority for task status, leases, review and billing.
This journal records *external operations*, not a second copy of Team's state.
An incomplete operation is deliberately not retried blindly after a crash.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import PolicyError, canonical, digest, identifier, scope


class RecoveryRequired(RuntimeError):
    """Inspect a durable pending operation before touching its resources again."""


def object_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise PolicyError("A full lowercase Git object ID is required")
    return value


@dataclass(frozen=True)
class TaskContract:
    """Construct only from a live ticket obtained directly from trusted Team.

    JSON is retained as an immutable string; callers receive defensive copies.
    Hash binding detects mutation, but is not network authentication. The backend
    must authenticate workers and must never accept a worker-supplied ticket.
    """
    project: str
    base_commit: str
    ticket_json: str

    def __post_init__(self) -> None:
        identifier(self.project, "project")
        object_id(self.base_commit)
        ticket = self.ticket
        inputs = ticket["inputs"]
        if (type(ticket.get('generation')) is not int or ticket['generation'] < 1
                or ticket['generation'] != inputs.get('role_generation')
                or ticket['generation'] != inputs.get('agent_card',{}).get('generation')):
            raise PolicyError('Assignment requires a bound role generation')
        execution = inputs["execution"]
        if not re.fullmatch(r"[0-9a-f]{32}", ticket["id"]):
            raise PolicyError("Expected a coordinator-generated lease ID")
        identifier(ticket["task_id"], "task")
        identifier(ticket["role"], "role")
        if ticket["status"] != "active" or ticket["phase"] not in ("implement", "review"):
            raise PolicyError("Only a live implementation/review assignment is admissible")
        if ticket["input_digest"] != digest(inputs):
            raise PolicyError("Ticket input digest mismatch")
        spec = inputs["task"]
        expected_role = spec["reviewer_role"] if ticket["phase"] == "review" else spec["role"]
        if (ticket["task_id"] != spec["id"] or ticket["phase"] != inputs["phase"]
                or ticket["role"] != expected_role
                or inputs["prompt_bundle"]["role"]["id"] != expected_role
                or inputs["prompt_digest"] != digest(inputs["prompt_bundle"])):
            raise PolicyError("Ticket phase, task or pinned role mismatch")
        for field in ("actor_id", "workspace_id", "capabilities"):
            if ticket[field] != execution[field]:
                raise PolicyError(f"Ticket {field} mismatch")
        expected_workspace = f"{self.project}/{ticket['task_id']}/{ticket['id']}"
        if ticket["workspace_id"] != expected_workspace:
            raise PolicyError("Ticket belongs to a different project")
        for name in ("started_at", "expires_at"):
            if type(ticket[name]) not in (int, float) or not math.isfinite(ticket[name]):
                raise PolicyError("Invalid lease time")
        if ticket["expires_at"] <= ticket["started_at"]:
            raise PolicyError("Invalid lease interval")
        for path in spec["write_scopes"]:
            scope(path)

    @classmethod
    def from_ticket(cls, ticket: dict, project: str, base_commit: str) -> TaskContract:
        return cls(project, base_commit, canonical(ticket))

    @property
    def ticket(self) -> dict:
        return json.loads(self.ticket_json)

    @property
    def lease_id(self) -> str:
        return self.ticket["id"]

    @property
    def trace_id(self) -> str:
        # The coordinator persists a UUID4 for every execution/review assignment.
        # Reconciliation reuses it; a retry gets a different Jaeger trace.
        from ..observability.run_identity import execution_trace
        return execution_trace(self.ticket,self.project)

    @property
    def binding(self) -> str:
        return digest({"schema": 1, "project": self.project, "base": self.base_commit,
                       "ticket": self.ticket})

    @property
    def write_scopes(self) -> tuple[str, ...]:
        if self.ticket["phase"] == "review":
            return ()
        return tuple(self.ticket["inputs"]["task"]["write_scopes"])


class TaskLifecycle:
    """SQLite write-ahead intent journal, separate from the legacy Team database.

    begin -> checkpoint -> finish is an external-operation lifecycle. SQLite cannot
    atomically commit a Git ref update, so recovery inspects both systems. A pending
    record is a stop signal unless the specific operation has a safe reconciler.
    Calls use independent connections and serialize competing writers.
    """
    def __init__(self, database: str | Path, *, cipher=None, project_id=None):
        from ..security.payload_crypto import PayloadCipher
        self.cipher = cipher if cipher is not None else PayloadCipher()
        self.database = str(Path(database).absolute())
        from .result_outbox import ResultOutbox
        self.outcomes = ResultOutbox(self)
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM sqlite_master WHERE name='operations'").fetchone()
            metadata = db.execute("SELECT 1 FROM sqlite_master WHERE name='journal_metadata'").fetchone()
            if existing and not metadata and db.execute('SELECT 1 FROM operations LIMIT 1').fetchone():
                raise PolicyError('Plaintext journal requires an explicit protected export to a new database')
            db.execute('CREATE TABLE IF NOT EXISTS journal_metadata(id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT, project TEXT, verifier TEXT)')
            meta = db.execute('SELECT * FROM journal_metadata WHERE id=1').fetchone()
            if meta:
                self.identity, self.project_id = meta['identity'], meta['project']
                if project_id is not None and project_id != self.project_id:
                    raise PolicyError('Journal belongs to another project')
                if self._decode(meta['verifier'], '__journal__', 'key_check') != {'format': 1}:
                    raise PolicyError('Journal key verification failed')
            else:
                self.identity, self.project_id = uuid.uuid4().hex, project_id or 'local'
                identifier(self.project_id, 'journal project')
                db.execute('INSERT INTO journal_metadata VALUES(1,?,?,?)',
                    (self.identity, self.project_id, self._encode({'format': 1}, '__journal__', 'key_check')))
            db.execute("CREATE TABLE IF NOT EXISTS operations ("
                       "key TEXT PRIMARY KEY, binding TEXT NOT NULL, request TEXT NOT NULL, "
                       "status TEXT NOT NULL CHECK(status IN ('pending','complete')), "
                       "checkpoint TEXT, result TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS telemetry_outbox ("
                       "id TEXT PRIMARY KEY, operation TEXT NOT NULL, payload TEXT NOT NULL, "
                       "delivered INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0)")
            db.commit()

    def _context(self, key, field):
        return {'journal': self.identity, 'project': self.project_id, 'operation': key, 'field': field}

    def _encode(self, value, key, field):
        return self.cipher.encrypt(value, self._context(key, field))

    def _decode(self, value, key, field):
        return self.cipher.decrypt(value, self._context(key, field))

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def get(self, key: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for field in ("request", "checkpoint", "result"):
            value[field] = self._decode(value[field], key, field) if value[field] is not None else None
        if key.startswith('protocol:'):
            known = self.outcomes.get(key)
            if known is not None:
                if canonical(known['request']) != canonical(value['request']):
                    raise PolicyError('Recovery result differs from reserved request')
                value.update(status='complete',result=known['result'],checkpoint=known['checkpoint'],
                             persistence_pending=value['status']!='complete')
        return value

    def begin(self, key: str, request: dict) -> dict | None:
        """Return a completed identical result, or reserve a new operation.

        An identical *pending* operation still raises: another process may be alive.
        Recovery must hold the operation's external lock and verify its checkpoint.
        """
        if not isinstance(key, str) or not key or len(key) > 300:
            raise PolicyError("Invalid operation key")
        if len(self.outcomes.cache) >= 1024:
            raise RecoveryRequired('Known-result recovery backlog requires reconciliation')
        binding = uuid.uuid4().hex  # Opaque metadata; no low-entropy plaintext fingerprint
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
            if row:
                if canonical(self._decode(row["request"], key, "request")) != canonical(request):
                    raise PolicyError("Operation identity reused with different inputs")
                if row["status"] != "complete":
                    raise RecoveryRequired(key)
                return self._decode(row["result"], key, "result")
            db.execute("INSERT INTO operations(key,binding,request,status) VALUES(?,?,?,'pending')",
                       (key, binding, self._encode(request, key, "request")))
            db.commit()
        return None

    def checkpoint(self, key: str, value: dict) -> None:
        with closing(self._connect()) as db:
            cursor = db.execute("UPDATE operations SET checkpoint=? WHERE key=? AND status='pending'",
                                (self._encode(value, key, "checkpoint"), key))
            if cursor.rowcount != 1:
                raise PolicyError("Only a pending operation can be checkpointed")

    def finish(self, key: str, result: dict, *, telemetry=(), checkpoint=None) -> dict:
        encoded = self._encode(result, key, "result")
        encoded_checkpoint = self._encode(checkpoint,key,'checkpoint') if checkpoint is not None else None
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,result FROM operations WHERE key=?", (key,)).fetchone()
            if not row:
                raise PolicyError("Operation was not reserved")
            if row["status"] == "complete" and canonical(self._decode(row["result"], key, "result")) != canonical(result):
                raise PolicyError("Conflicting duplicate completion")
            db.execute("UPDATE operations SET status='complete',result=? WHERE key=?", (encoded, key))
            if encoded_checkpoint is not None:
                db.execute('UPDATE operations SET checkpoint=? WHERE key=?',(encoded_checkpoint,key))
            self._enqueue_telemetry(db, key, telemetry)
            db.commit()
        return result

    def replay_outcome(self, key, request):
        """Read-only replay before admission; never reserve an operation here."""
        known = self.outcomes.get(key)
        if known is not None:
            if canonical(known['request']) != canonical(request):
                raise PolicyError('Operation identity reused with different inputs')
            return known
        old = self.get(key)
        if old is None:
            return None
        if canonical(old['request']) != canonical(request):
            raise PolicyError('Operation identity reused with different inputs')
        if old['status'] != 'complete':
            raise RecoveryRequired(key)
        return {'request':request,'result':old['result'],'checkpoint':old['checkpoint'],'telemetry':[]}

    def save_outcome(self, key, value):
        """Cache was populated synchronously before the caller could be cancelled.

        The encrypted sidecar permits recovery across restart if SQLite is down.
        Storage failure cannot turn a known RPC success into an unknown outcome.
        Call again with the cached value to reconcile without repeating the RPC.
        """
        try:
            self.outcomes.persist(key,value)
        except OSError:
            self.outcomes.failures += 1
        try:
            self.finish(key,value['result'],telemetry=value['telemetry'],checkpoint=value['checkpoint'])
        except sqlite3.Error:
            self.outcomes.failures += 1
            return False
        try:
            self.outcomes.discard(key)
        except OSError:
            self.outcomes.failures += 1
        return True

    def schedule_telemetry(self, key, exporter):
        from ..observability.background import submit
        if exporter is not None:
            submit(self.deliver_telemetry,key,exporter)

    def _enqueue_telemetry(self, db, key, events):
        for event in events:
            event_id = digest({'operation': key, 'event': event})
            payload = self._encode({**event, 'telemetry_id': event_id}, key, 'telemetry:' + event_id)
            db.execute('INSERT OR IGNORE INTO telemetry_outbox(id,operation,payload) VALUES(?,?,?)',
                       (event_id, key, payload))

    def queue_telemetry(self, key, events):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            self._enqueue_telemetry(db, key, events)
            db.commit()

    def deliver_telemetry(self, key, exporter):
        """Optional exporter failure cannot overwrite a known RPC outcome.

        No lock spans exporter IO. A crash after export may redeliver the event.
        SQL failure leaves the committed outbox available to a later retry.
        """
        if exporter is None:
            return
        try:
            with closing(self._connect()) as db:
                rows = db.execute('SELECT id,payload FROM telemetry_outbox WHERE operation=? AND delivered=0 LIMIT 256', (key,)).fetchall()
            for row in rows:
                try:
                    exporter(self._decode(row['payload'], key, 'telemetry:' + row['id']))
                except Exception:
                    with closing(self._connect()) as db:
                        db.execute('UPDATE telemetry_outbox SET failures=failures+1 WHERE id=?', (row['id'],))
                    continue
                with closing(self._connect()) as db:
                    db.execute('UPDATE telemetry_outbox SET delivered=1 WHERE id=?', (row['id'],))
        except Exception:
            # The original response is already committed. Operational monitoring
            # should inspect undelivered rows; do not rerun the external request.
            return

    def pending(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            keys = [row[0] for row in db.execute("SELECT key FROM operations WHERE status='pending' ORDER BY key")]
        return [value for key in keys if (value := self.get(key))['status']=='pending']

    def reconcile_outcomes(self):
        """Owner recovery pump: SQL repair only; never contacts the remote service."""
        with closing(self._connect()) as db:
            keys = [row[0] for row in db.execute("SELECT key FROM operations WHERE status='pending'")]
        recovered = 0
        for key in keys:
            value = self.outcomes.get(key)
            if value is not None:
                recovered += bool(self.save_outcome(key,value))
        return recovered
