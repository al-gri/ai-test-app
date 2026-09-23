"""Durable offline Batch outbox. Preparing JSONL never submits a paid job.

SQLite reserves membership before exporting files. An interrupted export is
replayed from immutable database bytes under the same batch ID. There is no
automatic network retry, and imported provider results are not Team receipts.
Keep this database and exports outside agent-writable worktrees.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

from ..models import PolicyError, identifier
from ..protocols.jsonrpc import decode, encode
from .router import ModelRouter, Route, RoutingMetadata


class BatchManager:
    def __init__(self, database: str | Path, router: ModelRouter, *, max_requests=1000, max_bytes=20_000_000):
        if type(max_requests) is not int or not 1 <= max_requests <= 50000:
            raise PolicyError("Batch request limit must be 1..50000")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 200_000_000:
            raise PolicyError("Invalid Batch byte limit")
        self.database = str(Path(database).absolute())
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self.router, self.max_requests, self.max_bytes = router, max_requests, max_bytes
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                    endpoint TEXT NOT NULL, policy TEXT NOT NULL, payload BLOB NOT NULL,
                    sha256 TEXT NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, custom_id TEXT UNIQUE NOT NULL,
                    binding BLOB NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    endpoint TEXT NOT NULL, policy TEXT NOT NULL, line BLOB NOT NULL,
                    batch_id TEXT REFERENCES batches(id), result BLOB);
            """)

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def enqueue(self, custom_id: str, *, project: str, role: str, metadata: RoutingMetadata,
                route: Route, body: dict, active_lease_id: str | None = None) -> str:
        """Trusted owner/backend call, never an agent-exposed queue operation.

        An active coding/review lease cannot be turned into a 24-hour Batch job.
        Project/role are attribution only; reservations and paid submission remain
        the responsibility of the trusted provider gateway introduced later.
        """
        if not isinstance(custom_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", custom_id):
            raise PolicyError("Batch custom_id must be a bounded stable identifier")
        identifier(project, "project"); identifier(role, "role")
        self.router.validate(route, metadata)
        if route.mode != "batch" or active_lease_id is not None:
            raise PolicyError("Only detached noncritical background jobs may enter Batch")
        if route.profile.provider != "openai":
            raise PolicyError("This JSONL adapter implements the OpenAI Batch format only")
        if not isinstance(body, dict) or body.get("model") != route.profile.model or body.get("stream", False) is not False:
            raise PolicyError("Batch body must match the route and disable streaming")
        required = "input" if route.profile.endpoint == "/v1/responses" else "messages"
        if required not in body:
            raise PolicyError("Batch request has no model input")
        limit = "max_output_tokens" if required == "input" else "max_completion_tokens"
        if type(body.get(limit)) is not int or not 1 <= body[limit] <= 1_000_000:
            raise PolicyError("An explicit positive output-token cap is required")
        line = encode({"custom_id": custom_id, "method": "POST", "url": route.profile.endpoint, "body": body}) + b"\n"
        if len(line) > min(self.max_bytes, 2 * 1024 * 1024):
            raise PolicyError("Single Batch request exceeds the configured bound")
        from dataclasses import asdict
        binding = encode({"project": project, "role": role, "metadata": asdict(metadata),
                          "route": asdict(route), "line_sha256": hashlib.sha256(line).hexdigest()})
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT binding FROM jobs WHERE custom_id=?", (custom_id,)).fetchone()
            if existing:
                if existing[0] != binding:
                    raise PolicyError("Batch identity reused with changed inputs")
            else:
                db.execute("INSERT INTO jobs(custom_id,binding,provider,model,endpoint,policy,line) VALUES(?,?,?,?,?,?,?)",
                    (custom_id, binding, route.profile.provider, route.profile.model, route.profile.endpoint, route.policy_digest, line))
            db.commit()
        return custom_id

    def prepare(self) -> dict | None:
        """Reserve one homogeneous group. Competing processes cannot double-claim."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            first = db.execute("SELECT * FROM jobs WHERE batch_id IS NULL ORDER BY seq LIMIT 1").fetchone()
            if not first:
                return None
            group = tuple(first[k] for k in ("provider", "model", "endpoint", "policy"))
            rows = db.execute("SELECT custom_id,line FROM jobs WHERE batch_id IS NULL AND provider=? "
                "AND model=? AND endpoint=? AND policy=? ORDER BY seq LIMIT ?", (*group, self.max_requests))
            parts, ids, size = [], [], 0
            for row in rows:
                if size + len(row["line"]) > self.max_bytes:
                    break
                parts.append(row["line"]); ids.append(row["custom_id"]); size += len(row["line"])
            if not ids:
                raise PolicyError("Queued job exceeds this manager's file limit")
            payload, batch_id = b"".join(parts), uuid.uuid4().hex
            db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?,?,?)", (batch_id, *group, payload,
                       hashlib.sha256(payload).hexdigest(), len(ids)))
            db.executemany("UPDATE jobs SET batch_id=? WHERE custom_id=? AND batch_id IS NULL", ((batch_id, key) for key in ids))
            db.commit()
        return self.manifest(batch_id)

    def _batch(self, batch_id):
        if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", batch_id):
            raise PolicyError("Invalid batch ID")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise PolicyError("Unknown batch")
        return row

    def manifest(self, batch_id):
        row = self._batch(batch_id)
        return {**{k: row[k] for k in ("id", "provider", "model", "endpoint", "policy", "sha256", "count")},
                "bytes": len(row["payload"]), "completion_window": "24h", "submitted": False}

    def prepared(self) -> list[dict]:
        """Recover IDs even if prepare committed before its caller received them."""
        with closing(self._connect()) as db:
            ids = [row[0] for row in db.execute("SELECT id FROM batches ORDER BY rowid")]
        return [self.manifest(batch_id) for batch_id in ids]

    def export(self, batch_id: str, directory: str | Path) -> Path:
        row = self._batch(batch_id)
        root = Path(directory).absolute()
        root.mkdir(parents=True, exist_ok=True)
        path = root / (batch_id + ".jsonl")
        # No overwrite. A crash after the link but before return is replayable.
        # Both names live on the same filesystem; fsync data before publication.
        fd, temporary = tempfile.mkstemp(prefix=".batch-", suffix=".tmp", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(row["payload"]); stream.flush(); os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.stat().st_size != len(row["payload"]) or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                    raise PolicyError("Existing Batch export differs from its immutable record")
            if os.name != "nt":
                handle = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(handle)
                finally:
                    os.close(handle)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return path

    def submission_request(self, batch_id: str, input_file_id: str) -> dict:
        """Return request data only; the caller must authorize upload/spend."""
        if not isinstance(input_file_id, str) or not re.fullmatch(r"file-[A-Za-z0-9_-]{1,200}", input_file_id):
            raise PolicyError("Expected an uploaded Batch input file ID")
        return {"input_file_id": input_file_id, "endpoint": self._batch(batch_id)["endpoint"], "completion_window": "24h"}

    def ingest_results(self, batch_id: str, jsonl: bytes) -> int:
        """Atomic, unordered, partial result import. Identical replay is harmless."""
        self._batch(batch_id)
        if not isinstance(jsonl, bytes) or len(jsonl) > self.max_bytes:
            raise PolicyError("Result import exceeds the configured bound; split into chunks")
        values = {}
        for line in jsonl.splitlines():
            if not line.strip():
                continue
            item = decode(line)
            key = item.get("custom_id")
            if not isinstance(key, str) or key in values:
                raise PolicyError("Missing or duplicate Batch result custom_id")
            response, error = item.get("response"), item.get("error")
            if (response is None) == (error is None):
                raise PolicyError("Batch result needs exactly one response or error")
            if response is not None and (not isinstance(response, dict) or type(response.get("status_code")) is not int
                                         or not 100 <= response["status_code"] <= 599 or "body" not in response):
                raise PolicyError("Malformed Batch response")
            if error is not None and (not isinstance(error, dict) or not isinstance(error.get("code"), str)):
                raise PolicyError("Malformed Batch error")
            values[key] = encode(item)
            if len(values) > self.max_requests:
                raise PolicyError("Too many imported results")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            for key, result in values.items():
                row = db.execute("SELECT result FROM jobs WHERE custom_id=? AND batch_id=?", (key, batch_id)).fetchone()
                if row is None or (row[0] is not None and row[0] != result):
                    raise PolicyError("Unknown or conflicting Batch result")
                db.execute("UPDATE jobs SET result=? WHERE custom_id=?", (result, key))
            db.commit()
        return len(values)

    def results(self, batch_id: str) -> dict:
        self._batch(batch_id)
        with closing(self._connect()) as db:
            return {row["custom_id"]: decode(row["result"]) if row["result"] else None
                    for row in db.execute("SELECT custom_id,result FROM jobs WHERE batch_id=? ORDER BY seq", (batch_id,))}
