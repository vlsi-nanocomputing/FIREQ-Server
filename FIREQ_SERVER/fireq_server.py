"""FIREQ TCP Server - single-client TCP server for hardware experiments.

Handles client connections at startup, owns the client socket and handles the connection lifetime
Handles the execution of commands received from the client

Architecture: 3 threads communicate via queue_in/queue_out.
- Main thread: executes commands, interfaces hardware (this module)
- Receiver thread: accepts connections, parses messages
- Sender thread: writes responses to client

Protocol:
- Requests: 4-byte big-endian length prefix + UTF-8 JSON payload.
- Responses: JSON messages (4-byte length) and streamed binary data (acquistion frames + timing).
  For run_experiment/run_sweep: StreamHeader (JSON) -> BinaryChunk (frames) -> StreamTiming (JSON).
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

MAX_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB max payload
QUEUE_MAX_MEMORY_BYTES = 1024 * 1024 * 1024  # 1 GB queue memory limit
HANDSHAKE_TIMEOUT_SECONDS = 3.0
SWEEP_WAIT_TIMEOUT_SECONDS = 10.0

# Exception handling configuration: (error_type, log_level, log_suffix)
# log_level: "warning", "error", or "exception"
# _EXCEPTION_CONFIG: dict[type, tuple[str, str, str]] = {
#    DMATimeoutError: ("timeout", "warning", "timed out"),
#    ConfigurationError: ("config", "warning", "config error"),
#    WaveCompilationError: ("data", "warning", "data error"),
#    EnvelopeUploadError: ("data", "warning", "data error"),
#    HardwareResourceError: ("hardware", "error", "hardware error"),
# }


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
        :param log: Optional log instance.
        :type log: logging.log | None
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
        self._abort_in_progress = Event()
        self._client_connected = Event()

        # Thread objects for the receive and transmit channel
        self._receive_worker = ReceiveWorker(self._client_connected)
        self._send_worker = SendWorker(self._client_connected)

        # Getting the two network packet queues
        self._queue_in = self._receive_worker.queue_in
        self._queue_out = self._send_worker.queue_out

        # sockets
        self._server_socket: socket.socket
        self._client_socket: socket.socket

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
        while not self._stop_event.is_set():
            # accept a client connection
            self._client_socket = self._accept_client()

            # assign client socket and inform threads
            self._receive_worker.client_socket = self._client_socket
            self._send_worker.client_socket = self._client_socket
            self._client_connected.set()

            # perform the handshake
            self.log.info("Performing handshake on client...")
            self._client_socket.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
            if not self._perform_handshake():
                self.log.warning("A client connection failed to complete the handshake, closing connection")
                self._close_client()

            # now start the actual main loop
            self._main_loop()

            # if the main loop exits, understand why and perform cleanup if necessary

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

        Note: ``abort`` is handled in the receiver loop for immediacy.
        """
        cmd = get_command(msg)
        # session_id = msg.get("session_id", "")

        self.log.debug(f"Processing command: {cmd}")

        try:
            if cmd == "apply_configuration":
                # apply the system configuration dictionary to the fireq soc
                callbacks = self._config_from_message(msg)
                if len(callbacks) > 0:
                    self.log.warning("Configuration from apply_configuration has sweepable parameters")

            elif cmd == "config_and_run":
                # run an experiment
                callbacks = self._config_from_message(msg)
                if callbacks is None:
                    self.log.warning("Configuration failed to apply, aborting experiment run")
                    return
                if len(callbacks) > 0:
                    variables = get_sweep_variables(msg)
                    if not variables:
                        self.log_and_send_warning(
                            "Message from client tryed to execute a sweep experiment without specifing variables"
                        )
                        return
                    exp = SweepExperiment(self, self._queue_out)
                    execution_time = exp.run(callbacks, variables)
                    self._queue_out.put(
                        FIREQNetworkPacket({"type": "status", "msg": "sweep ended", "time": f"{execution_time} ns"})
                    )
                else:
                    self._run_experiment()

            elif cmd == "ping":
                self._queue_out.put(FIREQNetworkPacket({"resp": "pong"}))

            elif cmd == "reset_all":
                self._fireq_soc.reset_all()
                self._queue_out.put(FIREQNetworkPacket({"type": "status", "msg": "successfully reset"}))

            # elif cmd == "status":
            #    self.log.debug("status")

            # deprecated functions:
            # rf_mapping
            # calibrate_adc
            # reset_waves
            # reset_envelopes

            elif cmd == "logout":
                self._close_client()

            # elif cmd == "reset_all":
            #    self.log.debug("reset all")

            else:
                self.log.debug("received a non-supported command from the client")
                self._queue_out.put(FIREQNetworkPacket({"type": "error", "msg": f"unrecognized command '{cmd}'"}))

        except Exception as e:
            self.log.exception("Caught error while running command")

    # =========================================================================
    # Connection Handling
    # =========================================================================

    def _accept_client(self) -> socket.socket:
        """Accept a TCP connection on the server socket.

        Will continue to wait for a connection indefinetly, and return when a connection
        has been established. Will raise on errors.

        :return: The client socket.
        :rtype: socket.socket
        """
        # create the server socket and
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self._host, self._port))
        self._server_socket.listen(1)
        self._server_socket.settimeout(1.0)
        self.log.info(f"Listening on {self._host}:{self._port}")

        while not self._stop_event.is_set():
            try:
                try:
                    client_socket, addr = self._server_socket.accept()
                except TimeoutError:
                    continue
                except OSError:
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

    def _close_client(self) -> None:
        if self._client_connected.is_set():
            self._client_socket.close()
            self.log.debug("closing client connection ...")
        disconnected = self._client_connected.wait(3.0)
        if not disconnected:
            self._client_connected.clear()
            self.log.debug("manually cleared the client connected flag")
        # empty the buffers
        self._receive_worker.clear_input_queue()
        self._send_worker.clear_output_queue()
        self.log.debug("emptied the input and output queues")

    def _perform_handshake(self) -> bool:
        """Perform handshake authentication with the client.

        This function tryes to be as safe as possible, returning false on errors.

        :return: True if authenticated, False otherwise.
        :rtype: bool
        """
        # put the handshake payload in the output queue
        self._queue_out.put(FIREQNetworkPacket({"type": "handshake"}))
        self.log.info("Handshake put in queue, waiting for client response...")

        # wait for the response on the input queue
        while not self._stop_event.is_set() and self._client_connected.is_set():
            try:
                response = self._queue_in.get(timeout=0.5)
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

    def _config_from_message(self, message: dict) -> list:
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

        # return the callbacks
        return callbacks

    def _run_experiment(self) -> None:
        # actually run the experiment
        self._queue_out.put(FIREQNetworkPacket({"type": "status", "msg": "experiment started"}))
        try:
            start = time.perf_counter_ns()
            self._fireq_soc.run_experiment(self._queue_out)
            end = time.perf_counter_ns()
        except Exception as e:
            self.log.exception("Exception occurred while running experiment")
            self._queue_out.put(FIREQNetworkPacket({"type": "error", "msg": "error while running experiment"}))
            return

        self._queue_out.put(
            FIREQNetworkPacket({"type": "status", "msg": "experiment ended", "time": f"{end-start} ns"})
        )

    def log_and_send_warning(self, warning: str):
        self.log.warning(warning)
        self._queue_out.put(FIREQNetworkPacket({"type": "warning", "msg": f"{warning}"}))

    def log_and_send_error(self, error: str):
        self.log.warning(error)
        self._queue_out.put(FIREQNetworkPacket({"type": "warning", "msg": f"{error}"}))
