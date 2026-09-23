"""Small local exact-cosine index, persisted transactionally with its documents.

HashEmbedding is an explicitly lexical test/local baseline, not a semantic model.
Deployments can inject a measured embedding adapter with a distinct stable ID.
Vectors from different models/dimensions are never compared or silently mixed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Protocol

from ..models import PolicyError, canonical, digest, identifier
from .state import LearningState


class Embedder(Protocol):
    model_id: str
    dimensions: int
    def embed(self, text: str) -> list[float]: ...


class HashEmbedding:
    model_id = "local-hash-lexical-v1-256"
    dimensions = 256

    def embed(self, text):
        values = [0.0] * self.dimensions
        for word in re.findall(r"\w+", text.casefold(), flags=re.UNICODE):
            token = hashlib.sha256(word.encode()).digest()
            values[int.from_bytes(token[:4], "big") % self.dimensions] += 1 if token[4] & 1 else -1
        return values


def bounded_text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise PolicyError("Invalid " + label)
    return value


def vector(values, dimensions):
    if not isinstance(values, (list, tuple)) or len(values) != dimensions:
        raise PolicyError("Embedding dimension mismatch")
    if any(type(v) not in (float, int) or not math.isfinite(v) or abs(v) > 1e6 for v in values):
        raise PolicyError("Invalid embedding component")
    norm = math.hypot(*values)
    if norm == 0:
        raise PolicyError("Zero embedding cannot be indexed")
    return [v / norm for v in values]


@dataclass(frozen=True)
class Lesson:
    id: str
    project: str
    role: str
    problem: str
    solution: str
    applicability: str
    evidence: tuple[str, ...]
    source_task: str
    artifact_digest: str
    prompt_digest: str
    tags: tuple[str, ...] = ()
    scope: str = "project"

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[0-9a-f]{64}", self.id):
            raise PolicyError("Lesson ID must be a content-bound hash")
        for value in (self.project, self.role, self.source_task):
            identifier(value, "lesson attribution")
        for key, size in (("problem", 2000), ("solution", 4000), ("applicability", 1000)):
            bounded_text(getattr(self, key), key, size)
        for value in (self.artifact_digest, self.prompt_digest):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise PolicyError("Lesson needs immutable evidence identities")
        if not isinstance(self.evidence, tuple) or not 1 <= len(self.evidence) <= 20:
            raise PolicyError("Lesson requires bounded evidence references")
        for ref in self.evidence:
            bounded_text(ref, "evidence reference", 1000)
        if not isinstance(self.tags, tuple) or len(self.tags) > 20:
            raise PolicyError("Invalid lesson tags")
        for tag in self.tags:
            bounded_text(tag, "tag", 100)
        if self.scope not in {"project", "portable"}:
            raise PolicyError("Invalid lesson scope")

    @classmethod
    def load(cls, data):
        try:
            return cls(**{**data, "evidence": tuple(data["evidence"]), "tags": tuple(data.get("tags", ()))})
        except (KeyError, TypeError) as exc:
            raise PolicyError("Malformed lesson") from exc

    @property
    def search_text(self):
        return "\n".join((self.problem, self.solution, self.applicability, " ".join(self.tags)))


class VectorStore:
    def __init__(self, state: LearningState, embedder: Embedder | None = None, *, max_documents=10000):
        self.state, self.embedder = state, embedder or HashEmbedding()
        bounded_text(self.embedder.model_id, "embedding model ID", 200)
        if type(self.embedder.dimensions) is not int or not 8 <= self.embedder.dimensions <= 4096:
            raise PolicyError("Embedding dimensions out of bounds")
        if type(max_documents) is not int or not 1 <= max_documents <= 10000:
            raise PolicyError("Local index document bound must be 1..10000")
        self.max_documents = max_documents

    def prepare(self, lesson: Lesson):
        if lesson.project != self.state.project:
            raise PolicyError("Cross-project lesson requires explicit portable import")
        return vector(self.embedder.embed(lesson.search_text), self.embedder.dimensions)

    def _put(self, db, lesson, values):
        # Called inside the same transaction as reflection completion/import.
        if lesson.project != self.state.project:
            raise PolicyError("Lesson project mismatch")
        document, encoded = canonical(asdict(lesson)), canonical(vector(values, self.embedder.dimensions))
        row = db.execute("SELECT * FROM lessons WHERE id=?", (lesson.id,)).fetchone()
        if row:
            if row["document"] != document or row["embedding"] != self.embedder.model_id or row["dimensions"] != self.embedder.dimensions:
                raise PolicyError("Conflicting lesson identity or embedding migration")
            return lesson.id
        if db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] >= self.max_documents:
            raise PolicyError("Local memory capacity reached; compact through reviewed maintenance")
        db.execute('INSERT INTO memory_subjects VALUES(?,?,?) ON CONFLICT(project,task_id,role_id) DO NOTHING',
            (lesson.project,lesson.source_task,lesson.role))
        db.execute('INSERT INTO post_mortems VALUES(?,?,?,?,?,?,?)',
            (lesson.id,lesson.project,lesson.source_task,lesson.role,lesson.artifact_digest,lesson.prompt_digest,document))
        db.execute('INSERT INTO memory_vectors VALUES(?,?,?,?)',
            (lesson.id,encoded,self.embedder.model_id,self.embedder.dimensions))
        return lesson.id

    def put(self, lesson: Lesson, *, expected_epoch: str):
        values = self.prepare(lesson)
        with self.state.transaction(expected_epoch=expected_epoch) as db:
            return self._put(db, lesson, values)

    def get(self, lesson_id):
        with self.state.transaction() as db:
            row = db.execute("SELECT document FROM lessons WHERE id=?", (lesson_id,)).fetchone()
            return Lesson.load(json.loads(row[0])) if row else None

    def search(self, query: str, *, role: str, top_k=5, min_score=.15, expected_epoch=None):
        bounded_text(query, "retrieval query", 8000); identifier(role, "retrieval role")
        if type(top_k) is not int or not 1 <= top_k <= 10:
            raise PolicyError("Retrieval top_k must be 1..10")
        if type(min_score) not in (int, float) or not math.isfinite(min_score) or not 0 <= min_score <= 1:
            raise PolicyError("Invalid retrieval score threshold")
        epoch = expected_epoch or self.state.epoch
        values = vector(self.embedder.embed(query), self.embedder.dimensions)
        with self.state.transaction(expected_epoch=epoch) as db:
            rows = db.execute("SELECT * FROM lessons WHERE embedding=? AND dimensions=? ORDER BY id LIMIT ?",
                (self.embedder.model_id, self.embedder.dimensions, self.max_documents + 1)).fetchall()
            if len(rows) > self.max_documents:
                raise PolicyError("Index exceeds the configured search bound")
            results = []
            for row in rows:
                lesson = Lesson.load(json.loads(row["document"]))
                if lesson.project != self.state.project or lesson.role != role:
                    continue
                stored = vector(json.loads(row["vector"]), self.embedder.dimensions)
                score = max(-1.0, min(1.0, sum(a * b for a, b in zip(values, stored))))
                if score >= min_score:
                    results.append({"lesson": asdict(lesson), "score": score, "epoch": epoch})
            return sorted(results, key=lambda item: (-item["score"], item["lesson"]["id"]))[:top_k]
