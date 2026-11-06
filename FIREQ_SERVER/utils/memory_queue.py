"""Memory-bounded thread-safe queue for streaming data.

This module provides a queue that bounds by memory usage instead of item count,
designed for streaming FPGA data where chunks vary greatly in size.
"""

import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from queue import Empty, Full

import numpy as np

# how often a put() waiting for space re-checks the discard condition, in seconds
_DISCARD_POLL_SECONDS = 0.5


class MemoryBoundedQueue:
    """Thread-safe queue that bounds by memory usage, not item count.

    Designed for streaming FPGA data where chunks vary greatly in size.
    Blocks on put() when memory limit is reached (backpressure).

    The queue preserves FIFO order - items come out in the same order they went in.
    Server NEVER reorders; it includes chunk_index in metadata for client-side reassembly.

    Items are tagged with the session they were put in (see :meth:`new_session`), so the consumer can
    tell the items of a previous client from those of the current one.
    """

    def __init__(
        self, max_memory_bytes: int = 1024 * 1024 * 1024, discard_when: Callable[[], bool] | None = None
    ) -> None:
        """Initialize queue with memory limit.

        :param max_memory_bytes: Maximum total memory in bytes (default: 1 GB).
        :param discard_when: Optional callable; while it returns True, put() drops its item instead of
            enqueuing it or waiting for space. Used to drop packets that cannot be delivered anymore.
        """
        self._queue: deque[tuple[object, int, int]] = deque()  # (item, size_bytes, session)
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._not_full = threading.Condition(self._lock)
        self._max_bytes = max_memory_bytes
        self._current_bytes = 0
        self._closed = False
        self._discard_when = discard_when or (lambda: False)
        self._session = 0
        # total time put() spent waiting for space, only measured when it actually waits
        self._blocked_ns = 0

    def put(self, item: object, timeout: float | None = None) -> bool:
        """Put item into queue, blocking if memory limit reached.

        Items are added to the end of the queue (FIFO order preserved).
        An item larger than the memory limit is accepted only when the queue is empty.
        The item is dropped if the queue is closed or the discard condition is true, also while waiting
        for space (checked every _DISCARD_POLL_SECONDS).

        :param item: Item to enqueue.
        :param timeout: Max seconds to wait (None = wait forever).
        :return: True if the item was enqueued, False if it was dropped.
        :raises Full: If timeout expires before space available.
        """
        item_bytes = self._estimate_size(item)
        end_time = None if timeout is None else time.monotonic() + timeout

        with self._not_full:
            if self._queue and self._current_bytes + item_bytes > self._max_bytes:
                wait_start = time.perf_counter_ns()
                try:
                    while self._queue and self._current_bytes + item_bytes > self._max_bytes:
                        if self._closed or self._discard_when():
                            return False
                        wait = _DISCARD_POLL_SECONDS
                        if end_time is not None:
                            remaining = end_time - time.monotonic()
                            if remaining <= 0:
                                raise Full("Memory limit reached")
                            wait = min(wait, remaining)
                        self._not_full.wait(timeout=wait)
                finally:
                    self._blocked_ns += time.perf_counter_ns() - wait_start

            if self._closed or self._discard_when():
                return False

            self._queue.append((item, item_bytes, self._session))
            self._current_bytes += item_bytes
            self._not_empty.notify()
            return True

    def get(self, timeout: float | None = None) -> object:
        """Get item from queue, blocking if empty.

        Items are removed from the front of the queue (FIFO order preserved).

        :param timeout: Max seconds to wait (None = wait forever).
        :return: Dequeued item.
        :raises Empty: If timeout expires before item available.
        """
        return self.get_with_session(timeout)[0]

    def get_with_session(self, timeout: float | None = None) -> tuple[object, int]:
        """Get item from queue, blocking if empty, together with the session it was put in.

        :param timeout: Max seconds to wait (None = wait forever).
        :return: Dequeued item and its session.
        :raises Empty: If timeout expires before item available.
        """
        with self._not_empty:
            if timeout is None:
                while not self._queue and not self._closed:
                    self._not_empty.wait()
            else:
                end_time = time.monotonic() + timeout
                while not self._queue and not self._closed:
                    remaining = end_time - time.monotonic()
                    if remaining <= 0:
                        raise Empty()
                    self._not_empty.wait(timeout=remaining)

            if not self._queue:
                raise Empty()

            item, item_bytes, session = self._queue.popleft()
            self._current_bytes -= item_bytes
            self._not_full.notify()
            return item, session

    def clear(self) -> None:
        """Clear all items from queue."""
        with self._lock:
            self._queue.clear()
            self._current_bytes = 0
            self._not_full.notify_all()

    def usage(self) -> tuple[int, int, int, int]:
        """Return a snapshot of the queue occupation, meant for diagnostics.

        :return: Number of items, their estimated size in bytes, the memory limit in bytes, and the total
            time put() has spent waiting for space since the queue was created, in ns.
        """
        with self._lock:
            return len(self._queue), self._current_bytes, self._max_bytes, self._blocked_ns

    def new_session(self) -> int:
        """Clear the queue and start a new session: the items put from now on are tagged with it.

        :return: The new session.
        """
        with self._lock:
            self._queue.clear()
            self._current_bytes = 0
            self._session += 1
            self._not_full.notify_all()
            return self._session

    def close(self) -> None:
        """Signal queue to stop blocking on put/get."""
        with self._lock:
            self._closed = True
            self._not_full.notify_all()
            self._not_empty.notify_all()

    def _estimate_size(self, item: object) -> int:
        """Estimate memory footprint of an item.

        Recursively calculates size for nested structures.
        For numpy arrays, uses nbytes which is the actual data size.
        Packet objects (FIREQNetworkPacket, DMAPayload) are sized by their "header" and "data" attributes.
        """
        if isinstance(item, dict):
            total = sys.getsizeof(item)
            for k, v in item.items():
                total += self._estimate_size(k)
                total += self._estimate_size(v)
            return total
        elif isinstance(item, np.ndarray):
            # nbytes gives the actual buffer size, getsizeof gives the wrapper overhead
            return item.nbytes + sys.getsizeof(item)
        elif isinstance(item, (list, tuple)):
            return sys.getsizeof(item) + sum(self._estimate_size(x) for x in item)
        elif isinstance(item, str):
            return sys.getsizeof(item)
        elif isinstance(item, (bytes, bytearray, memoryview)):
            return len(item) + sys.getsizeof(item)
        elif hasattr(item, "data") or hasattr(item, "header"):
            # network packets: count the payload, otherwise only the object wrapper would be counted
            total = sys.getsizeof(item)
            for attr in ("header", "data"):
                value = getattr(item, attr, None)
                if value is not None:
                    total += self._estimate_size(value)
            return total
        else:
            return sys.getsizeof(item)


__all__ = ["MemoryBoundedQueue"]
