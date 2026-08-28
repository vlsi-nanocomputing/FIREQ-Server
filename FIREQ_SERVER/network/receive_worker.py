"""Handles the incoming packets, owns the client sock.recv() method, puts packets in the input queue."""

import logging
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

    def __init__(self, client_connected: Event, logger: logging.Logger = None) -> None:
        """Initialize the receive worker.

        :param client_connected: Event that is set when a client is connected.
        :type client_connected: Event
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        """
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
            try:
                # receive a message
                msg = self._receive_message()
                # parse the payload and place it in queue
                self.queue_in.put(msg)
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

    def _receive_message(self) -> FIREQNetworkPacket:
        """Receive a message from the client socket.

        :return: A data packet.
        :rtype: FIREQNetworkPacket
        """
        # first, receive the length of the message
        length_bytes = self._recv_exact(4)
        length = int.from_bytes(length_bytes, "big")

        # now, receive the header
        payload = self._recv_exact(length)
        msg = FIREQNetworkPacket(header=unpack_header(payload))

        # see if there is data
        dlength = msg.header.get("tsize", 0)
        if dlength > 0:
            msg.data = self._recv_exact(dlength)

        return msg

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
                chunk = self.client_socket.recv(n - len(data))
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

    def _cleanup_and_exit(self) -> None:
        """Clean-up and exit the thread safely."""
        self._thread_running.clear()
