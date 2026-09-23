"""Host-local, process-crash-released mutex for cooperating filesystem writers.

The lock is independent of SQLite. Never unlink its inode: waiters must continue
to contend on the same file. The containing directory is owner-controlled.
"""
from contextlib import contextmanager
import errno
import os
import time
from ..code_integration._git import checked_path


@contextmanager
def file_lock(path, *, timeout=60):
    path = checked_path(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            def acquire():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            def release():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(fd, fcntl.LOCK_UN)
        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire(); acquired = True; break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError('Filesystem projection writer is busy') from None
                time.sleep(.01)
        yield
    finally:
        if acquired:
            release()
        os.close(fd)
