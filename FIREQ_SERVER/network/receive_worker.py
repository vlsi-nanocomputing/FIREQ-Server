"""Handles the incoming packets, owns the recv() calls on the client socket, puts packets in the input queue."""

import logging
import socket
from queue import Empty, Queue
from threading import Event, Thread

from ..utils import ClientDisconnectedError, IncompleteTransferError, InvalidPayloadError
from .protocol import FIREQNetworkPacket, unpack_header


class ReceiveWorker:
    """Worker thread that handles incoming messages from the client socket.

    Waits for the client connected event to be set, which is done by the main thread, before starting to
    pull messages from the socket.

    The socket must be set by the main thread before setting the client connected event.

    Will clear the client connected event if the connection is lost.
    """

    def __init__(
        self, client_connected: Event, logger: logging.Logger = None, max_payload_bytes: int = 10 * 1024 * 1024
    ) -> None:
        """Initialize the receive worker.

        :param client_connected: Event that is set when a client is connected.
        :type client_connected: Event
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        :param max_payload_bytes: Maximum accepted size of a packet header and of its trailing data, in bytes.
        :type max_payload_bytes: int
        """
        self._max_payload_bytes = max_payload_bytes
        # input queue
        self.queue_in = Queue(maxsize=10)
        # public attribute to set the client socket
        self.client_socket = None
        # input mutable objects and events
        self._client_connected = client_connected
        self.log = logger or logging.getLogger(__name__)
        # thread safe events that determine if the thread is running and if the thread should stop
        self._stop_event = Event()
        self._thread_running = Event()

    def start(self) -> None:
        """Start the receive worker thread."""
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the receive worker thread.

        The thread will be joined with a timeout of 2 seconds.
        """
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            self.log.warning("Thread did not exit cleanly")

    def clear_input_queue(self) -> None:
        """Empty the input queue."""
        # this is safe because the method owns the put method
        while True:
            try:
                self.queue_in.get_nowait()
            except Empty:
                break

    def _run(self) -> None:
        """Run loop for the thread.

        Will loop until the stop event is set.
        If a client is connected, which is determined by the client_connected event, it will receive
        messages on the client socket and put them in the queue_in queue.
        """
        self._thread_running.set()
        while not self._stop_event.is_set():
            if not self._client_connected.wait(0.5):
                continue
            # errors on a socket that has already been replaced must not disconnect the new client
            client_socket = self.client_socket
            try:
                # receive a message
                msg = self._receive_message(client_socket)
                # parse the payload and place it in queue
                self.queue_in.put(msg)
            except InvalidPayloadError as e:
                self.log.warning(f"Invalid payload received: {e}")
                continue
            except ClientDisconnectedError as e:
                if client_socket is self.client_socket:
                    self.log.info(f"Connection lost: {e}")
                    self._client_connected.clear()
                continue
            except IncompleteTransferError as e:
                # this only happens when the worker is stopped
                self.log.warning(f"Incomplete transfer received: {e}")
                continue
            except Exception:
                # the stream position is unknown: drop the client but keep the worker alive for the next one
                self.log.exception("Unexpected error in receive worker, dropping the client connection")
                if client_socket is self.client_socket:
                    self._client_connected.clear()
                continue
        # exit the thread safely
        self.log.info("Detected stop flag, exiting...")
        self._cleanup_and_exit()

    def _receive_message(self, client_socket: socket.socket) -> FIREQNetworkPacket:
        """Receive a message from the client socket.

        :param client_socket: Client socket to receive from.
        :type client_socket: socket.socket
        :return: A data packet.
        :rtype: FIREQNetworkPacket
        :raises InvalidPayloadError: if the header is not a valid msgpack dict (the stream stays aligned)
        :raises ClientDisconnectedError: if the connection is lost or a size exceeds the limit
            (the stream cannot be realigned, so the client must be dropped)
        """
        # first, receive the length of the message
        length_bytes = self._recv_exact(client_socket, 4)
        length = int.from_bytes(length_bytes, "big")
        if length > self._max_payload_bytes:
            raise ClientDisconnectedError(f"Header size {length} exceeds the limit of {self._max_payload_bytes} bytes")

        # now, receive the header
        payload = self._recv_exact(client_socket, length)
        try:
            header = unpack_header(payload)
        except Exception as e:
            raise InvalidPayloadError(f"header is not valid msgpack: {e}") from e
        if not isinstance(header, dict):
            raise InvalidPayloadError(f"header must be a dict, got {type(header).__name__}")
        msg = FIREQNetworkPacket(header=header)

        # see if there is data
        dlength = msg.header.get("tsize", 0)
        if not isinstance(dlength, int) or dlength < 0 or dlength > self._max_payload_bytes:
            raise ClientDisconnectedError(f"Invalid trailing data size {dlength!r}")
        if dlength > 0:
            msg.data = self._recv_exact(client_socket, dlength)

        return msg

    def _recv_exact(self, client_socket: socket.socket, n: int) -> bytes:
        """Receive exactly n bytes from the socket.

        Raises ClientDisconnectedError if the connection is lost or the socket has been replaced.
        Read timeouts are retried until the stop event is set.

        :param client_socket: Client socket to receive from.
        :type client_socket: socket.socket
        :param n: Number of bytes to receive.
        :type n: int
        :return: Received bytes.
        :rtype: bytes
        """
        data = b""
        while len(data) < n and not self._stop_event.is_set():
            try:
                chunk = client_socket.recv(n - len(data))
            except TimeoutError:
                if client_socket is not self.client_socket or not self._client_connected.is_set():
                    raise ClientDisconnectedError("Client connection closed by the server") from None
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

    def _cleanup_and_exit(self) -> None:
        """Clean-up and exit the thread safely."""
        self._thread_running.clear()
