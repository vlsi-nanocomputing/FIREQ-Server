# file: fireq-utils/server/network/memory_queue.py
"""Memory-bounded thread-safe queue for streaming data.

This module provides a queue that bounds by memory usage instead of item count,
designed for streaming FPGA data where chunks vary greatly in size.
"""

import sys
import threading
import time
from queue import Empty, Full

import numpy as np


class MemoryBoundedQueue:
    """Thread-safe queue that bounds by memory usage, not item count.

    Designed for streaming FPGA data where chunks vary greatly in size.
    Blocks on put() when memory limit is reached (backpressure).

    The queue preserves FIFO order - items come out in the same order they went in.
    Server NEVER reorders; it includes chunk_index in metadata for client-side reassembly.
    """

    def __init__(self, max_memory_bytes: int = 1024 * 1024 * 1024) -> None:
        """Initialize queue with memory limit.

        :param max_memory_bytes: Maximum total memory in bytes (default: 1 GB).
        """
        self._queue: list[tuple[object, int]] = []  # (item, size_bytes)
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._not_full = threading.Condition(self._lock)
        self._max_bytes = max_memory_bytes
        self._current_bytes = 0
        self._closed = False

    @property
    def current_memory_mb(self) -> float:
        """Current memory usage in MB."""
        with self._lock:
            return self._current_bytes / (1024 * 1024)

    @property
    def max_memory_mb(self) -> float:
        """Maximum memory limit in MB."""
        return self._max_bytes / (1024 * 1024)

    def put(self, item: object, timeout: float | None = None) -> None:
        """Put item into queue, blocking if memory limit reached.

        Items are added to the end of the queue (FIFO order preserved).

        :param item: Item to enqueue.
        :param timeout: Max seconds to wait (None = wait forever).
        :raises Full: If timeout expires before space available.
        """
        item_bytes = self._estimate_size(item)

        with self._not_full:
            if timeout is None:
                while self._current_bytes + item_bytes > self._max_bytes and not self._closed:
                    self._not_full.wait()
            else:
                end_time = time.monotonic() + timeout
                while self._current_bytes + item_bytes > self._max_bytes and not self._closed:
                    remaining = end_time - time.monotonic()
                    if remaining <= 0:
                        raise Full("Memory limit reached")
                    self._not_full.wait(timeout=remaining)

            if self._closed:
                return

            self._queue.append((item, item_bytes))
            self._current_bytes += item_bytes
            self._not_empty.notify()

    def get(self, timeout: float | None = None) -> object:
        """Get item from queue, blocking if empty.

        Items are removed from the front of the queue (FIFO order preserved).

        :param timeout: Max seconds to wait (None = wait forever).
        :return: Dequeued item.
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

            item, item_bytes = self._queue.pop(0)
            self._current_bytes -= item_bytes
            self._not_full.notify()
            return item

    def clear(self) -> None:
        """Clear all items from queue."""
        with self._lock:
            self._queue.clear()
            self._current_bytes = 0
            self._not_full.notify_all()

    def close(self) -> None:
        """Signal queue to stop blocking on put/get."""
        with self._lock:
            self._closed = True
            self._not_full.notify_all()
            self._not_empty.notify_all()

    def qsize(self) -> int:
        """Return number of items in queue."""
        with self._lock:
            return len(self._queue)

    def empty(self) -> bool:
        """Return True if queue is empty."""
        with self._lock:
            return len(self._queue) == 0

    def _estimate_size(self, item: object) -> int:
        """Estimate memory footprint of an item.

        Recursively calculates size for nested structures.
        For numpy arrays, uses nbytes which is the actual data size.
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
        elif isinstance(item, bytes):
            return len(item) + sys.getsizeof(item)
        else:
            return sys.getsizeof(item)


__all__ = ["MemoryBoundedQueue"]
