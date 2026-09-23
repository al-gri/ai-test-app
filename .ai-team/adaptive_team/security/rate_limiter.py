"""Atomic sliding-window admission; denial never sleeps or queues a live lease."""
import time
from ..models import PolicyError, integer
from ._store import SecurityStore, timestamp


class RateLimited(PolicyError):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__("Request rate limit exceeded")


class RateLimiter:
    def __init__(self, database, *, requests=5, window_seconds=1.0, clock=time.time):
        integer(requests, "request limit", 1)
        if requests > 10000 or not 0 < timestamp(window_seconds) <= 3600:
            raise PolicyError("Rate policy exceeds bounds")
        self.store, self.limit, self.window, self.clock = SecurityStore(database), requests, float(window_seconds), clock
        with self.store.edit() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS rate_config(key TEXT PRIMARY KEY, quota INTEGER, window REAL, last REAL);"
                "CREATE TABLE IF NOT EXISTS rate_events(id INTEGER PRIMARY KEY, key TEXT, at REAL);"
                "CREATE INDEX IF NOT EXISTS rate_lookup ON rate_events(key,at);")

    def acquire(self, key):
        from ..orchestration.task_lifecycle import object_id
        object_id(key)
        now = timestamp(self.clock())
        denied = None
        with self.store.edit() as db:
            row = db.execute("SELECT * FROM rate_config WHERE key=?", (key,)).fetchone()
            if row and (row['quota'] != self.limit or row['window'] != self.window):
                raise PolicyError("Rate policy drift requires maintenance")
            if row:
                now = max(now, row['last'])  # Clock rollback cannot refill capacity.
            elif db.execute("SELECT COUNT(*) FROM rate_config").fetchone()[0] >= 10000:
                raise PolicyError("Security scope capacity reached")
            db.execute("INSERT OR REPLACE INTO rate_config VALUES(?,?,?,?)", (key, self.limit, self.window, now))
            db.execute("DELETE FROM rate_events WHERE key=? AND at<=?", (key, now - self.window))
            count, oldest = db.execute("SELECT COUNT(*),MIN(at) FROM rate_events WHERE key=?", (key,)).fetchone()
            if count >= self.limit:
                denied = max(0.0, oldest + self.window - now)
            else:
                db.execute("INSERT INTO rate_events(key,at) VALUES(?,?)", (key, now))
        if denied is not None:
            raise RateLimited(denied)
