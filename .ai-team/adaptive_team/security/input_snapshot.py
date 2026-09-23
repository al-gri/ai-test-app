"""Bounded byte snapshots into a private daemon-side tmpfs volume.

The trusted loader is paused after measuring copied bytes. Only that loader ever
has a writable mount; candidate mounts are read-only. Docker/root remain trusted.
No original host directory, hardlink, symlink or reparse point enters the worker.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid
from ..models import PolicyError, digest
from ..code_integration._git import checked_path

MAX_BYTES = 32 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_ENTRIES = 10000


def _collect(source, destination=None):
    root = checked_path(source)
    if not root.is_dir(): raise PolicyError('Snapshot source must be a directory')
    entries, total = [], 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs+files):
            path = checked_path(Path(base)/name)
            relative = path.relative_to(root).as_posix()
            if len(entries) >= MAX_ENTRIES or len(relative) > 1024:
                raise PolicyError('Snapshot entry limit exceeded')
            info = path.lstat()
            target = Path(destination)/relative if destination else None
            if stat.S_ISDIR(info.st_mode):
                entries.append({'path': relative, 'type': 'directory', 'mode': 0o755})
                if target: target.mkdir(parents=True, exist_ok=False)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if info.st_size > MAX_FILE: raise PolicyError('Snapshot file limit exceeded')
                fd = os.open(path, os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_BINARY',0))
                with os.fdopen(fd,'rb') as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise PolicyError('Snapshot inode changed')
                    content = stream.read(MAX_FILE+1)
                    after = os.fstat(stream.fileno())
                current = path.lstat()
                identity = lambda v: (v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns,v.st_nlink)
                if identity(info) != identity(before) or identity(before) != identity(after) or identity(after) != identity(current):
                    raise PolicyError('Source changed during snapshot capture')
                total += len(content)
                if len(content)>MAX_FILE or total>MAX_BYTES:
                    raise PolicyError('Snapshot total size limit exceeded')
                mode = 0o755 if info.st_mode & 0o111 else 0o644
                entries.append({'path':relative, 'type':'file', 'mode':mode,
                    'bytes':len(content), 'sha256':hashlib.sha256(content).hexdigest()})
                if target:
                    with target.open('xb') as out: out.write(content)
                    target.chmod(mode)
            else:
                raise PolicyError('Hardlinks, links and special files are forbidden in snapshots')
    return sorted(entries,key=lambda e:e['path'])


def measure_input(source):
    """Owner-side measurement; bind to the approved artifact BEFORE execution.

    Use a stopped writer/pinned checkout. A self-selected worker digest is not
    authorization. Sandbox compares this value to separately copied input bytes.
    """
    return digest(_collect(source))


# This fixed program only measures data. -I -S prevents importing from the input.
# It runs in an approved utility image before candidate code can run.
MEASURE = r'''
import os,json,hashlib,stat
from pathlib import Path
p=Path('/snapshot'); rows=[]; total=0
assert any(' /snapshot ' in x and ' - tmpfs ' in x for x in Path('/proc/self/mountinfo').read_text().splitlines())
for base,dirs,files in os.walk(p,followlinks=False):
 for name in sorted(dirs+files):
  item=Path(base)/name; s=item.lstat(); rel=item.relative_to(p).as_posix()
  assert not item.is_symlink() and len(rows)<10000
  if stat.S_ISDIR(s.st_mode): rows.append({'path':rel,'type':'directory','mode':493})
  else:
   assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_size<=8388608
   data=item.read_bytes();total+=len(data);assert total<=33554432
   rows.append({'path':rel,'type':'file','mode':493 if s.st_mode&73 else 420,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
rows.sort(key=lambda x:x['path'])
raw=json.dumps(rows,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
print(hashlib.sha256(raw).hexdigest())
'''


@dataclass(frozen=True)
class InputSnapshot:
    volume: str
    loader: str
    loader_id: str
    digest: str


class DockerSnapshotter:
    def __init__(self, docker, watchdog, image):
        self.docker, self.watchdog, self.image = docker, watchdog, image

    def create(self, source, expected_digest, *, ttl):
        token = uuid.uuid4().hex
        volume, name = 'ai-team-input-'+token, 'ai-team-'+token
        labels = self.watchdog.arm(name, ttl)
        # A Docker local volume backed by tmpfs can be mounted in the trusted
        # loader and read-only worker. This is NOT an ordinary --tmpfs mount.
        self.docker.command(['volume','create','--driver','local','--opt','type=tmpfs',
            '--opt','device=tmpfs','--opt','o=size=64m,nodev,nosuid,noexec',
            '--label','adaptive-team.snapshot='+name,volume])
        created_id = None
        try:
            args = ['create','--name',name,'--pull=never','--network=none','--read-only',
                '--cap-drop=ALL','--security-opt=no-new-privileges=true','--security-opt=seccomp=builtin',
                '--memory=128m','--memory-swap=128m','--cpus=1','--pids-limit=32','--user=0:0',
                '--log-driver=none','--restart=no','--no-healthcheck',
                '--mount','type=volume,source='+volume+',target=/snapshot,volume-nocopy']
            for key,value in labels.items(): args += ['--label', key+'='+value]
            args += ['--entrypoint','python',self.image,'-I','-S','-c','import time; time.sleep(4000)']
            created_id = self.docker.command(args).strip()
            self.watchdog.bind(name,created_id)
            self.docker.command(['start',created_id])
            with tempfile.TemporaryDirectory(prefix='ai-input-copy-') as tmp:
                copied = digest(_collect(source, tmp))
                if copied != expected_digest: raise PolicyError('Approved input digest differs from copied bytes')
                self.docker.command(['cp',str(Path(tmp))+os.sep+'.',created_id+':/snapshot'],timeout=60)
            measured = self.docker.command(['exec',created_id,'python','-I','-S','-c',MEASURE],timeout=30).strip()
            if measured != expected_digest: raise PolicyError('Daemon snapshot digest mismatch')
            self.docker.command(['pause',created_id])
            snapshot = InputSnapshot(volume,name,created_id,measured)
            self.verify(snapshot)
            return snapshot
        except BaseException:
            # Retain the original exception unless termination is uncertain.
            self.cleanup(InputSnapshot(volume,name,created_id or '',expected_digest))
            raise

    def verify(self, snapshot):
        volume = json.loads(self.docker.command(['volume','inspect',snapshot.volume]))[0]
        if (volume.get('Driver')!='local' or volume.get('Options',{}).get('type')!='tmpfs'
                or volume.get('Labels',{}).get('adaptive-team.snapshot')!=snapshot.loader):
            raise PolicyError('Snapshot volume ownership/type changed')
        value = json.loads(self.docker.command(['inspect',snapshot.loader_id]))[0]
        if value.get('Id')!=snapshot.loader_id or value.get('State',{}).get('Paused') is not True:
            raise PolicyError('Snapshot writer is not sealed')

    def cleanup(self, snapshot):
        row = self.watchdog.get(snapshot.loader)
        present = self.docker.command(['ps','--all','--no-trunc','--filter','id='+snapshot.loader_id,'--format','{{.ID}}']).splitlines() if snapshot.loader_id else []
        if present and present != [snapshot.loader_id]: raise PolicyError('Unexpected snapshot loader identity')
        if present:
            value = json.loads(self.docker.command(['inspect',snapshot.loader_id]))[0]
            labels = value.get('Config',{}).get('Labels',{})
            from .watchdog import TOKEN
            if not row or value.get('Id')!=row['container_id'] or labels.get(TOKEN)!=row['token']:
                raise PolicyError('Snapshot cleanup identity mismatch')
            if value['State'].get('Paused'): self.docker.command(['unpause',snapshot.loader_id])
            if value['State'].get('Running'): self.docker.command(['kill',snapshot.loader_id])
            value = json.loads(self.docker.command(['inspect',snapshot.loader_id]))[0]
            self.watchdog.stopped(snapshot.loader,snapshot.loader_id,value['State'])
            self.docker.command(['rm',snapshot.loader_id])
        elif snapshot.loader_id and (not row or row['status'] != 'stopped'):
            raise PolicyError('Missing snapshot loader without termination proof')
        volumes = self.docker.command(['volume','ls','--filter','name=^'+snapshot.volume+'$','--format','{{.Name}}']).splitlines()
        if not volumes: return
        if volumes != [snapshot.volume]: raise PolicyError('Unexpected snapshot volume identity')
        volume = json.loads(self.docker.command(['volume','inspect',snapshot.volume]))[0]
        if volume.get('Labels',{}).get('adaptive-team.snapshot') != snapshot.loader:
            raise PolicyError('Snapshot volume cleanup identity mismatch')
        self.docker.command(['volume','rm',snapshot.volume])
