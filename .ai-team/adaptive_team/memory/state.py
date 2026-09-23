"""One learning database, one host-local serialization boundary.

SQLite owns documents, vectors, observations and prompt revisions together.
Short-lived connections prevent stale handles surviving a rollback. The external
control database owns the epoch and rollback intent; it is never rewound.
Controller-only paths and a local filesystem are deployment requirements.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from ..code_integration._git import checked_path
from ..models import PolicyError, canonical, identifier
from ..orchestration.task_lifecycle import RecoveryRequired

_UNSET = object()


class LearningState:
    def __init__(self, directory: str | Path, project: str):
        identifier(project, "memory project")
        self.root, self.project = checked_path(directory), project
        self.root.mkdir(parents=True, exist_ok=True)
        self.learning = checked_path(self.root / "learning")
        self.learning.mkdir(exist_ok=True)
        self.database = checked_path(self.learning / "state.sqlite")
        self.control = checked_path(self.root / "memory-control.sqlite")
        self.mutex = checked_path(self.root / "memory-lock.sqlite")
        with closing(self.connect(self.mutex)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS mutex(id INTEGER PRIMARY KEY)")
        with self.locked(allow_pending=True):
            with closing(self.connect(self.control)) as db:
                db.execute("CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS milestones(id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS reflection_claims(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
                row = db.execute("SELECT value FROM control WHERE id=1").fetchone()
                if row is None:
                    db.execute("INSERT INTO control VALUES(1,?)", (canonical({"project": project,
                        "epoch": uuid.uuid4().hex, "pending": None, "history": []}),))
                elif json.loads(row[0])["project"] != project:
                    raise PolicyError("Memory state belongs to another project")
            # A pending rollback must be reconciled, not initialized away.
            if self._control()["pending"] is not None:
                raise RecoveryRequired("Use SnapshotManager.recover on the existing state")
            with closing(self.connect(self.database)) as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lessons'").fetchone():
                    raise PolicyError('Legacy memory requires a new Stage 2 learning database')
                from ..storage import relational
                for statement in Path(relational.__file__).with_name('memory.sql').read_text().split(';'):
                    if statement.strip(): db.execute(statement)
                ddl = """
                    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS failures(id TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS corrections(id TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
                for statement in ddl.split(';'):
                    if statement.strip(): db.execute(statement)
                existing = db.execute("SELECT value FROM metadata WHERE key='project'").fetchone()
                if existing and existing[0] != project:
                    raise PolicyError("Learning database belongs to another project")
                db.execute("INSERT OR IGNORE INTO metadata VALUES('project',?)", (project,))
                existing_schema=db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
                if existing_schema and existing_schema[0]!='2':raise PolicyError('Unsupported learning schema')
                db.execute("INSERT OR IGNORE INTO metadata VALUES('schema','2')")
                db.commit()

    @staticmethod
    def connect(path):
        checked_path(path)
        db = sqlite3.connect(path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute("PRAGMA synchronous=FULL")
        # DELETE mode keeps the entire capsule in one file at closed-connection
        # boundaries. Do not change to WAL without a new restore design.
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA trusted_schema=OFF")
        return db

    @contextmanager
    def locked(self, *, allow_pending=False):
        with closing(self.connect(self.mutex)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            try:
                if not allow_pending and self._control()["pending"] is not None:
                    raise RecoveryRequired("Pending learning rollback must be reconciled")
                yield
            finally:
                lock.rollback()

    def _control(self):
        with closing(self.connect(self.control)) as db:
            return json.loads(db.execute("SELECT value FROM control WHERE id=1").fetchone()[0])

    def _save_control(self, value):
        with closing(self.connect(self.control)) as db:
            db.execute("UPDATE control SET value=? WHERE id=1", (canonical(value),))

    @property
    def epoch(self):
        with self.locked():
            return self._control()["epoch"]

    @contextmanager
    def transaction(self, *, expected_epoch=_UNSET):
        if expected_epoch is not _UNSET and (not isinstance(expected_epoch, str) or not re.fullmatch(r"[0-9a-f]{32}", expected_epoch)):
            raise PolicyError("An explicitly supplied learning epoch must be a canonical identity")
        with self.locked():
            if expected_epoch is not _UNSET and self._control()["epoch"] != expected_epoch:
                raise PolicyError("Learning operation belongs to a pre-rollback epoch")
            with closing(self.connect(self.database)) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

    @classmethod
    def reopen_for_recovery(cls, directory, project):
        """Open existing pending state without initializing or changing any file."""
        value = object.__new__(cls)
        value.root, value.project = checked_path(directory), identifier(project, "project")
        value.learning = checked_path(value.root / "learning")
        value.database = checked_path(value.learning / "state.sqlite")
        value.control, value.mutex = checked_path(value.root / "memory-control.sqlite"), checked_path(value.root / "memory-lock.sqlite")
        if not all(path.is_file() for path in (value.database, value.control, value.mutex)):
            raise PolicyError("Recovery requires an existing initialized state")
        with value.locked(allow_pending=True):
            if value._control()["project"] != project:
                raise PolicyError("Wrong recovery project")
        return value
