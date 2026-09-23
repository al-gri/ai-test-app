"""Sanitized lesson bridge to existing Portable Releases; never copy live state.

Transfer authorizations bind the exact content and destination epoch. Signing
keys remain in trusted services. Imported lessons are reference data, not policy.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..evolution import verify
from ..models import PolicyError, canonical, digest, identifier
from ..portable import _validate_lesson
from ..code_integration._git import checked_path
from .vector_store import Lesson, VectorStore, bounded_text


class PortableMemory:
    def __init__(self, store: VectorStore, authorities: dict):
        self.store, self.authorities = store, authorities

    def export(self, lesson_id: str, content: dict, approval: dict) -> dict:
        """Return the existing portable lesson schema after signed sanitization.

        content is an explicit, reviewed redaction; no source log, vector or
        credential is automatically copied to another project.
        """
        epoch = self.store.state.epoch
        lesson = self.store.get(lesson_id)
        if lesson is None:
            raise PolicyError("Unknown source lesson")
        actor, grant = verify(approval, self.authorities, "export_memory")
        expected = {"project": self.store.state.project, "epoch": epoch, "lesson_id": lesson_id,
                    "source_digest": digest(asdict(lesson)), "content_digest": digest(content), "sanitized": True}
        if grant != expected:
            raise PolicyError("Portable export approval does not bind the exact redaction")
        if not isinstance(content, dict) or set(content) != {"problem", "solution", "applicability", "tags", "evidence_refs"}:
            raise PolicyError("Invalid portable redaction schema")
        for key, size in (("problem", 2000), ("solution", 4000), ("applicability", 1000)):
            bounded_text(content[key], key, size)
        if not isinstance(content["tags"], list) or len(content["tags"]) > 20:
            raise PolicyError("Invalid portable tags")
        for tag in content["tags"]:
            bounded_text(tag, "tag", 100)
        if not isinstance(content["evidence_refs"], list) or not 1 <= len(content["evidence_refs"]) <= 20:
            raise PolicyError("Structured portable memory requires 1..20 evidence references")
        for reference in content["evidence_refs"]:
            bounded_text(reference, "portable evidence reference", 1000)
        value = {"schema_version": 1, "id": digest({"source": lesson_id, "content": content}),
            "lesson": canonical({k: v for k, v in content.items() if k != "evidence_refs"}),
            "evidence_refs": content["evidence_refs"], "scope": "portable", "sanitized": True,
            "approved_by": actor, "approved_at": datetime.now(timezone.utc).isoformat()}
        _validate_lesson("memory/portable/lessons/" + value["id"] + ".json", canonical(value).encode())
        if self.store.state.epoch != epoch:
            raise PolicyError("Rollback invalidated memory export")
        return value

    @staticmethod
    def write_to_install(install_directory: str | Path, lesson: dict) -> Path:
        root = checked_path(Path(install_directory) / "memory/portable/lessons")
        _validate_lesson("memory/portable/lessons/" + lesson.get("id", "") + ".json", canonical(lesson).encode())
        root.mkdir(parents=True, exist_ok=True)
        destination = checked_path(root / (lesson["id"] + ".json"))
        # Exclusive creation: transfer never overwrites an existing release.
        with open(destination, "x", encoding="utf-8") as stream:
            stream.write(canonical(lesson) + "\n")
        return destination

    def import_lessons(self, lessons: list[dict], approval: dict):
        if not isinstance(lessons, list) or not 1 <= len(lessons) <= 100:
            raise PolicyError("Portable import requires 1..100 reviewed lessons")
        lessons = json.loads(canonical(lessons))
        actor, grant = verify(approval, self.authorities, "import_memory")
        epoch = self.store.state.epoch
        role = identifier(grant.get("role_id"), "imported lesson role")
        if grant != {"project": self.store.state.project, "epoch": epoch, "role_id": role,
                     "release_digest": digest(lessons), "reviewed": True}:
            raise PolicyError("Destination must approve the exact portable release")
        prepared, seen = [], set()
        for item in lessons:
            _validate_lesson("memory/portable/lessons/" + item.get("id", "") + ".json", canonical(item).encode())
            if item["id"] in seen:
                raise PolicyError("Duplicate portable lesson")
            seen.add(item["id"])
            from ..protocols.jsonrpc import decode
            data = decode(item["lesson"].encode(), limit=24000)
            if set(data) != {"problem", "solution", "applicability", "tags"} or not isinstance(data["tags"], list):
                raise PolicyError("Portable lesson needs the Block 3 structured content schema")
            source = digest(item)
            lesson = Lesson(digest({"project": self.store.state.project, "source": source, "role": role}),
                self.store.state.project, role, data["problem"], data["solution"], data["applicability"],
                tuple(item["evidence_refs"]), "portable-import", source, digest("portable-reference-no-local-prompt"),
                tuple(data["tags"]), "portable")
            prepared.append((lesson, self.store.prepare(lesson)))
        # All-or-nothing; vectors are rebuilt using the destination's embedder.
        with self.store.state.transaction(expected_epoch=epoch) as db:
            for lesson, values in prepared:
                self.store._put(db, lesson, values)
        return [lesson.id for lesson, _ in prepared]
