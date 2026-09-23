"""Bounded Git subprocesses and host-local serialization.

The repository configuration and control directories belong to the trusted host.
This is not a defense against another process with the same filesystem authority.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..models import PolicyError


class GitError(RuntimeError):
    def __init__(self, operation: str, returncode: int, stderr: bytes):
        self.operation, self.returncode = operation, returncode
        # Do not retain an unbounded diff, environment, or full command in errors.
        self.detail = stderr[-4096:].decode("utf-8", "replace")
        super().__init__(f"Git {operation} failed ({returncode}): {self.detail}")


def checked_path(path: str | Path) -> Path:
    """Reject symlinks/reparse points before resolving, including parent links."""
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise PolicyError("Linked/reparse paths are not admitted to the Git control host")
    return path


class Git:
    def __init__(self, control: Path, *, timeout: int = 30, output_limit: int = 8 * 1024 * 1024):
        self.executable = shutil.which("git")
        if not self.executable:
            raise RuntimeError("Git is required")
        self.control = checked_path(control)
        self.control.mkdir(parents=True, exist_ok=True)
        self.hooks = self.control / "empty-hooks"
        self.hooks.mkdir(exist_ok=True)
        if any(self.hooks.iterdir()):
            raise PolicyError("Trusted hooks override directory must be empty")
        self.timeout, self.output_limit = timeout, output_limit
        # No inherited GIT_CONFIG_COUNT, GIT_DIR, credentials, editors or provider
        # secrets. System PATH is used only to resolve the Git executable above.
        keep = {"systemroot", "windir", "path", "pathext", "temp", "tmp", "home", "userprofile"}
        self.env = {key: value for key, value in os.environ.items() if key.lower() in keep}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1", LC_ALL="C",
                        GIT_AUTHOR_NAME="Adaptive Team Coordinator",
                        GIT_AUTHOR_EMAIL="coordinator@adaptive-team.invalid",
                        GIT_COMMITTER_NAME="Adaptive Team Coordinator",
                        GIT_COMMITTER_EMAIL="coordinator@adaptive-team.invalid")

    def run(self, cwd: Path, *args: str, input: bytes | None = None,
            allowed: tuple[int, ...] = (0,)) -> bytes:
        settings = ["core.hooksPath=" + str(self.hooks), "core.fsmonitor=false",
                    "core.autocrlf=false", "core.attributesFile=" + os.devnull,
                    "commit.gpgSign=false", "tag.gpgSign=false", "gc.auto=0",
                    "maintenance.auto=false", "submodule.recurse=false",
                    "protocol.allow=never", "rerere.enabled=false", "merge.conflictStyle=diff3"]
        command = [self.executable, "--no-pager"]
        for setting in settings:
            command.extend(("-c", setting))
        command.extend(args)
        # Spool output rather than accumulating unlimited data in RAM. Repository
        # size and disk quota are still deployment responsibilities.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            result = subprocess.run(command, cwd=cwd, env=self.env, input=input,
                                    stdout=out, stderr=err, timeout=self.timeout, shell=False)
            out.seek(0); err.seek(0)
            output, errors = out.read(self.output_limit + 1), err.read(self.output_limit + 1)
        if len(output) > self.output_limit or len(errors) > self.output_limit:
            raise GitError(args[0], result.returncode, b"Git output exceeded the configured bound")
        if result.returncode not in allowed:
            raise GitError(args[0], result.returncode, errors)
        return output

    def safe_config(self, repository: Path) -> None:
        # `-c` disables hooks/fsmonitor; filters and custom merge/diff drivers can
        # execute arbitrary commands during otherwise ordinary Git operations.
        dangerous = self.run(repository, "config", "--includes", "--name-only",
                             "--get-regexp", r"^(filter\.|merge\..*\.driver$|diff\..*\.(command|textconv)$)",
                             allowed=(0, 1))
        if dangerous.strip():
            raise PolicyError("External Git filters/drivers require a separately isolated adapter")

    @contextmanager
    def lock(self, *, operation: str | None = None):
        # A process crash releases SQLite's OS lock. No stale-lock deletion or
        # PID guessing is necessary. All managers for one repo MUST share control.
        from ..models import digest
        name = "git-lock.sqlite" if operation is None else "operation-" + digest(operation) + ".sqlite"
        db = sqlite3.connect(checked_path(self.control / name), timeout=30, isolation_level=None)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS mutex (id INTEGER PRIMARY KEY)")
            db.execute("BEGIN IMMEDIATE")
            yield
            db.commit()
        finally:
            db.close()
