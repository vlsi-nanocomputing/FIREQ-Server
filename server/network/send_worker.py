"""Handles the outgoing queue, sends serialized responses to the client socket, owns the sock.send_all() method.

The only rule on the queue objects is that they must have a "to_buffers" method that returns a sequence of
bytes-like objects ready for sendmsg(). The protocol is defined in the to_buffers method of the object.
"""

import json
import logging
import socket
import struct
import time
from collections.abc import Iterator
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread

import numpy as np

from system import FIREQSystemNode

from ..utils import ClientDisconnectedError, IncompleteTransferError
from .memory_queue import MemoryBoundedQueue


class SendWorker:
    """Worker thread that handles outgoing messages to the client socket.

    Waits for the client connected event to be set, which is done by the main thread.
    Then, it will start popping items from the queue_out queue and sending them to the client socket.
    """

    def init(self, queue_out: MemoryBoundedQueue, client_connected: Event, logger: logging.Logger = None) -> None:
        """Initialize the send worker.

        :param queue_in: Queue to put incoming messages in.
        :type queue_in: Queue
        :param client_connected: Event that is set when a client is connected.
        :type client_connected: Event
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        """
        self._client_socket = None
        self._client_connected = client_connected
        self._queue_out = queue_out
        self.log = logger or logging.getLogger(__name__)
        # thread safe events that determine if the thread is running and if the thread should stop
        self._stop_event = Event()
        self._thread_running = Event()

    def start(self) -> None:
        """Start the send worker thread."""
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the send worker thread.

        The thread will be joined with a timeout of 2 seconds.
        """
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            self.log.warning("Thread did not exit cleanly")

    def _run(self):
        self._thread_running.set()
        while not self._stop_event.is_set():
            if not self._client_connected.wait(0.5):
                continue
            try:
                # pop a message from the queue
                msg = self._receive_message()
                # parse it
                parsed = self._parse_message(msg)
                # parse the payload and place it in queue
                self._queue_in.put(parsed)
            except InvalidPayloadError as e:
                self.log.warning(f"Invalid payload received: {e}")
                continue
            except ClientDisconnectedError as e:
                self.log.info(f"Connection lost: {e}")
                self._client_connected.clear()
                continue
            except IncompleteTransferError as e:
                # this only happens when the worker is stopped
                self.log.warning(f"Incomplete transfer received: {e}")
                continue
            except Exception as e:
                self.log.error(f"Unexpected error in receive worker: {e}")
                break
        # exit the thread safely
        self.log.info("Detected stop flag or unexpected error, exiting...")
        self._cleanup_and_exit()

    def _receive_message(self) -> bytes:
        """Receive a message from the client socket.

        Internally calls the socket recv method, which is blocking.
        The message is expected to be a 4-byte length prefix followed by a payload.

        :return: The payload bytes.
        :rtype: bytes
        """
        # first, receive the length of the message
        try:
            length_bytes = self._recv_exact(4)
            length = int.from_bytes(length_bytes, "big")

        except Exception:
            raise

        try:
            # now, receive the payload
            payload = self._recv_exact(length)
        except Exception:
            raise

        return payload

    def _recv_exact(self, n: int) -> bytes:
        """Receive exactly n bytes from the socket.

        Raises ClientDisconnectedError if the connection is lost.
        Propagates socket.timeout if a read timeout expires (you can retry).

        :param n: Number of bytes to receive.
        :type n: int
        :return: Received bytes.
        :rtype: bytes
        """
        data = b""
        while len(data) < n and not self._stop_event.is_set():
            try:
                chunk = self._client_socket.recv(n - len(data))
            except TimeoutError:
                continue
            except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
                raise ClientDisconnectedError(f"Connection lost: {e}") from e

            # check if client closed the connection
            if not chunk:
                raise ClientDisconnectedError("Peer closed the connection")

            data += chunk
        if len(data) != n:
            raise IncompleteTransferError(f"Expected {n} bytes, got {len(data)}")
        return data

    def _parse_message(self, payload: bytes) -> dict:
        """Parse a payload message from the client.

        Always returns a dict, which is the payload bytes parsed as a message pack.

        :param payload: Raw payload bytes.
        :type payload: bytes
        :return: Parsed message dictionary.
        :rtype: dict
        """
