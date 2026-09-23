"""Per-principal/tool circuit breaker with durable, single-probe half-open state."""
import time
import uuid
from dataclasses import dataclass
from ..models import PolicyError, integer
from ._store import SecurityStore, timestamp


class CircuitOpen(PolicyError):
    pass


@dataclass(frozen=True)
class Permit:
    key: str
    token: str
    generation: int


class CircuitBreaker:
    def __init__(self, database, *, threshold=3, cooldown_seconds=30, probe_seconds=60, clock=time.time):
        integer(threshold, "failure threshold", 1)
        if threshold > 1000 or not 0 < timestamp(cooldown_seconds) <= 86400 or not 0 < timestamp(probe_seconds) <= 3600:
            raise PolicyError("Invalid breaker policy")
        self.store, self.threshold = SecurityStore(database), threshold
        self.cooldown, self.probe_seconds, self.clock = cooldown_seconds, probe_seconds, clock
        with self.store.edit() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS circuits(key TEXT PRIMARY KEY, failures INTEGER, until REAL,
                    generation INTEGER, probe TEXT, probe_until REAL, threshold INTEGER, cooldown REAL, duration REAL, last REAL);
                CREATE TABLE IF NOT EXISTS permits(token TEXT PRIMARY KEY, key TEXT, generation INTEGER, done INTEGER);
            """)

    def acquire(self, key):
        from ..orchestration.task_lifecycle import object_id
        object_id(key)
        now, token, denied = timestamp(self.clock()), uuid.uuid4().hex, False
        with self.store.edit() as db:
            row = db.execute("SELECT * FROM circuits WHERE key=?", (key,)).fetchone()
            if row is None:
                if db.execute("SELECT COUNT(*) FROM circuits").fetchone()[0] >= 10000:
                    raise PolicyError("Circuit scope capacity reached")
                db.execute("INSERT INTO circuits VALUES(?,0,0,0,NULL,0,?,?,?,?)", (key, self.threshold, self.cooldown, self.probe_seconds, now))
                row = db.execute("SELECT * FROM circuits WHERE key=?", (key,)).fetchone()
            if (row['threshold'], row['cooldown'], row['duration']) != (self.threshold, self.cooldown, self.probe_seconds):
                raise PolicyError("Breaker policy drift requires maintenance")
            now = max(now, row['last'])
            generation, until, probe = row['generation'], row['until'], row['probe']
            if probe and now >= row['probe_until']:
                generation += 1
                until, probe = now + self.cooldown, None
                db.execute("UPDATE circuits SET generation=?,until=?,probe=NULL,probe_until=0 WHERE key=?", (generation, until, key))
            if now < until or probe:
                denied = True
            elif row['failures'] >= self.threshold:
                db.execute("UPDATE circuits SET probe=?,probe_until=? WHERE key=?", (token, now + self.probe_seconds, key))
            db.execute("UPDATE circuits SET last=? WHERE key=?", (now, key))
            if not denied:
                # Old acknowledged permits are dispensable; active ones are kept
                # until completion or explicit recovery, never silently reused.
                db.execute("DELETE FROM permits WHERE key=? AND done=1", (key,))
                if db.execute("SELECT COUNT(*) FROM permits WHERE key=? AND done=0", (key,)).fetchone()[0] >= 1000:
                    raise PolicyError("Too many unresolved circuit permits")
                db.execute("INSERT INTO permits VALUES(?,?,?,0)", (token, key, generation))
        if denied:
            raise CircuitOpen("Tool circuit is open; no request was sent")
        return Permit(key, token, generation)

    def finish(self, permit: Permit, *, success: bool):
        if type(success) is not bool:
            raise PolicyError("Breaker result must be a boolean")
        now = timestamp(self.clock())
        with self.store.edit() as db:
            saved = db.execute("SELECT * FROM permits WHERE token=?", (permit.token,)).fetchone()
            if saved is None:
                return  # Previously acknowledged and pruned; never count twice.
            if saved['key'] != permit.key or saved['generation'] != permit.generation:
                raise PolicyError("Breaker permit identity mismatch")
            if saved['done']:
                return
            db.execute("UPDATE permits SET done=1 WHERE token=?", (permit.token,))
            row = db.execute("SELECT * FROM circuits WHERE key=?", (permit.key,)).fetchone()
            if row['generation'] != permit.generation:
                return  # A late success cannot close a newer open circuit.
            now = max(now, row['last'])
            if row['probe'] and (row['probe'] != permit.token or now >= row['probe_until']):
                return
            if success:
                db.execute("UPDATE circuits SET failures=0,until=0,probe=NULL,probe_until=0,last=? WHERE key=?", (now, permit.key))
            else:
                failures = row['failures'] + 1
                opened = failures >= self.threshold
                db.execute("UPDATE circuits SET failures=?,until=?,generation=?,probe=NULL,probe_until=0,last=? WHERE key=?",
                    (failures, now + self.cooldown if opened else 0, row['generation'] + int(opened), now, permit.key))

    def reset(self, key):
        """Trusted owner-maintenance API; never expose as an agent tool."""
        with self.store.edit() as db:
            db.execute("UPDATE circuits SET failures=0,until=0,generation=generation+1,probe=NULL,probe_until=0 WHERE key=?", (key,))
