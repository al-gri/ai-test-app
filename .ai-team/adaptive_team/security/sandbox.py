"""Disposable Linux Docker runner. Candidate code never runs on the controller.

The Docker daemon, approved image and this adapter are trusted infrastructure.
Use a dedicated patched Linux host/VM; a container is not a kernel-independent
security boundary. No Docker socket, host secrets or arbitrary host path is
exposed to the worker. Tests/images/commands are owner-configured contracts.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

from ..code_integration._git import checked_path
from ..models import PolicyError, digest
from ..orchestration.task_lifecycle import TaskLifecycle, RecoveryRequired
from ..observability.tracing import TraceContext


class SandboxUnavailable(RuntimeError):
    pass


class TerminationUnknown(RecoveryRequired):
    """Do not finish/refund a Team lease until external termination is proven."""


@dataclass(frozen=True)
class SandboxPolicy:
    image: str                        # Exact locally provisioned image ID.
    command: tuple[str, ...]          # Owner-controlled argv, never a shell string.
    timeout_seconds: int = 60
    memory_mb: int = 256
    cpus: int = 1
    pids: int = 64
    tmp_mb: int = 64
    output_bytes: int = 262144
    user: str = '10001:10001'

    def __post_init__(self):
        if not isinstance(self.image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', self.image):
            raise PolicyError("Sandbox image must be pinned to a local SHA256 image ID")
        if not isinstance(self.command, tuple) or not 1 <= len(self.command) <= 100 or any(
                not isinstance(v, str) or not v or len(v) > 8192 or '\0' in v for v in self.command):
            raise PolicyError("Sandbox command requires bounded immutable argv")
        for name, low, high in (('timeout_seconds',1,3600), ('memory_mb',32,4096), ('cpus',1,8),
                              ('pids',8,256), ('tmp_mb',1,512), ('output_bytes',1024,4*1024*1024)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise PolicyError("Invalid sandbox limit: " + name)
        if not re.fullmatch(r'[1-9][0-9]{0,5}:[1-9][0-9]{0,5}', self.user):
            raise PolicyError("Sandbox requires non-root numeric user and group")


@dataclass(frozen=True)
class ConsoleResult:
    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class SandboxEvidence:
    run_id: str
    subject_digest: str
    policy_digest: str
    stopped: bool
    timed_out: bool
    output_limited: bool
    console: ConsoleResult
    input_digest: str

    def agent_result(self):
        # The worker sees exactly three fields. Controller lifecycle/evidence
        # remains outside the untrusted agent protocol.
        return asdict(self.console)


class DockerCLI:
    def __init__(self, *, endpoint: str, config_directory: str | Path, allow_local_tcp=False):
        # Desktop may expose its local engine over TCP. This is an explicit
        # owner opt-in, never an inherited DOCKER_HOST or arbitrary remote URL.
        local_tcp = allow_local_tcp is True and endpoint in {'tcp://localhost:2375', 'tcp://127.0.0.1:2375', 'tcp://host.docker.internal:2375'}
        if not isinstance(endpoint, str) or not (endpoint.startswith('unix:///') or endpoint == 'npipe:////./pipe/docker_engine' or local_tcp):
            raise PolicyError("Only an explicitly configured local Docker socket is supported")
        executable = shutil.which('docker')
        if not executable:
            raise SandboxUnavailable("Docker executable is unavailable")
        config = checked_path(config_directory)
        config.mkdir(parents=True, exist_ok=True)
        if any(config.iterdir()):
            raise PolicyError("Sandbox Docker config directory must be empty")
        self.prefix = [executable, '--host', endpoint, '--config', str(config)]
        self.env = {k: v for k, v in os.environ.items() if k.lower() in {'systemroot','windir','path','pathext','temp','tmp'}}

    def command(self, args, *, timeout=10):
        # Docker management responses are bounded too; commands never use shell=True.
        result, limited, timed_out = self.stream(args, timeout=timeout, limit=1024*1024)
        if limited or timed_out:
            raise SandboxUnavailable("Docker control command exceeded its bound")
        if result.exit_code:
            raise SandboxUnavailable("Docker control command failed")
        return result.stdout

    def stream(self, args, *, timeout, limit):
        process = subprocess.Popen([*self.prefix, *args], env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        buffers, total = [bytearray(), bytearray()], [0]
        overflow, lock = threading.Event(), threading.Lock()
        def drain(stream, index):
            try:
                while chunk := stream.read(8192):
                    with lock:
                        available = max(0, limit - total[0])
                        buffers[index].extend(chunk[:available]); total[0] += len(chunk)
                        if total[0] > limit:
                            overflow.set()
            finally:
                stream.close()
        readers = [threading.Thread(target=drain, args=(stream, i), daemon=True)
                   for i, stream in enumerate((process.stdout, process.stderr))]
        for reader in readers: reader.start()
        deadline, expired = time.monotonic() + timeout, False
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline or overflow.is_set():
                    expired = time.monotonic() >= deadline
                    process.kill()  # Kills CLI only; runner must terminate container.
                    break
                time.sleep(.01)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
            for reader in readers: reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            raise SandboxUnavailable("Docker output reader did not terminate")
        return ConsoleResult(*(bytes(value).decode('utf-8', 'replace') for value in buffers), process.returncode), overflow.is_set(), expired


class Sandbox:
    def __init__(self, docker, journal: TaskLifecycle, *, workspace_root: str | Path, allowed_images: frozenset[str], traces=None, watchdog=None, snapshotter=None):
        self.docker, self.journal = docker, journal
        self.watchdog, self.snapshotter = watchdog, snapshotter
        self.workspace_root = checked_path(workspace_root)
        if not self.workspace_root.is_dir() or not isinstance(allowed_images, frozenset) or not allowed_images:
            raise PolicyError("Sandbox requires a workspace root and explicit image allowlist")
        self.allowed_images, self.traces = allowed_images, traces

    def validate(self, workspace, policy):
        path = checked_path(workspace)
        if not path.is_dir() or path == self.workspace_root or not path.is_relative_to(self.workspace_root):
            raise PolicyError("Sandbox source must be a specific managed worktree")
        if ',' in str(path) or any(c in str(path) for c in '\r\n'):
            raise PolicyError("Worktree path cannot be represented safely in a Docker mount")
        if policy.image not in self.allowed_images:
            raise PolicyError("Image is not approved for this sandbox")
        # Reject links/reparse points and device files in the input tree. The
        # trusted caller must freeze the checkout for the duration of execution.
        import stat
        for base, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                entry = checked_path(Path(base) / name)
                info = entry.lstat()
                mode = info.st_mode
                if stat.S_ISREG(mode) and info.st_nlink != 1:
                    raise PolicyError("Hardlinked input is not admissible")
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise PolicyError("Special files are forbidden in sandbox input")
        return path

    def preflight(self, policy):
        info = json.loads(self.docker.command(['info', '--format', '{{json .}}']))
        if info.get('OSType') != 'linux' or not any(value.startswith('name=seccomp') and 'unconfined' not in value for value in info.get('SecurityOptions', [])):
            raise SandboxUnavailable("A Linux Docker daemon with seccomp is required")
        if info.get('MemoryLimit') is not True or info.get('PidsLimit') is not True:
            raise SandboxUnavailable("Docker daemon does not advertise required resource limits")
        image = json.loads(self.docker.command(['image','inspect',policy.image]))[0]
        if image.get('Id') != policy.image or image.get('Os') != 'linux' or image.get('Config', {}).get('Volumes'):
            raise SandboxUnavailable("Image identity, platform or implicit volumes are not admitted")

    def create_arguments(self, snapshot, policy, name, labels):
        args = ['create', '--name', name, '--label', 'adaptive-team.run=' + name,
            '--init', '--pull=never', '--network=none', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges=true', '--security-opt=seccomp=builtin', '--user', policy.user,
            '--memory', str(policy.memory_mb)+'m', '--memory-swap', str(policy.memory_mb)+'m',
            '--cpus', str(policy.cpus), '--pids-limit', str(policy.pids), '--ipc=none',
            '--ulimit', 'nofile=256:256', '--ulimit', 'core=0:0', '--log-driver=none', '--no-healthcheck', '--restart=no',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size='+str(policy.tmp_mb)+'m,mode=1777',
            '--mount', 'type=volume,source='+snapshot.volume+',target=/workspace,readonly,volume-nocopy',
            '--workdir', '/workspace', '--env', 'HOME=/tmp', '--env', 'TMPDIR=/tmp',
            '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1',
            '--entrypoint', policy.command[0]]
        for key, value in labels.items():
            args += ['--label', key+'='+value]
        return args + [policy.image, *policy.command[1:]]

    def _inspect(self, name):
        value = json.loads(self.docker.command(['inspect', name]))
        if not isinstance(value, list) or len(value) != 1 or value[0].get('Config', {}).get('Labels', {}).get('adaptive-team.run') != name:
            raise TerminationUnknown("Container ownership cannot be verified")
        from .watchdog import TOKEN, LABEL
        row = self.watchdog.get(name)
        item = value[0]
        labels = item.get('Config',{}).get('Labels',{})
        if (not row or labels.get(TOKEN) != row['token']
                or labels.get(LABEL) != self.watchdog.namespace):
            raise TerminationUnknown('Exact container ownership cannot be verified')
        # A lost create acknowledgement leaves the durable registry unbound.
        # Recover only through its unguessable token and namespace, then use
        # the immutable full ID for every destructive operation.
        if row['container_id'] is None:
            self.watchdog.bind(name, item.get('Id'))
            row = self.watchdog.get(name)
        if item.get('Id') != row['container_id']:
            raise TerminationUnknown('Exact container ownership cannot be verified')
        return item

    def _stop(self, name):
        value = self._inspect(name)
        if value.get('State', {}).get('Running') is True:
            if value.get('State',{}).get('Paused'): self.docker.command(['unpause',value['Id']])
            self.docker.command(['kill', value['Id']])
            value = self._inspect(name)
        state = value.get('State', {})
        if state.get('Running') is not False or state.get('Status') not in {'created','exited','dead'}:
            raise TerminationUnknown("Container termination is not confirmed")
        self.watchdog.stopped(name, value['Id'], state)
        return state

    def _exists(self, name):
        names = self.docker.command(['ps','--all','--filter','name=^/'+name+'$', '--format','{{.Names}}']).splitlines()
        if names not in ([], [name]):
            raise TerminationUnknown("Unexpected Docker identity response")
        return bool(names)

    def _cleanup(self, name):
        key = 'sandbox:' + name
        record = self.journal.get(key)
        known_stopped = bool((record.get('checkpoint') or {}).get('stopped'))
        if not known_stopped or self._exists(name):
            stopped = self._stop(name)
            # Persist proof BEFORE destructive removal. Missing after this point
            # is recoverable; missing before verified stop is not proof.
            self.journal.checkpoint(key, {'stopped': True, 'state': stopped})
        if self._exists(name):
            self.docker.command(['rm', self._inspect(name)['Id']])
        if self._exists(name):
            raise TerminationUnknown("Container removal was not confirmed")

    def _verify_created(self, name, policy, snapshot):
        value = self._inspect(name)
        host, config = value.get('HostConfig', {}), value.get('Config', {})
        mounts = value.get('Mounts', [])
        bound = [m for m in mounts if m.get('Destination') == '/workspace']
        if (len(bound) != 1 or bound[0].get('Type') != 'volume' or bound[0].get('Name') != snapshot.volume
                or bound[0].get('RW') is not False or any(m.get('Type') == 'bind' for m in mounts)):
            raise SandboxUnavailable('Candidate does not mount the measured snapshot read-only')
        options = set(host.get('SecurityOpt', []))
        if (host.get('Privileged') is not False or host.get('ReadonlyRootfs') is not True
                or host.get('NetworkMode') != 'none' or 'seccomp=builtin' not in options
                or not options.intersection({'no-new-privileges', 'no-new-privileges=true'})
                or config.get('User') != policy.user or value.get('Image') != policy.image
                or host.get('Memory') != policy.memory_mb*1024*1024 or host.get('PidsLimit') != policy.pids
                or host.get('NanoCpus') != policy.cpus*1_000_000_000):
            raise SandboxUnavailable("Created container does not match the approved isolation policy")

    from ..observability.otel import instrument
    @instrument('sandbox.run', lambda self,workspace,policy,**kw: dict(trace_id=kw['trace_id'],
        attributes={'subject_digest':kw['subject_digest'],'input_digest':kw['input_digest']}))
    def run(self, workspace, policy: SandboxPolicy, *, subject_digest: str, input_digest: str, trace_id: str) -> SandboxEvidence:
        if not isinstance(subject_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', subject_digest):
            raise PolicyError("Sandbox verification requires an exact candidate digest")
        if not isinstance(input_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', input_digest):
            raise PolicyError('Trusted expected input manifest digest is required')
        from .watchdog import WatchdogRegistry
        from .input_snapshot import DockerSnapshotter
        if self.watchdog is None: self.watchdog = WatchdogRegistry.from_environment()
        context = TraceContext.task(trace_id)
        path = self.validate(workspace, policy)
        self.preflight(policy)
        self.watchdog.require_ready(self.docker.command(['info','--format','{{.ID}}']).strip())
        snapshotter = self.snapshotter or DockerSnapshotter(self.docker, self.watchdog, policy.image)
        snapshot = snapshotter.create(path, input_digest, ttl=policy.timeout_seconds+120)
        name = 'ai-team-' + uuid.uuid4().hex
        key = 'sandbox:' + name
        self.journal.begin(key, {'name': name, 'subject_digest': subject_digest,
            'policy_digest': digest(asdict(policy)), 'trace_id': trace_id, 'input_digest': input_digest,
            'snapshot': asdict(snapshot), 'lifecycle_version': 2})
        snapshot_removed = False
        create_attempted = False
        try:
            self.journal.checkpoint(key, {'not_started': True, 'stopped': True})
            if self.traces: self.traces.emit(context, 'sandbox.start', {'run_id': name, 'subject': subject_digest})
            labels = self.watchdog.arm(name, policy.timeout_seconds+30)
            args = self.create_arguments(snapshot, policy, name, labels)
            # Write intent before calling Docker: a crash/timeout after this
            # point is ambiguous and must never be classified as "no start".
            self.journal.checkpoint(key, {'create_attempted': True})
            create_attempted = True
            cid = self.docker.command(args).strip()
            self.watchdog.bind(name, cid)
            self.journal.checkpoint(key, {'created': True})
            self._verify_created(name, policy, snapshot)
            snapshotter.verify(snapshot)
            self.watchdog.require_ready()
            row = self.watchdog.get(name)
            if row['status'] != 'armed' or time.time() >= row['deadline']:
                raise SandboxUnavailable('Watchdog lease expired before execution')
            console, limited, expired = self.docker.stream(['start','--attach',cid], timeout=policy.timeout_seconds, limit=policy.output_bytes)
            stopped = self._stop(name)
            if expired or limited:
                console = ConsoleResult(console.stdout, console.stderr, 124 if expired else 125)
            else:
                code = stopped.get('ExitCode')
                if type(code) is not int or stopped.get('Status') != 'exited' or stopped.get('OOMKilled') or (console.exit_code != 0 and code == 0):
                    console = ConsoleResult(console.stdout, console.stderr, 125)
                else:
                    console = ConsoleResult(console.stdout, console.stderr, code)
            evidence = SandboxEvidence(name, subject_digest, digest(asdict(policy)), True, expired, limited, console, snapshot.digest)
            # Confirm termination BEFORE removal. Docker failure leaves a pending
            # operation; no stdout/exit code is presented as an accepted test run.
            self.journal.checkpoint(key, {'stopped': True, 'state': stopped})
            self._cleanup(name)
            snapshotter.cleanup(snapshot)
            snapshot_removed = True
            self.journal.finish(key, {k:v for k,v in asdict(evidence).items() if k != 'console'})
            if self.traces: self.traces.emit(context, 'sandbox.complete', {'run_id': name, 'exit_code': console.exit_code,
                'timed_out': expired, 'output_limited': limited})
            return evidence
        except BaseException as exc:
            try:
                if create_attempted:
                    self._cleanup(name)
                else:
                    # No Docker create call was made. Do not inspect a worker
                    # that cannot exist; retain this proof across cleanup retry.
                    self.journal.checkpoint(key, {'not_started': True, 'stopped': True})
                if not snapshot_removed: snapshotter.cleanup(snapshot)
                if not create_attempted:
                    self.journal.finish(key, {'run_id': name, 'stopped': True,
                        'removed': True, 'recovered_without_test_result': True})
            except Exception:
                raise TerminationUnknown("Inspect pending sandbox operation; retain its lease and resources") from None
            raise

    def reconcile(self, run_id: str):
        """Owner recovery after controller crash; never execute the candidate again."""
        if not re.fullmatch(r'ai-team-[0-9a-f]{32}', run_id):
            raise PolicyError("Invalid sandbox identity")
        record = self.journal.get('sandbox:' + run_id)
        if not record:
            raise PolicyError("No sandbox operation to reconcile")
        if record['status'] == 'complete': return record['result']
        from .watchdog import WatchdogRegistry
        from .input_snapshot import DockerSnapshotter, InputSnapshot
        if self.watchdog is None: self.watchdog = WatchdogRegistry.from_environment()
        checkpoint = record.get('checkpoint')
        # Version 2 ALWAYS persists create intent before Docker create. Thus a
        # crash directly after begin (no checkpoint yet) also proves no create.
        # Never infer this for older/unknown lifecycle protocols.
        no_create = bool((checkpoint or {}).get('not_started')) or (
            checkpoint is None and record['request'].get('lifecycle_version') == 2)
        if not no_create:
            self._cleanup(run_id)
        snapshot = InputSnapshot(**record['request']['snapshot'])
        snapshotter = self.snapshotter or DockerSnapshotter(self.docker,self.watchdog,None)
        snapshotter.cleanup(snapshot)
        return self.journal.finish('sandbox:' + run_id, {'run_id': run_id, 'stopped': True, 'removed': True, 'recovered_without_test_result': True})
