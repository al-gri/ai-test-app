"""Owner-provisioned key ring, outside repositories, worker mounts and backups.

The file adapter is for a protected local service account. Deployments can inject
a KMS/HSM implementation of get()/active_id without changing journal encryption.
On Windows the owner must set an ACL limited to the controller service identity.
"""
import json
import os
import stat
from pathlib import Path
from ..models import PolicyError, identifier
from ..code_integration._git import checked_path


class KeyUnavailable(PolicyError):
    pass


class FileKeyProvider:
    def __init__(self, path):
        try:
            path = checked_path(path)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384 or info.st_nlink != 1:
                raise KeyUnavailable('Invalid key ring file')
            if os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid != os.geteuid()):
                raise KeyUnavailable('Key ring must belong to service user with mode 0600')
            value = json.loads(path.read_text(encoding='utf-8'))
            self.active_id = identifier(value['active'], 'key ID')
            self._keys = {identifier(k, 'key ID'): bytes.fromhex(v) for k,v in value['keys'].items()}
            if not 1 <= len(self._keys) <= 32 or any(len(k) != 32 for k in self._keys.values()):
                raise KeyUnavailable('AES-256 wrapping keys required')
            self.get(self.active_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise KeyUnavailable('Cannot load protected owner key ring') from None

    def get(self, key_id):
        try:
            return self._keys[key_id]
        except KeyError:
            raise KeyUnavailable('Required journal key is unavailable') from None

    @classmethod
    def from_environment(cls):
        path = os.environ.get('AI_TEAM_KEY_FILE')
        if not path or not Path(path).is_absolute():
            raise KeyUnavailable('Set AI_TEAM_KEY_FILE to an absolute protected key ring path')
        return cls(path)


def create_keyring(path, key_id='primary'):
    """Explicit owner provisioning; never automatically called by a journal."""
    identifier(key_id, 'key ID')
    path = checked_path(path)
    raw = json.dumps({'active': key_id, 'keys': {key_id: os.urandom(32).hex()}}).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(raw); out.flush(); os.fsync(out.fileno())


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Provision a new external key ring (never overwrite)')
    parser.add_argument('path')
    args = parser.parse_args()
    create_keyring(args.path)
