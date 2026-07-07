"""Handles the outgoing queue, sends serialized responses to the client socket, owns the sock.send_all() method.

The only rule on the queue objects is that they must have a "to_buffers" method that returns a sequence of
bytes-like objects ready for sendmsg(). The protocol is defined in the to_buffers method of the object.
"""

import logging
import socket
from threading import Event, Thread

from ..utils import ClientDisconnectedError, MemoryBoundedQueue


class SendWorker:
    """Worker thread that handles outgoing messages to the client socket.

    Waits for the client connected event to be set, which is done by the main thread.
    Then, it will start popping items from the queue_out queue and sending them to the client socket.
    """

    def __init__(self, client_connected: Event, logger: logging.Logger = None) -> None:
        """Initialize the send worker.

        :param client_connected: Event that is set when a client is connected.
        :type client_connected: Event
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        """
        # output queue
        self.queue_out = MemoryBoundedQueue()
        # public attribute to set the client socket
        self.client_socket = socket.socket()
        # input mutable objects and events
        self._client_connected = client_connected
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
                # pop an item from the queue
                item = self.queue_out.get()
                # send it over the socket
                self._send_payload(item)
            except ClientDisconnectedError as e:
                self.log.info(f"Connection lost: {e}")
                self._client_connected.clear()
                continue
            except Exception as e:
                self.log.error(f"Unexpected error in send worker: {e}")
                break
        # exit the thread safely
        self.log.info("Detected stop flag or unexpected error, exiting...")
        self._cleanup_and_exit()

    def _send_payload(self, item: object) -> bytes:
        """Send a queue item over the network.

        The input item should implement the to_buffers() method,
        the result of which is directly sent over the network without
        any other checks.

        :param item: Data object implementing the to_buffers() method.
        :type item: object
        :return: Received bytes.
        :rtype: bytes
        """
        while not self._stop_event.is_set():
            try:
                serialized = item.to_buffers()
                self.client_socket.sendmsg(serialized)
                break
            except TimeoutError:
                # might change it later to add a maximum number of timeouts
                continue
            except (ConnectionResetError, ConnectionAbortedError) as e:
                # These indicate the connection is gone – no recovery possible.
                raise ClientDisconnectedError(f"Connection lost: {e}") from e
            except BrokenPipeError as e:
                # catch broken pipe in case of a disconnect during send
                raise ClientDisconnectedError(f"Broken pipe: {e}") from e
            except OSError as e:
                # Catch‑all for other operating‑system errors (e.g., EAGAIN, EWOULDBLOCK
                # in non‑blocking mode).  If they arise, the connection is likely broken.
                raise ClientDisconnectedError(f"Unexpected OS error: {e}") from e

    def _cleanup_and_exit(self) -> None:
        """Clean-up and exit the thread safely."""
        self._thread_running.clear()
