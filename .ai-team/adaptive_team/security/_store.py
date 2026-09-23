"""Durable local security state, outside all learning rollback capsules."""
import sqlite3
import math
from contextlib import closing, contextmanager
from ..code_integration._git import checked_path
from ..models import PolicyError, digest


def scope_key(project: str, principal: str, endpoint: str, tool: str) -> str:
    # Principals come from authenticated owner configuration, never tool text.
    # Use a stable role/worker principal across lease retries to avoid resets.
    values = (project, principal, endpoint, tool)
    if any(not isinstance(v, str) or not v or len(v) > 4096 for v in values):
        raise PolicyError("Invalid security scope")
    return digest(values)


class SecurityStore:
    def __init__(self, database, *, timeout=10):
        self.timeout = timeout
        self.database = checked_path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def edit(self):
        with closing(sqlite3.connect(checked_path(self.database), timeout=self.timeout, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise PolicyError("Invalid trusted security clock")
    return float(value)
