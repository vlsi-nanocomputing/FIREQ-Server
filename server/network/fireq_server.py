# file: fireq-utils/server/network/fireq_server.py
"""FIREQ TCP Server - single-client TCP server for hardware experiments.

Architecture: 3 threads communicate via queue_in/queue_out.
- Main thread: executes commands, interfaces hardware
- Receiver thread: accepts connections, parses messages
- Sender thread: writes responses to client

Protocol:
- Requests: 4-byte big-endian length prefix + UTF-8 JSON payload.
- Responses: JSON messages (4-byte length) and streamed binary data (acquistion frames + timing).
  For run_experiment/run_sweep: StreamHeader (JSON) → BinaryChunk (frames) → StreamTiming (JSON).
"""
import json
import logging
import socket
import struct
import time
from collections.abc import Iterator
from queue import Empty, Full, Queue
from threading import Event, Thread

import numpy as np

from ..execution.handlers import EnvelopeHandler
from ..execution.message_handler import MessageHandler
from ..models import (
    BinaryChunk,
    ConfigurationError,
    DMATimeoutError,
    EnvelopeUploadError,
    HardwareResourceError,
    StreamHeader,
    StreamTiming,
    WaveCompilationError,
)
from .memory_queue import MemoryBoundedQueue

MAX_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB max payload
QUEUE_MAX_MEMORY_BYTES = 1024 * 1024 * 1024  # 1 GB queue memory limit
HANDSHAKE_TIMEOUT_SECONDS = 3.0
SWEEP_WAIT_TIMEOUT_SECONDS = 10.0

# Exception handling configuration: (error_type, log_level, log_suffix)
# log_level: "warning", "error", or "exception"
_EXCEPTION_CONFIG: dict[type, tuple[str, str, str]] = {
    DMATimeoutError: ("timeout", "warning", "timed out"),
    ConfigurationError: ("config", "warning", "config error"),
    WaveCompilationError: ("data", "warning", "data error"),
    EnvelopeUploadError: ("data", "warning", "data error"),
    HardwareResourceError: ("hardware", "error", "hardware error"),
}


class FIREQServer:
    """TCP server for FIREQ experiments.

    Routes client JSON commands to a :class:`~.message_handler.MessageHandler`.

    :param handler: MessageHandler for command execution.
    :type handler: MessageHandler
    :param host: TCP bind address ("0.0.0.0" for all interfaces).
    :type host: str
    :param port: TCP port to bind.
    :type port: int
    :param auth_token: Shared token for handshake authentication.
    :type auth_token: str
    :param logger: Optional logger instance.
    :type logger: logging.Logger | None
    """

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def __init__(
        self,
        handler: MessageHandler,
        host: str = "0.0.0.0",
        port: int = 5000,
        auth_token: str = "fireq",
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize the FIREQ TCP server.

        :param handler: MessageHandler instance for command execution.
        :type handler: MessageHandler
        :param host: TCP bind address (use "0.0.0.0" for all interfaces).
        :type host: str
        :param port: TCP port to bind.
        :type port: int
        :param auth_token: Shared token for client authentication.
        :type auth_token: str
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        """
        self.handler = handler
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.logger = logger or logging.getLogger(__name__)

        # Inter-thread queues (receiver -> queue_in -> main -> queue_out -> sender)
        self.queue_in = Queue()
        self.queue_out = MemoryBoundedQueue(max_memory_bytes=QUEUE_MAX_MEMORY_BYTES)

        # Server state
        self._running = False
        self._stop_event = Event()  # Cooperative sweep cancellation
        self._authenticated = False
        self._abort_in_progress = Event()
        self._sweep_active = Event()  # Tracks active sweep for cleanup sync
        self._sender_dead = Event()  # Signals sender thread death
        self._cleanup_done = Event()  # Main-thread cleanup acknowledgment

        self._server_socket: socket.socket | None = None
        self._client_socket: socket.socket | None = None

    def start(self) -> None:
        """Start the server and block on the main thread.

        Starts the network accept/receiver thread (daemon), then runs the main
        experiment loop in the current thread. Stops when :meth:`stop` is called.
        """
        self._running = True
        self._net_thread = Thread(target=self._network_receiver_thread, daemon=True)
        self._net_thread.start()
        self.logger.info(f"FIREQ Server started on {self.host}:{self.port}")
        self._main_loop()

    def stop(self) -> None:
        """Stop the server gracefully.

        Sets running flag to False, triggers stop event to abort sweeps,
        unblocks the main loop, and closes server/client sockets.
        """
        self.logger.info("Stopping server...")
        self._running = False
        self._stop_event.set()
        self.queue_in.put(None)

        for sock in (self._server_socket, self._client_socket):
            if sock:
                try:
                    sock.close()
                except (OSError, Exception):
                    pass

    # =========================================================================
    # MAIN THREAD - Command Execution
    # =========================================================================

    def _main_loop(self) -> None:
        """Consume commands from ``queue_in`` and execute them.

        Runs on the main thread for hardware stability. Exits on ``None``
        from ``queue_in`` or when ``_running`` becomes False.
        """
        self.logger.info("Main loop started, waiting for commands...")

        while self._running:
            try:
                msg = self.queue_in.get(timeout=1.0)
            except Empty:
                if self._stop_event.is_set():
                    self._do_hardware_cleanup("Client disconnect")
                    self._stop_event.clear()
                    self._cleanup_done.set()
                continue

            if msg is None:
                break

            self._process_message(msg)

        self.logger.info("Main loop exited")

    def _process_message(self, msg: dict) -> None:
        """Execute one command message and enqueue a response.

        :param msg: Parsed command dictionary from the client.
        :type msg: dict

        Note: ``abort`` is handled in the receiver loop for immediacy.
        """
        cmd = msg.get("cmd", "")
        session_id = msg.get("session_id", "")

        self.logger.info(f"Processing command: {cmd}")

        try:
            if cmd == "upload_envelopes":
                total_envelopes, invalid_metadata = EnvelopeHandler.validate_metadata(msg)

                if invalid_metadata:
                    raise ValueError("Envelope metadata must include num_samples.")

                envelope_data = msg.get("envelope_data")
                if total_envelopes > 0 and envelope_data is None:
                    raise ValueError("Missing binary envelope frames.")

                result = self.handler.env_h.upload(msg, envelope_data)
                self.queue_out.put(self._build_response(cmd, session_id, result))

            elif cmd == "compile_waves":
                result = self.handler.wave_h.compile(msg)
                self.queue_out.put(self._build_response(cmd, session_id, result))

            elif cmd == "run_experiment":
                self._stop_event.clear()
                t_start = time.perf_counter()
                last_timing = self._stream_items_to_queue(self.handler.run(msg, cmd, session_id))
                self._add_server_timing(last_timing, t_start)

            elif cmd == "run_sweep":
                t_start = time.perf_counter()
                self._do_hardware_cleanup("Pre-sweep")
                self._sweep_active.set()
                self._stop_event.clear()

                sweep_id = msg.get("sweep_id", "unnamed")
                self.logger.info(f"Sweep '{sweep_id}' with stream_mode=header_binary")

                try:
                    last_timing = self._stream_items_to_queue(
                        self.handler.run_sweep(
                            msg,
                            cmd,
                            session_id,
                            stop_check=lambda: self._stop_event.is_set() or self._sender_dead.is_set(),
                        )
                    )
                    self._add_server_timing(last_timing, t_start)
                finally:
                    self._sweep_active.clear()

            elif cmd == "ping":
                self.queue_out.put(self._build_response("pong", session_id, ok=True))

            elif cmd == "status":
                self.queue_out.put(
                    self._build_response(
                        cmd,
                        session_id,
                        ok=True,
                        generators=self.handler.status_h.get_all_generators_status(),
                    )
                )

            elif cmd == "logout":
                self._handle_logout()

            elif cmd == "reset_waves":
                gen_index = msg.get("gen_index", 0)
                preserve_wave_specs = msg.get("preserve_wave_specs", True)
                result = self.handler.reset_h.reset_waves(gen_index, preserve_wave_specs)
                self.queue_out.put(self._build_response(cmd, session_id, result))

            elif cmd == "reset_envelopes":
                gen_index = msg.get("gen_index", 0)
                result = self.handler.reset_h.reset_envelopes(gen_index)
                self.queue_out.put(self._build_response(cmd, session_id, result))

            elif cmd == "reset_all":
                preserve_wave_specs = msg.get("preserve_wave_specs", False)
                results = self.handler.reset_h.reset_all_generators(preserve_wave_specs)
                self.queue_out.put(self._build_response(cmd, session_id, ok=True, results=results))

            else:
                self.queue_out.put(self._build_response(cmd, session_id, ok=False, error=f"Unknown command: {cmd}"))

        except (
            DMATimeoutError,
            ConfigurationError,
            WaveCompilationError,
            EnvelopeUploadError,
            HardwareResourceError,
        ) as e:
            self._handle_command_error(cmd, session_id, e)
        except Exception as e:
            self._handle_command_error(cmd, session_id, e)

    # =========================================================================
    # NETWORK THREADS - Connection Handling
    # =========================================================================

    def _network_receiver_thread(self) -> None:
        """Accept TCP connections and handle one client at a time (single-client design)."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(1)
        self._server_socket.settimeout(1.0)
        self.logger.info(f"Listening on {self.host}:{self.port}")

        while self._running:
            try:
                try:
                    client_socket, addr = self._server_socket.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._running:
                        self.logger.error("Accept failed")
                    continue

                self.logger.info(f"Client connected from {addr}")
                client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                try:
                    self._handle_client(client_socket)
                except Exception as e:
                    self.logger.error(f"Critical error handling client: {e}")
            except Exception as main_e:
                self.logger.critical(f"Unexpected error in network loop: {main_e}")

        try:
            self._server_socket.close()
        except (OSError, Exception):
            pass
        self.logger.info("Network thread exited")

    def _handle_client(self, client_socket: socket.socket) -> None:
        """Handle a single client session: handshake, sender thread, receiver loop, cleanup.

        :param client_socket: Connected client socket.
        :type client_socket: socket.socket
        """
        self._client_socket = client_socket
        self._authenticated = False
        client_socket.settimeout(None)

        try:
            client_socket.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
            if not self._do_handshake(client_socket):
                self.logger.warning("Handshake failed, closing connection")
                return
            client_socket.settimeout(None)

            self._sender_dead.clear()
            sender = Thread(target=self._sender_loop, daemon=True)
            sender.start()
            self._receiver_loop(client_socket)
            self.queue_out.put(None)
            sender.join(timeout=2.0)

        except Exception as e:
            self.logger.error(f"Client handler error: {e}")

        finally:
            self.logger.info("Client teardown...")
            self._stop_event.set()

            # Drain stale messages from disconnected client
            while not self.queue_in.empty():
                try:
                    self.queue_in.get_nowait()
                except Empty:
                    break

            # Wait for main loop to finish current message + do cleanup
            self._cleanup_done.clear()
            cleanup_timeout = SWEEP_WAIT_TIMEOUT_SECONDS + 5.0
            if not self._cleanup_done.wait(timeout=cleanup_timeout):
                self.logger.warning("Cleanup acknowledgment timeout (%.1fs)", cleanup_timeout)

            self.queue_out.clear()

            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
            try:
                client_socket.close()
            except Exception:
                pass
            self._client_socket = None
            self._authenticated = False
            self._sender_dead.clear()
            self.logger.info("Client disconnected, ready for new connection")

    def _receiver_loop(self, sock: socket.socket) -> None:
        """Receive client messages and enqueue commands. Abort bypasses queue_in.

        :param sock: Connected socket.
        :type sock: socket.socket
        """
        while self._running:
            try:
                msg = self._receive_message(sock)
                if msg is None:
                    self.logger.info("Client connection closed")
                    break

                cmd = msg.get("cmd", "")
                if cmd == "abort":
                    self.logger.info("Abort command received")
                    self._stop_event.set()
                    self._abort_in_progress.set()
                    self.queue_out.clear()
                    self.queue_out.put(
                        {
                            "ok": True,
                            "cmd": "abort",
                            "session_id": msg.get("session_id", ""),
                            "message": "Sweep aborted",
                        }
                    )
                else:
                    if cmd == "upload_envelopes":
                        total_envelopes, invalid_metadata = EnvelopeHandler.validate_metadata(msg)
                        if (not invalid_metadata) and total_envelopes > 0:
                            msg["envelope_data"] = self._recv_envelope_frames(sock, total_envelopes)
                            self.logger.info(f"Received {total_envelopes} binary envelope frames")
                    self.queue_in.put(msg)
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                self.logger.warning("Connection lost")
            except Exception as e:
                self.logger.error(f"Receiver error: {e}")
                break

    def _sender_loop(self) -> None:
        """Send responses from ``queue_out`` to client. Exits on ``None`` or disconnect."""
        while self._running:
            try:
                item = self.queue_out.get(timeout=1.0)
            except Empty:
                continue

            if item is None:
                break

            try:
                if self._client_socket is None:
                    break

                # Skip non-abort items when abort is in progress
                is_abort_cmd = isinstance(item, dict) and item.get("cmd") == "abort"
                if self._abort_in_progress.is_set() and not is_abort_cmd:
                    continue

                # Handle typed queue items (streaming commands)
                if isinstance(item, StreamHeader):
                    self._send_message(self._client_socket, item.metadata)

                elif isinstance(item, BinaryChunk):
                    for acq_ip_idx, arr in item.binary_data.items():
                        self._send_binary_frame(self._client_socket, acq_ip_idx, arr)
                    if item.timing:
                        self._send_timing_trailer(self._client_socket, item.timing[0], item.timing[1])

                elif isinstance(item, StreamTiming):
                    include_timing = item.type == "sweep_status"
                    self._send_message(self._client_socket, item.metadata, include_timing=include_timing)

                elif isinstance(item, dict):
                    # Legacy dict handling for simple commands (ping, status, reset_*, abort)
                    self._send_message(self._client_socket, item)
                    if is_abort_cmd:
                        self._abort_in_progress.clear()

            except (BrokenPipeError, ConnectionResetError, OSError):
                self.logger.error("Client disconnected during send")
                self._stop_event.set()
                self._sender_dead.set()
                self.queue_out.clear()
                break
            except Exception as e:
                self.logger.error(f"Send error: {e}")
                self._sender_dead.set()
                self.queue_out.clear()
                break

        self.logger.debug("Sender loop exited")

    # =========================================================================
    # HANDSHAKE & AUTHENTICATION
    # =========================================================================

    def _build_handshake_info(self) -> dict:
        """Build the server -> client handshake message."""
        return {
            "type": "handshake",
            "protocol_version": "0.3.0",
            "hw_summary": self.handler.status_h.hw_summary,
        }

    def _do_handshake(self, sock: socket.socket) -> bool:
        """Perform handshake authentication with the client.

        :param sock: Connected socket.
        :type sock: socket.socket
        :return: True if authenticated, False otherwise.
        :rtype: bool
        """
        try:
            self._send_message(sock, self._build_handshake_info())
            self.logger.info("Handshake sent, waiting for client response...")

            response = self._receive_message(sock)
            if response is None:
                self.logger.warning("Client disconnected during handshake")
                return False

            if response.get("type") != "handshake_ack":
                self.logger.warning(f"Invalid handshake response: {response.get('type')}")
                return False

            client_token = response.get("token", "")
            client_name = response.get("client_name", "unknown")

            if client_token != self.auth_token:
                self.logger.warning(f"Authentication failed for client '{client_name}'")
                self._send_message(sock, {"type": "handshake_error", "error": "Invalid token"})
                return False

            self._authenticated = True
            self._send_message(sock, {"type": "handshake_ok", "message": f"Welcome {client_name}"})
            self.logger.info(f"Client '{client_name}' authenticated successfully")
            return True

        except Exception as e:
            self.logger.error(f"Handshake failed: {e}")
            return False

    def _handle_logout(self) -> None:
        """Reset server-side caches and notify the client."""
        self.logger.info("Logout requested, resetting caches...")
        try:
            results = self.handler.reset_h.reset_all_generators(preserve_wave_specs=False)
            for r in results:
                if not r["waves"]["ok"]:
                    self.logger.warning(f"Wave reset failed for gen {r['gen_index']}")
                if not r["envelopes"]["ok"]:
                    self.logger.warning(f"Envelope reset failed for gen {r['gen_index']}")
            self.queue_out.put(
                {
                    "ok": True,
                    "cmd": "logout",
                    "message": f"Logout successful, {len(results)} generator(s) reset",
                }
            )
        except Exception as e:
            self.logger.exception("Logout failed")
            self.queue_out.put({"ok": False, "cmd": "logout", "error": str(e)})

    # =========================================================================
    # PROTOCOL - Length-Prefixed JSON & Binary Frames
    # =========================================================================

    def _receive_message(self, sock: socket.socket) -> dict | None:
        """Receive one length-prefixed JSON message (4-byte big-endian length + UTF-8 JSON).

        :param sock: Connected socket.
        :type sock: socket.socket
        :return: Parsed JSON dictionary, or None on disconnect/error.
        :rtype: dict | None
        """
        try:
            length_bytes = self._recv_exact(sock, 4)
            if not length_bytes:
                return None
            length = int.from_bytes(length_bytes, "big")
            if length > MAX_PAYLOAD_BYTES:
                self.logger.error(f"Payload too large: {length} bytes")
                return None
            payload = self._recv_exact(sock, length)
            if not payload:
                return None
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            self.logger.error("Malformed JSON received")
            return None
        except Exception as e:
            self.logger.error(f"Receive error: {e}")
            return None

    def _send_message(self, sock: socket.socket, msg: dict, include_timing: bool = False) -> None:
        """Send one length-prefixed JSON message.

        :param sock: Connected socket.
        :type sock: socket.socket
        :param msg: Message payload to send.
        :type msg: dict
        :param include_timing: Whether to add encoding timing metadata.
        :type include_timing: bool
        """
        if include_timing:
            debug_timing = msg.setdefault("debug_timing", {})
            t0 = time.perf_counter()
            payload = json.dumps(msg).encode("utf-8")
            debug_timing["server_encode_ms"] = (time.perf_counter() - t0) * 1000.0
            debug_timing["payload_bytes"] = len(payload)
            payload = json.dumps(msg).encode("utf-8")
        else:
            payload = json.dumps(msg).encode("utf-8")
        sock.sendall(len(payload).to_bytes(4, "big") + payload)

    def _send_binary_frame(self, sock: socket.socket, acq_index: int, data: np.ndarray) -> None:
        """Send binary frame: [4B AcqIP][data].

        Client computes data length and valid_words from request params + acq_ip_metadata.

        :param sock: Connected socket.
        :type sock: socket.socket
        :param acq_index: Acquisition IP index.
        :type acq_index: int
        :param data: Numpy array to transmit.
        :type data: np.ndarray
        """
        t_start = time.perf_counter()
        self.logger.debug(
            f"Sending binary frame: AcqIp={acq_index}, size={data.nbytes}B, " f"dtype={data.dtype}, shape={data.shape}"
        )
        sock.sendall(struct.pack(">I", acq_index))
        sock.sendall(memoryview(data))
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        data_bytes_len = data.nbytes
        if elapsed_ms > 50:
            self.logger.warning(f"Slow send: AcqIp {acq_index}, {data_bytes_len}B in {elapsed_ms:.1f}ms")

    def _send_timing_trailer(self, sock: socket.socket, hw_ms: float, sw_ms: float) -> None:
        """Send timing trailer (2x float32 big-endian).

        :param sock: Connected socket.
        :type sock: socket.socket
        :param hw_ms: FPGA active time in ms.
        :type hw_ms: float
        :param sw_ms: Software overhead in ms.
        :type sw_ms: float
        """
        sock.sendall(struct.pack(">ff", float(hw_ms), float(sw_ms)))

    def _recv_envelope_frames(self, sock: socket.socket, total_count: int) -> dict[tuple[int, int], np.ndarray]:
        """Receive envelope frames: [4B gen][4B env][4B samples][N*8B IQ data].

        :param sock: Connected socket.
        :type sock: socket.socket
        :param total_count: Number of envelopes to receive.
        :type total_count: int
        :return: Dict mapping (gen_idx, env_idx) to float32 I/Q array.
        :rtype: dict[tuple[int, int], np.ndarray]
        """
        envelope_data = {}
        for _ in range(total_count):
            header = self._recv_exact(sock, 12)
            if header is None:
                raise ConnectionError("Incomplete envelope frame header")
            gen_idx, env_idx, num_samples = struct.unpack(">III", header)

            sample_bytes = self._recv_exact(sock, num_samples * 8)
            if sample_bytes is None:
                raise ConnectionError(f"Incomplete envelope samples for gen={gen_idx}, env={env_idx}")
            envelope_data[(gen_idx, env_idx)] = np.frombuffer(sample_bytes, dtype=np.float32).reshape(num_samples, 2)
        return envelope_data

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes | None:
        """Receive exactly *n* bytes. Returns None on disconnect.

        :param sock: Connected socket.
        :type sock: socket.socket
        :param n: Number of bytes to receive.
        :type n: int
        :return: Bytes received or None on disconnect.
        :rtype: bytes | None
        """
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _safe_queue_put(self, item: object, timeout: float = 1.0) -> bool:
        """Put item to queue_out with timeout, checking for sender death.

        :param item: Item to enqueue.
        :type item: object
        :param timeout: Timeout per attempt in seconds.
        :type timeout: float
        :return: True if enqueued, False if sender died or stop requested.
        :rtype: bool
        """
        t_start, retry_count = time.perf_counter(), 0
        while self._running:
            if self._sender_dead.is_set() or self._stop_event.is_set():
                return False
            try:
                self.queue_out.put(item, timeout=timeout)
                if retry_count > 0:
                    self.logger.warning(
                        f"Queue put after {retry_count} retries, {(time.perf_counter() - t_start) * 1000:.1f}ms"
                    )
                return True
            except Full:
                retry_count += 1
                if retry_count % 5 == 0:
                    self.logger.warning(f"Queue full, retry #{retry_count}")
        return False

    def _build_response(self, cmd: str, session_id: str, result: object = None, **extra: object) -> dict:
        """Build a standard command response dictionary.

        :param cmd: Command name.
        :type cmd: str
        :param session_id: Client session identifier.
        :type session_id: str
        :param result: Optional result object with to_dict() method.
        :type result: object
        :param extra: Additional fields to include in the response.
        :return: Response dictionary ready for queue_out.
        :rtype: dict
        """
        response = {"cmd": cmd, "session_id": session_id}
        if result is not None:
            if hasattr(result, "to_dict"):
                response.update(result.to_dict())
            elif isinstance(result, dict):
                response.update(result)
        response.update(extra)
        return response

    def _add_server_timing(self, timing_item: StreamTiming | None, t_start: float) -> None:
        """Add total server time to a StreamTiming item's metadata.

        :param timing_item: StreamTiming item to update, or None.
        :type timing_item: StreamTiming | None
        :param t_start: Start time from time.perf_counter().
        :type t_start: float
        """
        if timing_item is not None:
            debug_timing = timing_item.metadata.setdefault("debug_timing", {})
            debug_timing["total_server_time_ms"] = (time.perf_counter() - t_start) * 1000
            self._safe_queue_put(timing_item)

    def _stream_items_to_queue(
        self, items_generator: Iterator[StreamHeader | BinaryChunk | StreamTiming]
    ) -> StreamTiming | None:
        """Stream items from generator to queue_out, tracking the last timing item.

        :param items_generator: Generator yielding stream items.
        :return: The last StreamTiming item encountered, or None.
        :rtype: StreamTiming | None
        """
        last_timing = None
        try:
            for item in items_generator:
                if isinstance(item, StreamTiming):
                    last_timing = item
                    continue
                if not self._safe_queue_put(item):
                    break
        finally:
            items_generator.close()
        return last_timing

    def _handle_command_error(self, cmd: str, session_id: str, exc: Exception) -> None:
        """Handle command execution errors with appropriate logging and response.

        :param cmd: Command name that failed.
        :type cmd: str
        :param session_id: Client session identifier.
        :type session_id: str
        :param exc: The exception that occurred.
        :type exc: Exception
        """
        config = _EXCEPTION_CONFIG.get(type(exc))
        if config:
            error_type, log_level, log_suffix = config
            getattr(self.logger, log_level)(f"Command '{cmd}' {log_suffix}: {exc}")
        else:
            error_type = "unknown"
            self.logger.exception(f"Command '{cmd}' failed")
        self.queue_out.put(self._build_response(cmd, session_id, ok=False, error=str(exc), error_type=error_type))

    def _do_hardware_cleanup(self, context: str) -> None:
        """Best-effort hardware cleanup to avoid stale DMA/acquisition state.

        Must be called from the main thread only — serialized with hardware ops.

        :param context: Description for log messages (e.g., "Client disconnect").
        :type context: str
        """
        try:
            self.handler.cleanup()
        except Exception as e:
            self.logger.warning(f"{context}: cleanup failed: {e}")
