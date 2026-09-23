"""System-level dependency ownership from real Git paths, not patch prose.

Use --name-status -z --no-renames to report a rename as deletion plus creation.
The parser also understands R/C records defensively and checks BOTH paths.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from ..models import PolicyError, scope


OWNER = "dependency_manager"
PROTECTED_NAMES = frozenset({"package.json", "package-lock.json", "requirements.txt", "poetry.lock"})


def protected(path: str) -> bool:
    return PurePosixPath(scope(path)).name.casefold() in PROTECTED_NAMES


def enforce_paths(role: str, paths) -> None:
    blocked = sorted({scope(path) for path in paths if protected(path)})
    if blocked and role != OWNER:
        raise PolicyError("Only dependency_manager may change dependency files: " + ", ".join(blocked))


def parse_name_status(data: bytes) -> tuple[str, ...]:
    if not isinstance(data, bytes) or len(data) > 8 * 1024 * 1024:
        raise PolicyError("Invalid or oversized Git name-status report")
    if not data:
        return ()
    if not data.endswith(b"\0"):
        raise PolicyError("Name-status report must be NUL terminated")
    tokens = data[:-1].split(b"\0")
    paths = []
    index = 0
    try:
        while index < len(tokens):
            status = tokens[index].decode("ascii")
            index += 1
            if not re.fullmatch(r"[ADMTUXB]|[RC][0-9]{1,3}", status):
                raise PolicyError("Malformed Git change status")
            count = 2 if status[0] in "RC" else 1
            if index + count > len(tokens):
                raise PolicyError("Truncated Git name-status record")
            for value in tokens[index:index + count]:
                paths.append(scope(value.decode("utf-8", "strict")))
            index += count
    except (UnicodeError, IndexError) as exc:
        raise PolicyError("Malformed Git path encoding") from exc
    return tuple(paths)


def enforce_diff(role: str, data: bytes) -> None:
    enforce_paths(role, parse_name_status(data))
