"""Recoverable physical task-file projection of the authoritative Team database.

Outbox events commit in the SAME SQLite transaction as Team state. Files contain
immutable configuration; their directories express lifecycle state. No worker may
write this tree. An OS mutex serializes mirror writers without locking Team SQL.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

from ..models import PolicyError, canonical, identifier
from ..code_integration._git import checked_path
from ..storage import relational as rows
from ..security.file_lock import file_lock


FOLDERS = {"pending": "backlog", "running": "active", "review_ready": "active",
           "reviewing": "active", "accepted": "complete", "blocked": "blocked", "escalated": "failed"}


def install_schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS task_outbox ("
               "seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id), "
               "payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS task_projection ("
               "task_id TEXT PRIMARY KEY REFERENCES tasks(id), seq INTEGER NOT NULL REFERENCES task_outbox(seq), folder TEXT NOT NULL, sha256 TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS task_projection_root (id INTEGER PRIMARY KEY CHECK(id=1), path TEXT NOT NULL)")


def _lease_index(state):
    result = {}
    for lease in state["leases"].values():
        if lease["status"] in ("active", "expired"):
            result.setdefault(lease["task_id"], []).append(lease["id"])
    return {key: sorted(value) for key, value in result.items()}


def _view(state, key, index):
    task = state["tasks"][key]
    leases = index.get(key, [])
    return {"status": task["status"], "attempt": task["attempts"], "leases": leases,
            "spec": task["spec"]}


def enqueue(db, before: dict | None, after: dict) -> None:
    """Call before commit, on Team's own connection; never commit independently."""
    install_schema(db)
    # Seed legacy databases BEFORE their first delta, in the same transaction.
    if before and not db.execute("SELECT 1 FROM task_outbox LIMIT 1").fetchone():
        enqueue(db, None, before)
    old_index = _lease_index(before) if before else {}
    new_index = _lease_index(after)
    for key in after["tasks"]:
        old = _view(before, key, old_index) if before and key in before["tasks"] else None
        new = _view(after, key, new_index)
        if old == new:
            continue
        if old and old["spec"] != new["spec"]:
            raise PolicyError("Task file configuration is immutable; add a new task revision")
        body = (canonical({"schema_version": 1, "project": after["project"], "task": new["spec"]}) + "\n").encode()
        event = {"source": FOLDERS[old["status"]] if old else None,
                 "destination": FOLDERS[new["status"]], "body": body.decode(),
                 "sha256": hashlib.sha256(body).hexdigest(),
                 "status": new["status"], "attempt": new["attempt"], "leases": new["leases"]}
        db.execute("INSERT INTO task_outbox(task_id,payload) VALUES(?,?)", (key, canonical(event)))


def _sync_directory(path: Path) -> None:
    # POSIX supports directory fsync. Python does not expose the equivalent for
    # Windows directories; Windows guarantees here cover process crashes, not a
    # universal power-loss guarantee across storage hardware/filesystems.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class TaskFiles:
    def __init__(self, database: str | Path, root: str | Path, *, fault_hook=None):
        self.database = str(checked_path(database))
        self.root = checked_path(root)
        self.fault_hook = fault_hook or (lambda phase, event: None)
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in {*FOLDERS.values(), ".staging"}:
            path = checked_path(self.root / folder)
            path.mkdir(exist_ok=True)
            if path.stat().st_dev != self.root.stat().st_dev:
                raise PolicyError("Task directories must share one filesystem")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            install_schema(db)
            row = db.execute("SELECT path FROM task_projection_root WHERE id=1").fetchone()
            if row and row[0] != str(self.root):
                raise PolicyError("A Team database already has another task-file root")
            if not row:
                db.execute("INSERT INTO task_projection_root VALUES(1,?)", (str(self.root),))
            # Explicitly bootstrap an older database that predates the outbox.
            if not db.execute("SELECT 1 FROM task_outbox LIMIT 1").fetchone():
                state = rows.load(db)
                enqueue(db, None, state)
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _path(self, folder: str, task_id: str) -> Path:
        identifier(task_id)
        if folder not in set(FOLDERS.values()):
            raise PolicyError("Unknown task directory")
        return checked_path(self.root / folder / (task_id + ".json"))

    @staticmethod
    def _verify(path: Path, expected: str) -> None:
        checked_path(path)
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 2 * 1024 * 1024:
            raise PolicyError("Task projection must be a bounded regular file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise PolicyError("Task configuration was modified outside the journal")

    def _create(self, event: dict, task_id: str, seq: int) -> None:
        destination = self._path(event["destination"], task_id)
        stage = checked_path(self.root / ".staging" / f"{seq}.tmp")
        if destination.exists():
            # Creation recovery: only our reserved event's exact bytes are valid.
            self._verify(destination, event["sha256"])
        else:
            if stage.exists():
                metadata = stage.stat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise PolicyError("Unexpected staging file ownership")
                stage.unlink()  # Only this journal event's incomplete private stage.
            with stage.open("xb") as stream:
                stream.write(event["body"].encode()); stream.flush(); os.fsync(stream.fileno())
            os.link(stage, destination)  # Atomic no-replace publication, including POSIX.
            _sync_directory(destination.parent)
        if stage.exists():
            self._verify(stage, event["sha256"])
            stage.unlink()
            _sync_directory(stage.parent)

    def _move(self, event: dict, task_id: str, seq: int) -> None:
        source = self._path(event["source"], task_id)
        destination = self._path(event["destination"], task_id)
        if source == destination:
            self._verify(source, event["sha256"])
            return
        if source.exists():
            self._verify(source, event["sha256"])
            if destination.exists():
                self._verify(destination, event["sha256"])
                if not os.path.samefile(source, destination):
                    raise PolicyError("Unrelated destination already exists; refusing overwrite")
                # Two names for the SAME inode are our interrupted link/unlink.
            else:
                if source.stat().st_nlink != 1:
                    raise PolicyError("Unexpected hard link outside the task projection")
                os.link(source, destination)
                _sync_directory(destination.parent)
                self.fault_hook("after_link", seq)
            source.unlink()
            _sync_directory(source.parent)
            self.fault_hook("after_move", seq)
        elif destination.exists():
            self._verify(destination, event["sha256"])
        else:
            raise PolicyError("Neither expected task source nor destination exists")

    def drain(self, *, limit: int = 10000) -> int:
        """Read -> filesystem apply -> conditional SQL ack, under an OS mutex.

        The mutex is released by the kernel on process death; there is no timed
        ownership takeover that could race a slow filesystem writer. Other Team
        operations never take this mutex and can commit during fsync.
        """
        with file_lock(self.root / '.projection.lock'):
            return self._drain_locked(limit)

    def _drain_locked(self, limit):
        count = 0
        for _ in range(limit):
            with closing(self._connect()) as db:
                db.execute('BEGIN')
                row = db.execute('SELECT * FROM task_outbox WHERE delivered=0 ORDER BY seq LIMIT 1').fetchone()
                if row is None:
                    return count
                row = dict(row)
                event, task_id, seq = json.loads(row['payload']), row['task_id'], row['seq']
                previous = db.execute('SELECT * FROM task_projection WHERE task_id=?', (task_id,)).fetchone()
                previous = dict(previous) if previous else None
                if event['source'] is None:
                    if previous:
                        raise PolicyError('Duplicate task-file creation')
                elif (not previous or previous['folder'] != event['source']
                      or previous['sha256'] != event['sha256'] or previous['seq'] >= seq):
                    raise PolicyError('Task projection sequence or expected source mismatch')
                db.commit()
            # All path checks, hashes, links, unlinks and fsync are outside SQL.
            allowed = {event['source'], event['destination']}
            if any(self._path(folder, task_id).exists() for folder in set(FOLDERS.values()) - allowed):
                raise PolicyError('Task exists in an unexpected lifecycle directory')
            self.fault_hook('before_move', seq)
            (self._create if event['source'] is None else self._move)(event, task_id, seq)
            self.fault_hook('before_ack', seq)
            with closing(self._connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                head = db.execute('SELECT * FROM task_outbox WHERE delivered=0 ORDER BY seq LIMIT 1').fetchone()
                current = db.execute('SELECT * FROM task_projection WHERE task_id=?', (task_id,)).fetchone()
                if (head is None or dict(head) != row
                        or (dict(current) if current else None) != previous):
                    raise PolicyError('Task projection acknowledgment lost its predecessor')
                db.execute('INSERT INTO task_projection VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET '
                           'seq=excluded.seq,folder=excluded.folder,sha256=excluded.sha256',
                           (task_id, seq, event['destination'], event['sha256']))
                db.execute('UPDATE task_outbox SET delivered=1 WHERE seq=? AND delivered=0', (seq,))
                db.commit()
                count += 1
        raise PolicyError('Task outbox drain limit reached; resume draining before dispatch')

    def _admission_view(self, db, ticket, now):
        # Indexed lease lookup, not a reconstruction of all historical leases.
        lease = rows.lease(db, ticket['id'])
        if (not lease or lease['status'] != 'active' or lease['input_digest'] != ticket['input_digest']
                or lease['expires_at'] <= (time.time() if now is None else now)
                or lease['actor_id'] != ticket['actor_id']):
            raise PolicyError('Task-file admission requires the current live lease')
        rows.fence(db, ticket)
        if rows.held(db, ticket['task_id']):
            raise PolicyError('Task-file admission blocked by human hold')
        latest = db.execute('SELECT * FROM task_outbox WHERE task_id=? ORDER BY seq DESC LIMIT 1',
                            (ticket['task_id'],)).fetchone()
        projected = db.execute('SELECT * FROM task_projection WHERE task_id=?', (ticket['task_id'],)).fetchone()
        if (not latest or not projected or not latest['delivered']
                or projected['seq'] != latest['seq'] or projected['folder'] != 'active'
                or ticket['id'] not in json.loads(latest['payload'])['leases']):
            raise PolicyError("Task file has not reached this lease's active revision")
        return dict(projected)

    def admit(self, ticket: dict, *, now: float | None = None) -> None:
        """Mirror mutex prevents moves; final SQL recheck catches new lifecycle events."""
        with file_lock(self.root / '.projection.lock'):
            self._drain_locked(10000)
            with closing(self._connect()) as db:
                db.execute('BEGIN')
                expected = self._admission_view(db, ticket, now)
                db.commit()
            self._verify(self._path('active', ticket['task_id']), expected['sha256'])
            # No stat/hash/fsync in this final admission transaction.
            with closing(self._connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                if self._admission_view(db, ticket, now) != expected:
                    raise PolicyError('Task projection changed during admission')
                db.commit()
