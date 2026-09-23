"""Bounded optional-telemetry worker; never execute exporters on an event loop.

Jobs capture context at submission. Business results never depend on delivery.
Durable journal jobs remain in their SQL outbox if the queue is full or a callback
fails. Bare-client events are best effort. flush() is for owner shutdown/tests.
"""
import contextvars
import queue
import threading
import time


class BackgroundWorker:
    def __init__(self, capacity=256):
        self.queue = queue.Queue(maxsize=capacity)
        self.failures = 0
        self.thread = threading.Thread(target=self._run, name='ai-team-telemetry', daemon=True)
        self.thread.start()

    def submit(self, callback, *args):
        try:
            self.queue.put_nowait((contextvars.copy_context(), callback, args))
            return True
        except Exception:
            self.failures += 1
            return False

    def _run(self):
        while True:
            context, callback, args = self.queue.get()
            try:
                context.run(callback, *args)
            except Exception:
                self.failures += 1
            finally:
                self.queue.task_done()

    def flush(self, timeout=2):
        deadline = time.monotonic() + timeout
        with self.queue.all_tasks_done:
            while self.queue.unfinished_tasks:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    return False
                self.queue.all_tasks_done.wait(remaining)
        return True


_worker = None
_lock = threading.Lock()


def submit(callback, *args):
    global _worker
    try:
        with _lock:
            if _worker is None:
                _worker = BackgroundWorker()
            worker = _worker
        return worker.submit(callback, *args)
    except Exception:
        # Thread/resource exhaustion is an optional telemetry outage too.
        return False


def flush(timeout=2):
    return _worker is None or _worker.flush(timeout)
