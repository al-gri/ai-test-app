"""Encrypted recovery copy for a known RPC outcome when SQLite cannot commit.

Not a replay permission: a pending SQL intent must already exist. Cache first,
then publish an AEAD file, then complete SQL. A process can retain a known result
even if both storage devices fail; after such a process dies the durable intent
still blocks blind replay. No storage design can promise durability on total IO
failure. The outbox directory must remain outside worker mounts and snapshots.
"""
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from ..models import PolicyError, canonical
from ..code_integration._git import checked_path
from .task_files import _sync_directory


class ResultOutbox:
    FIELDS = ('request','result','telemetry','checkpoint')
    def __init__(self, journal):
        self.journal = journal
        self.root = Path(journal.database + '.results')
        self.lock = threading.RLock()
        self.cache = {}
        self.failures = 0

    def _path(self, key):
        return checked_path(self.root / (hashlib.sha256(key.encode()).hexdigest()+'.json'))

    def _decode_file(self, raw, key):
        envelope = json.loads(raw)
        if (set(envelope) != {'format','parts'} or envelope['format'] != 1
                or set(envelope['parts']) != set(self.FIELDS)):
            raise PolicyError('Invalid recovery envelope')
        return {field:self.journal._decode(envelope['parts'][field],key,'known_outcome:'+field)
                for field in self.FIELDS}

    def remember(self, key, request, result, telemetry, checkpoint):
        # Deep, canonical copy: caller mutation cannot relabel a completed effect.
        value = json.loads(canonical(dict(request=request, result=result,
            telemetry=list(telemetry), checkpoint=checkpoint)))
        with self.lock:
            old = self.cache.get(key)
            if old is not None and old != value:
                raise PolicyError('Conflicting known RPC outcome')
            self.cache[key] = value
        return value

    def get(self, key):
        with self.lock:
            value = self.cache.get(key)
            if value is not None:
                return json.loads(canonical(value))
        path = self._path(key)
        if not path.exists():
            return None
        # Four individually bounded ciphertexts plus base64/envelope overhead.
        if path.stat().st_size > 96*1024*1024:
            raise PolicyError('Oversized RPC result outbox')
        return self._decode_file(path.read_text(encoding='utf-8'),key)

    def persist(self, key, value):
        # Runs outside every SQLite transaction. No overwrite of another result.
        checked_path(self.root).mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = self._path(key)
        if destination.exists():
            previous = self._decode_file(destination.read_text(encoding='utf-8'),key)
            if previous != value:
                raise PolicyError('Conflicting durable RPC outcome')
            return
        # Request and result may EACH be near the AEAD payload bound. Encrypt
        # them independently with distinct AAD; adding them must not reject a
        # valid known response merely because the recovery wrapper is larger.
        encoded = canonical({'format':1,'parts':{field:self.journal._encode(
            value[field],key,'known_outcome:'+field) for field in self.FIELDS}}).encode()
        temp = checked_path(self.root / (uuid.uuid4().hex+'.tmp'))
        try:
            fd = os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:
                stream.write(encoded);stream.flush();os.fsync(stream.fileno())
            try:
                os.link(temp,destination)
            except FileExistsError:
                previous = self._decode_file(destination.read_text(encoding='utf-8'),key)
                if previous != value:
                    raise PolicyError('Concurrent conflicting RPC outcome')
            _sync_directory(self.root)
        finally:
            if temp.exists():
                temp.unlink()

    def discard(self, key):
        # Only after SQL completion. Leftover files are harmless and replayable.
        path = self._path(key)
        if path.exists():
            path.unlink();_sync_directory(self.root)
        with self.lock:
            self.cache.pop(key,None)
