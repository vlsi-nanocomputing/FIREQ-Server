"""Handles the outgoing queue, sends serialized responses to the client socket, owns the sock.send_all() method.

The only rule on the queue objects is that they must have a "to_buffers" method that returns a sequence of
bytes-like objects ready for sendmsg(). The protocol is defined in the to_buffers method of the object.
"""

import logging
import socket
from queue import Empty
from threading import Event, Thread

from ..utils import ClientDisconnectedError, MemoryBoundedQueue


class SendWorker:
    """Worker thread that handles outgoing messages to the client socket.

    Waits for the client connected event to be set, which is done by the main thread.
    Then, it will start popping items from the queue_out queue and sending them to the client socket.
    """

    def __init__(
        self, client_connected: Event, logger: logging.Logger = None, max_queue_bytes: int = 1024 * 1024 * 1024
    ) -> None:
        """Initialize the send worker.

        :param client_connected: Event that is set when a client is connected.
        :type client_connected: Event
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        :param max_queue_bytes: Memory limit of the output queue, in bytes.
        :type max_queue_bytes: int
        """
        # output queue
        self.queue_out = MemoryBoundedQueue(max_memory_bytes=max_queue_bytes)
        # public attribute to set the client socket
        self.client_socket: socket.socket | None = None
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

    def clear_output_queue(self) -> None:
        """Empty the output queue."""
        # this is safe because the method owns the put method
        self.queue_out.clear()

    def _run(self) -> None:
        self._thread_running.set()
        while not self._stop_event.is_set():
            if not self._client_connected.wait(0.5):
                continue
            # errors on a socket that has already been replaced must not disconnect the new client
            sock = self.client_socket
            try:
                # pop an item from the queue
                item = self.queue_out.get(timeout=0.5)
                # send it over the socket
                self._send_payload(sock, item)
            except Empty:
                continue
            except ClientDisconnectedError as e:
                if sock is self.client_socket:
                    self.log.info(f"Connection lost: {e}")
                    self._client_connected.clear()
                continue
            except Exception as e:
                self.log.error(f"Unexpected error in send worker: {e}")
                break
        # exit the thread safely
        self.log.info("Detected stop flag or unexpected error, exiting...")
        self._cleanup_and_exit()

    def _send_payload(self, sock: socket.socket, item: object) -> None:
        """Send a queue item over the network.

        The input item should implement the to_buffers() method,
        the result of which is directly sent over the network without
        any other checks.

        :param sock: Client socket to send on.
        :type sock: socket.socket
        :param item: Data object implementing the to_buffers() method.
        :type item: object
        """
        self._sendmsg_all(sock, item.to_buffers())

    def _sendmsg_all(self, sock: socket.socket, buffers: tuple) -> None:
        """Send all the buffers over the socket, handling partial sends.

        sendmsg() may send only part of the data (e.g. when the socket has a timeout), so the
        buffers are advanced by the number of bytes actually sent until everything is sent.
        A timeout means that nothing was sent, so the remaining data is retried.

        :param sock: Client socket to send on.
        :type sock: socket.socket
        :param buffers: sequence of bytes-like objects
        :type buffers: tuple
        :raises ClientDisconnectedError: if the connection is lost
        """
        pending = [memoryview(buf).cast("B") for buf in buffers if len(buf) > 0]
        while pending and not self._stop_event.is_set():
            try:
                sent = sock.sendmsg(pending)
            except TimeoutError:
                # might change it later to add a maximum number of timeouts
                if sock is not self.client_socket or not self._client_connected.is_set():
                    raise ClientDisconnectedError("Client connection closed by the server") from None
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
            # drop the fully sent buffers and advance the partially sent one
            while pending and sent >= len(pending[0]):
                sent -= len(pending[0])
                pending.pop(0)
            if sent:
                pending[0] = pending[0][sent:]

    def _cleanup_and_exit(self) -> None:
        """Clean-up and exit the thread safely."""
        self._thread_running.clear()
