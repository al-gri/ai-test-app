"""Consistent learning snapshots with fenced, crash-recoverable replacement.

Never copy a live SQLite file with shutil.copy. The backup API captures all
learning tables, including vectors and Evolution, in one consistent database.
Operational Team/billing/protocol journals are deliberately outside rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from ..code_integration._git import checked_path
from ..models import PolicyError, canonical, digest, identifier
from .state import LearningState

MAX_DATABASE_BYTES = 128 * 1024 * 1024


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def sync_file(path):
    with open(path, "r+b") as stream:
        os.fsync(stream.fileno())


class SnapshotManager:
    def __init__(self, state: LearningState):
        self.state = state
        self.root = checked_path(state.root / "snapshots")
        self.root.mkdir(exist_ok=True)

    def _path(self, snapshot_id):
        if not isinstance(snapshot_id, str) or not re.fullmatch(r"[0-9a-f]{32}", snapshot_id):
            raise PolicyError("Invalid snapshot ID")
        return checked_path(self.root / snapshot_id)

    def _create(self, label):
        if self.state.database.stat().st_size > MAX_DATABASE_BYTES:
            raise PolicyError("Learning capsule exceeds local snapshot limit")
        snapshot_id = uuid.uuid4().hex
        staging = self.root / (".pending-" + snapshot_id)
        staging.mkdir()
        target = staging / "state.sqlite"
        with closing(self.state.connect(self.state.database)) as source, closing(self.state.connect(target)) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise PolicyError("Snapshot database integrity failure")
        sync_file(target)
        manifest = {"schema": 1, "id": snapshot_id, "project": self.state.project,
            "label": label, "created_at": time.time(), "epoch": self.state._control()["epoch"],
            "bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        with open(staging / "manifest.json", "x", encoding="utf-8") as stream:
            stream.write(canonical(manifest)); stream.flush(); os.fsync(stream.fileno())
        sync_directory(staging)
        # Unique directory publication: a crash before rename leaves an ignored
        # staging directory, after rename leaves a discoverable valid snapshot.
        os.rename(staging, self._path(snapshot_id))
        sync_directory(self.root)
        return manifest

    def create(self, label: str) -> dict:
        identifier(label, "snapshot label")
        with self.state.locked():
            return self._create(label)

    def before_milestone(self, milestone_id: str) -> dict:
        identifier(milestone_id, "milestone")
        with self.state.locked():
            # Milestone checkpoints belong to an epoch: rollback followed by a
            # new attempt can create a new checkpoint with the same logical name.
            key = self.state._control()["epoch"] + ":" + milestone_id
            with closing(self.state.connect(self.state.control)) as db:
                row = db.execute("SELECT snapshot FROM milestones WHERE id=?", (key,)).fetchone()
                if row:
                    return self._validate(row[0])[0]
                result = self._create(milestone_id)
                db.execute("INSERT INTO milestones VALUES(?,?)", (key, result["id"]))
                return result

    def _validate(self, snapshot_id):
        root = self._path(snapshot_id)
        manifest_file, database = checked_path(root / "manifest.json"), checked_path(root / "state.sqlite")
        if not manifest_file.is_file() or manifest_file.stat().st_size > 16384:
            raise PolicyError("Snapshot manifest missing or oversized")
        from ..protocols.jsonrpc import decode
        manifest = decode(manifest_file.read_bytes(), limit=16384)
        if manifest.get("schema") != 1 or manifest.get("id") != snapshot_id or manifest.get("project") != self.state.project:
            raise PolicyError("Snapshot identity/schema mismatch")
        if not database.is_file() or not 0 < database.stat().st_size <= MAX_DATABASE_BYTES:
            raise PolicyError("Snapshot database missing or oversized")
        if database.stat().st_size != manifest.get("bytes") or hashlib.sha256(database.read_bytes()).hexdigest() != manifest.get("sha256"):
            raise PolicyError("Snapshot hash mismatch")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise PolicyError("Snapshot integrity failure")
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise PolicyError('Snapshot foreign key integrity failure')
            if dict(db.execute("SELECT key,value FROM metadata")) != {"project": self.state.project, "schema": "2"}:
                raise PolicyError("Snapshot learning schema/project mismatch")
        return manifest, database

    def list(self) -> list[dict]:
        with self.state.locked(allow_pending=True):
            return [self._validate(path.name)[0] for path in sorted(self.root.iterdir()) if re.fullmatch(r"[0-9a-f]{32}", path.name)]

    def rollback(self, snapshot_id: str, *, expected_epoch: str, reason: str) -> dict:
        identifier(reason, "rollback reason code")
        with self.state.locked():
            self._validate(snapshot_id)
            control = self.state._control()
            if control["epoch"] != expected_epoch:
                raise PolicyError("Stale rollback request")
            safety = self._create("before-rollback")
            operation = {"id": uuid.uuid4().hex, "snapshot": snapshot_id, "safety_snapshot": safety["id"],
                "reason": reason, "previous_epoch": expected_epoch, "new_epoch": uuid.uuid4().hex}
            # Commit a fence BEFORE file replacement. On interruption every
            # regular reader/writer stops until recover() completes this intent.
            control.update(epoch=operation["new_epoch"], pending=operation)
            self.state._save_control(control)
            return self._recover()

    def _recover(self):
        control = self.state._control()
        operation = control["pending"]
        if operation is None:
            return {"recovered": False, "epoch": control["epoch"]}
        _, source = self._validate(operation["snapshot"])
        temporary = self.state.learning / (".restore-" + uuid.uuid4().hex + ".sqlite")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, closing(self.state.connect(temporary)) as dst:
            src.backup(dst)
        sync_file(temporary)
        checked_path(self.state.database)
        os.replace(temporary, self.state.database)
        sync_directory(self.state.learning)
        control["history"].append(operation)
        control["pending"] = None
        self.state._save_control(control)
        return {"recovered": True, **operation}

    def recover(self):
        with self.state.locked(allow_pending=True):
            return self._recover()
