"""FIREQ TCP Server - single-client TCP server for hardware experiments.

Handles client connections at startup, owns the client socket and handles the connection lifetime
Handles the execution of commands received from the client

Architecture: 3 threads communicate via queue_in/queue_out.
- Main thread: executes commands, interfaces hardware (this module)
- Receiver thread: accepts connections, parses messages
- Sender thread: writes responses to client

Protocol (see network/protocol.py):
- Every packet: 4-byte big-endian header length + msgpack header dict + optional trailing data
  (present when the header has a "tsize" key).
- config_and_run: experiment_header -> (dma_package(s) + iteration_ended) per iteration -> experiment_footer.
"""

import logging
import socket
import time
from pathlib import Path
from queue import Empty
from threading import Event

from FIREQ_SYSTEM import FIREQSystemNode

from .execution import SweepExperiment
from .network import FIREQNetworkPacket, NetworkDMAPayload, ReceiveWorker, SendWorker, get_command, get_sweep_variables
from .utils import SweepAbortedError

MAX_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB max incoming header/data size
QUEUE_MAX_MEMORY_BYTES = 1024 * 1024 * 1024  # 1 GB queue memory limit
HANDSHAKE_TIMEOUT_SECONDS = 3.0
SOCKET_TIMEOUT_SECONDS = 1.0  # client socket timeout, keeps the workers responsive to stop requests


class FIREQServer:
    """TCP server for FIREQ experiments.

    Spawns three threads:

    - a client handler thread, which communicates with the client to receive
      commands and send info/configuration
    - an execution thread, which manages the hw and runs commands received
      from the client
    - a data streamer, which sends acquisition data back to the client
    """

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def __init__(
        self,
        overlay_file: Path = None,
        host: str = "0.0.0.0",
        port: int = 5000,
        auth_token: str = "fireq",
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize the FIREQ TCP server.

        :param overlay_file: Path to the overlay to load.
        :type overlay_file: Path
        :param host: TCP bind address (use "0.0.0.0" for all interfaces).
        :type host: str
        :param port: TCP port to bind.
        :type port: int
        :param auth_token: Shared token for client authentication.
        :type auth_token: str
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        """
        # load the system
        if overlay_file is None:
            raise ValueError("overlay_file must be provided")
        self._fireq_soc = FIREQSystemNode(overlay_file)
        # set the dma payload interface class to use a class that provides a
        # compatible network interface
        self._fireq_soc.set_dma_payload_interface_class(NetworkDMAPayload)
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self.log = logger or logging.getLogger(__name__)

        # Server state
        self._stop_event = Event()
        self._thread_running = Event()
        self._client_connected = Event()

        # Thread objects for the receive and transmit channel
        self._receive_worker = ReceiveWorker(self._client_connected, max_payload_bytes=MAX_PAYLOAD_BYTES)
        self._send_worker = SendWorker(self._client_connected, max_queue_bytes=QUEUE_MAX_MEMORY_BYTES)

        # Getting the two network packet queues
        self._queue_in = self._receive_worker.queue_in
        self._queue_out = self._send_worker.queue_out

        # sockets
        self._server_socket: socket.socket = None
        self._client_socket: socket.socket = None

    def start(self) -> None:
        """Start the server and block on the main thread.

        Starts the network accept/receiver thread (daemon), then runs the main
        experiment loop in the current thread. Stops when :meth:`stop` is called.
        """
        self._receive_worker.start()
        self._send_worker.start()
        self.log.info(f"FIREQ Server started on {self._host}:{self._port}")
        self._run()

    def stop(self) -> None:
        """Stop the server gracefully.

        Sets running flag to False, triggers stop event to abort sweeps,
        unblocks the main loop, and closes server/client sockets.
        """
        self.log.info("Stopping server...")
        self._stop_event.set()

        for sock in (self._server_socket, self._client_socket):
            if sock:
                try:
                    sock.close()
                except (OSError, Exception):
                    pass

        # stop threads
        self._receive_worker.stop()
        self._send_worker.stop()

    # =========================================================================
    # MAIN THREAD - Command Execution
    # =========================================================================

    def _run(self) -> None:
        """Handle a client connection and execute the commands received from the queue_in."""
        self._thread_running.set()
        self._open_server_socket()
        while not self._stop_event.is_set():
            # accept a client connection, None means that the server is stopping
            client_socket = self._accept_client()
            if client_socket is None:
                break
            self._client_socket = client_socket

            # assign client socket and inform threads
            self._client_socket.settimeout(SOCKET_TIMEOUT_SECONDS)
            self._receive_worker.client_socket = self._client_socket
            self._send_worker.client_socket = self._client_socket
            self._client_connected.set()

            # perform the handshake
            self.log.info("Performing handshake on client...")
            if not self._perform_handshake():
                self.log.warning("A client connection failed to complete the handshake, closing connection")
                self._close_client()
                continue

            # now start the actual main loop
            self._main_loop()

            # the main loop exits on logout, disconnect or stop: always release the client and drop
            # stale packets so they are not delivered to the next client
            self._close_client()

        self.log.info("Execution thread exited, cleaning up")

    def _main_loop(self) -> None:
        """Gather a message from the input queue, parse it and execute commands.

        Will exit on stop_event or client disconnect.
        """
        while not self._stop_event.is_set() and self._client_connected.is_set():
            # receive message from the input queue
            try:
                msg = self._queue_in.get(timeout=1.0)
            except Empty:
                continue

            # process the message
            self._process_message(msg)

        self.log.debug("main loop exited")

    def _process_message(self, msg: dict) -> None:
        """Execute one command message and enqueue a response.

        :param msg: Parsed command dictionary from the client.
        :type msg: dict
        """
        cmd = get_command(msg)
        # session_id = msg.get("session_id", "")

        self.log.debug(f"Processing command: {cmd}")

        try:
            if cmd == "apply_configuration":
                # apply the system configuration dictionary to the fireq soc
                callbacks = self._config_from_message(msg)
                if callbacks:
                    self.log_and_send_warning(
                        "Configuration from apply_configuration has sweepable parameters, they were not applied"
                    )

            elif cmd == "config_and_run":
                self._config_and_run(msg)

            elif cmd == "ping":
                self._queue_out.put(FIREQNetworkPacket({"resp": "pong"}))

            elif cmd == "reset_all":
                self._fireq_soc.reset_all()
                self._queue_out.put(FIREQNetworkPacket({"type": "status", "msg": "successfully reset"}))

            elif cmd == "logout":
                self._close_client()

            elif cmd == "mts_sync":
                self._fireq_soc.synchronize_dac_tiles()
                self._queue_out.put(FIREQNetworkPacket({"type": "status", "msg": "ok"}))

            elif cmd == "set_nyquist":
                self._fireq_soc.set_nyquist_zone(msg.get("tile"), msg.get("block_id"), msg.get("zone"))
                self._queue_out.put(FIREQNetworkPacket({"type": "status", "msg": "ok"}))

            elif cmd == "trigger_manually":
                try:
                    self._fireq_soc.trigger_ip_manually(msg.get("ip_name", ""))
                    self._queue_out.put(FIREQNetworkPacket({"type": "status", "msg": "ok"}))
                except Exception as e:
                    self._queue_out.put(FIREQNetworkPacket({"type": "error", "msg": f"{e}"}))

            else:
                self.log.debug("received a non-supported command from the client")
                self._queue_out.put(FIREQNetworkPacket({"type": "error", "msg": f"unrecognized command '{cmd}'"}))

        except Exception as e:
            self.log.exception("Caught error while running command")
            self._queue_out.put(FIREQNetworkPacket({"type": "error", "msg": f"error while executing '{cmd}': {e}"}))

    # =========================================================================
    # Connection Handling
    # =========================================================================

    def _open_server_socket(self) -> None:
        """Create, bind and listen on the server socket. It is reused for every client connection."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self._host, self._port))
        self._server_socket.listen(1)
        self._server_socket.settimeout(1.0)
        self.log.info(f"Listening on {self._host}:{self._port}")

    def _accept_client(self) -> socket.socket | None:
        """Accept a TCP connection on the server socket.

        Will continue to wait for a connection indefinetly, and return when a connection
        has been established. Will raise on errors.

        :return: The client socket, or None if the server is stopping.
        :rtype: socket.socket | None
        """
        while not self._stop_event.is_set():
            try:
                try:
                    client_socket, addr = self._server_socket.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        break
                    self.log.error("Accept failed")
                    continue

                self.log.info(f"Client connected from {addr}")

                # set the socket options
                client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                # return the client socket
                return client_socket
            except Exception as e:
                self.log.critical(f"Unexpected error in network loop: {e}")
                raise
        return None

    def _close_client(self) -> None:
        """Close the client socket, signal the disconnect to the workers and clear both queues."""
        if self._client_socket is not None:
            try:
                self._client_socket.close()
            except OSError:
                pass
            self._client_socket = None

        # signal to the workers that the client is disconnected
        self._client_connected.clear()
        self._receive_worker.clear_input_queue()
        self._send_worker.clear_output_queue()
        self.log.debug("closed client connection and cleared queues")

    def _perform_handshake(self) -> bool:
        """Perform handshake authentication with the client.

        This function tryes to be as safe as possible, returning false on errors.
        The client must answer within HANDSHAKE_TIMEOUT_SECONDS.

        :return: True if authenticated, False otherwise.
        :rtype: bool
        """
        # put the handshake payload in the output queue
        self._queue_out.put(FIREQNetworkPacket({"type": "handshake"}))
        self.log.info("Handshake put in queue, waiting for client response...")

        # wait for the response on the input queue
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_SECONDS
        while not self._stop_event.is_set() and self._client_connected.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.log.warning("Handshake timed out")
                return False
            try:
                response = self._queue_in.get(timeout=min(0.5, remaining))
            except Empty:
                continue

            # wait for the client response and handle it
            if response.get("type") != "handshake_ack":
                self.log.warning(f"Invalid handshake response: {response.get('type')}")
                return False

            client_token = response.get("token", "")
            client_name = response.get("client_name", "unknown")

            if client_token != self._auth_token:
                self.log.warning(f"Authentication failed for client '{client_name}'")
                return False

            self.log.info(f"Client '{client_name}' authenticated successfully")
            return True

        self.log.debug("Received the stop condition or client disconnected on handshake")
        return False

    def _config_from_message(self, message: dict) -> list | None:
        """Apply the "system" configuration dict of a client message to the system.

        Sends a status "ok" packet on success, or an error packet on failure.

        :param message: network message fetched from the client
        :type message: dict
        :return: the sweepable callbacks of the configuration, or None if the configuration failed
        :rtype: list | None
        """
        config = message.get("system", None)
        if config is None:
            self.log.warning("Could not get the system dict from the client message, aborting apply")
            self._queue_out.put(
                FIREQNetworkPacket({"type": "error", "msg": "system dict not found in command message"})
            )
            return None

        # apply the configuration to the system
        try:
            callbacks = self._fireq_soc.apply_configuration(config)
        except Exception as e:
            self.log.warning(f"Invalid configuration: {e}")
            self._queue_out.put(
                FIREQNetworkPacket({"type": "error", "msg": f"system dict for run experiment command is invalid: {e}"})
            )
            return None

        self._queue_out.put(FIREQNetworkPacket({"type": "status", "msg": "ok"}))
        # return the callbacks
        return callbacks

    def _run_experiment(self) -> None:
        """Run an experiment and time the execution.

        Sends an "iteration_ended" status message to the client that includes the execution time in ns.
        Exceptions are propagated to the caller.
        """
        start = time.perf_counter_ns()
        self._fireq_soc.run_experiment(self._queue_out)
        end = time.perf_counter_ns()
        # send a status message to the client that includes the execution time
        self._queue_out.put(FIREQNetworkPacket({"type": "status", "msg": "iteration_ended", "time": end - start}))

    def _should_abort_run(self) -> bool:
        """Return True if a running sweep must stop (server stopping or client disconnected)."""
        return self._stop_event.is_set() or not self._client_connected.is_set()

    def _config_and_run(self, message: dict) -> None:
        """
        Configure from network message and run experiment.

        Will send an experiment header followed by the DMA payloads.
        The iteration end is indicated by an "iteration_ended" packet followed
        by another iteration or an experiment footer.
        Once the header has been sent, the footer is always sent: if the run fails an error packet
        is sent first and the footer has "aborted" set to True.

        :param message: network message fetched from the client
        :type message: dict
        """
        callbacks = self._config_from_message(message)
        if callbacks is None:
            # the error packet has already been sent by _config_from_message
            self.log.warning("Configuration failed to apply, aborting experiment run")
            return

        start_message = {"shots": self._fireq_soc.shots}
        exp = None
        if callbacks:
            variables = get_sweep_variables(message)
            if not variables:
                self.log_and_send_error(
                    "Message from client tryed to execute a sweep experiment without specifing variables"
                )
                return
            try:
                exp = SweepExperiment(self, callbacks, variables, should_abort=self._should_abort_run)
            except Exception as e:
                self.log_and_send_error(f"Invalid sweep definition: {e}")
                return
            start_message["variable_order"] = exp.vars_order
            # numpy arrays cannot be serialized by msgpack
            start_message["variable_values"] = {key: values.tolist() for key, values in exp.computed_vars.items()}

        self._queue_out.put(FIREQNetworkPacket({"type": "status", "msg": "experiment_header"} | start_message))

        end_message = {"aborted": False}
        start = time.perf_counter_ns()
        try:
            if exp is not None:
                exp.run()
            else:
                self._run_experiment()
        except SweepAbortedError:
            self.log.warning("Sweep aborted, the server is stopping or the client disconnected")
            end_message["aborted"] = True
        except Exception as e:
            self.log.exception("Exception occurred while running experiment")
            self._queue_out.put(FIREQNetworkPacket({"type": "error", "msg": f"error while running experiment: {e}"}))
            end_message["aborted"] = True
        end_message["run_time"] = time.perf_counter_ns() - start

        self._queue_out.put(FIREQNetworkPacket({"type": "status", "msg": "experiment_footer"} | end_message))

    def log_and_send_warning(self, warning: str) -> None:
        """Log a warning and send it to the client as a "warning" packet.

        :param warning: warning message
        :type warning: str
        """
        self.log.warning(warning)
        self._queue_out.put(FIREQNetworkPacket({"type": "warning", "msg": f"{warning}"}))

    def log_and_send_error(self, error: str) -> None:
        """Log an error and send it to the client as an "error" packet.

        :param error: error message
        :type error: str
        """
        self.log.error(error)
        self._queue_out.put(FIREQNetworkPacket({"type": "error", "msg": f"{error}"}))
