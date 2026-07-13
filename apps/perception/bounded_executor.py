"""Daemon worker pool with an explicit, finite cooperative shutdown boundary."""

from concurrent.futures import Future
import queue
import threading
import time


class DaemonWorkerPool:
    """A minimal Future executor whose workers cannot pin process shutdown.

    Python cannot safely kill a thread blocked inside a native library. Callers
    must still pass cancellation into their work, but the service process is the
    final killable boundary: daemon workers cannot force systemd to SIGKILL an
    otherwise quiesced process.
    """

    _STOP = object()

    def __init__(self, max_workers, thread_name_prefix):
        count = max(1, int(max_workers))
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._shutdown = False
        self._threads = []
        for index in range(count):
            thread = threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def submit(self, function, *args, **kwargs):
        future = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule work after shutdown")
            self._queue.put((future, function, args, kwargs))
        return future

    def _worker(self):
        while True:
            task = self._queue.get()
            if task is self._STOP:
                return
            future, function, args, kwargs = task
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def shutdown(self, *, wait=True, cancel_futures=False, timeout=None):
        """Stop accepting work and return false if workers exceed ``timeout``."""
        with self._lock:
            if not self._shutdown:
                self._shutdown = True
                if cancel_futures:
                    while True:
                        try:
                            task = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        if task is not self._STOP:
                            task[0].cancel()
                for _thread in self._threads:
                    self._queue.put(self._STOP)
        if not wait:
            return all(not thread.is_alive() for thread in self._threads)
        deadline = (
            None
            if timeout is None
            else time.monotonic() + max(0.0, float(timeout))
        )
        for thread in self._threads:
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            thread.join(remaining)
        return all(not thread.is_alive() for thread in self._threads)
