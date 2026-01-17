#file: fireq-utils/server/tcp_server.py
"""
FIREQ TCP Server.

This module implements a single-client TCP server that receives length-prefixed JSON commands
from a client and executes them through a MessageHandler (message_handler.py).

High-level architecture
-----------------------
The server uses three concurrent execution roles:

1) Main thread (hardware/experiment runner)
   - Runs :meth:`FIREQServer._main_loop`.
   - Consumes parsed commands from ``queue_in`` and executes them via the handler.
   - Produces response dictionaries into ``queue_out``.
   - REMARK : this is the only thread interfacing the HW. 

2) Network receiver thread (connection acceptor / client session)
   - Transport-only thread : the thread does not directly interfaces the HW.
   - Runs :meth:`FIREQServer._network_receiver_thread`.
   - Accepts incoming TCP connections and handles one client at a time.
   - After handshake, runs a blocking receiver loop that parses messages and
     enqueues them into ``queue_in``. The only exception is the immediate command "abort".
 
3) Network sender thread (per-client)
   - Transport-only thread : the thread does not directly interfaces the HW.
   - Runs :meth:`FIREQServer._sender_loop`.
   - Pops responses from ``queue_out`` and writes them to the connected client.

Threads synchronization : queues are the only synchronization boundaries. Data flow through queue_in/queue_out
avoids race conditions.
   
Protocol
--------
Length-prefixed JSON:
    - 4 bytes unsigned length prefix (big-endian)
    - JSON UTF-8 payload

Lifecycle
---------
1. Server start -> listen for a client connection.
2. Client connects -> handshake:
   - server sends hardware summary
   - client replies with auth token
3. Normal operations:
   - upload_envelopes, compile_waves, run_experiment, run_sweep, status, etc.
4. Client disconnects -> internal states persist (envelope memory and wave cache), server returns to accept loop.
5. Explicit logout -> reset caches (client may then disconnect).

Notes
-----
- Hardware stability: experiment execution is deliberately kept on the main thread.
- Abort semantics: command ``abort`` bypasses the main queue and sets ``stop_event``
  immediately to stop sweeps quickly.
- Inter-thread communication uses thread-safe queues (queue_in/queue_out) as synchronization boundary.
"""
import json
import time
import socket
import logging
import numpy as np
from queue import Queue, Empty
from threading import Thread, Event
from typing import Optional, Dict, Any, List, Tuple
from .message_handler import MessageHandler, SweepPointResult, SweepStatus
import struct

# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================
# Number of points to accumulate before sending a response.
# Batches are meant to reduce network overhead during sweep experiments : they
# group results and send them in fewer messages (each batch up to batch_size points).
# Note that the last batch is expected to be "partially filled".
# It can be reconfigured for optimization
SWEEP_BATCH_SIZE = 10

# Maximum allowed payload size in bytes (10 MB)
# Protects against unexpectedly large frames from buggy clients or DoS attempts.
MAX_PAYLOAD_BYTES = 10 * 1024 * 1024 


class FIREQServer:
    """
    TCP server for FIREQ experiments.

    The server receives client commands as JSON dictionaries and routes them to a
    :class:`~.message_handler.MessageHandler` instance.

    Parameters
    ----------
    handler:
        The MessageHandler responsible for executing commands and producing results.
    host:
        TCP bind address. Use ``"0.0.0.0"`` to listen on all interfaces.
    port:
        TCP port to bind.
    auth_token:
        Shared-token required during handshake authentication. 
    logger:
        Optional logger instance. If not provided, ``logging.getLogger(__name__)``
        is used.

    Examples
    --------
    >>> handler = MessageHandler(adapter)
    >>> server = FIREQServer(handler, port=5000, auth_token="secret")
    >>> server.start()  # blocks the main thread
    """

    
    def __init__(
        self,
        handler: MessageHandler,
        host: str = "0.0.0.0",
        port: int = 5000,
        auth_token: str = "fireq",
        logger: Optional[logging.Logger] = None
    ):
        """
        Inizializza il server.
        
        :param handler: MessageHandler instance 
        :param host: bind address
        :param port: TCP port
        :param auth_token: simple authentication token
        :param logger: Logger instance
        """

        self.handler = handler
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.logger = logger or logging.getLogger(__name__)
        
        # Inter-thread queues

        # queue_in : filled by "receiver_thread", used by "main_thread".
        # It contains pre-parsed commands, ready to be executed by the "main_thread".
        # Only the main thread calls handler methods / touches hardware.
        # Purpose of queue_in: decouple network reception from command execution.
        self.queue_in = Queue() 

        # queue_out: filled by "main_thread", used by "sender_thread".
        # Contains response data produced by the main thread, including acquisitions.
        # for JSON serialization and client sending.
        # "Backpressure" (intentional)
        # If the client/connection is slow or not reading, queue_out may fill up and put() will block
        # Thus queue_out is clamped to a maximum dimension to avoid memory saturation.
        # Trade-off : large enough to exploit FPGA throughput BUT limited by the network bottleneck
        self.queue_out = Queue(250)  
        
        # Server status
        self._running = False
        # Cooperative cancellation flag for sweeps:
        # set by network threads on abort/disconnect, checked by handler.run_sweep().
        self._stop_event = Event()      
        self._authenticated = False
        self._abort_in_progress = Event()
        
        # Socket references (owned by the network threads, shared with the sender thread)
        self._server_socket: Optional[socket.socket] = None
        self._client_socket: Optional[socket.socket] = None
    
    # =========================================================================
    # PUBLIC API
    # =========================================================================
    
    def start(self):
        """
        Start the server and block on the main thread.

        This method:
        - starts the network accept/receiver thread (daemon thread),
        - then runs the main experiment loop in the current thread.

        The server stops when :meth:`stop` is called or when the main loop is
        unblocked with a "None" flag.
        """
        # self._running : global run flag shared by all loops (accept/recv/send/main).
        # Single source of truth for shutdown conditions : 
        # when running becomes False, each thread will exit its loop
        # at the next timeout/ iteration
        self._running = True
        
        # Network thread : owns the accept() loop and the per-connection receiver logic.
        # It must not handle the hardware : it only receives/parses JSON and forwards commands
        # to the main thread via queue_in. 
        # Marked "daemon" : if the main thread terminates unexpectedly, the main thread exits too (fail-safe behavior)
        self._net_thread = Thread(target=self._network_receiver_thread, daemon=True)
        self._net_thread.start()
        self.logger.info(f"FIREQ Server started on {self.host}:{self.port}")
        
        # Main thread runs the experiment execution loop. 
        self._main_loop()
        
    def stop(self):
        """Stop the server.

        Effects:
        - sets internal running flag to False,
        - sets the stop event (to abort any running sweep),
        - unblocks the main loop,
        - closes server/client sockets if present.
        """
        self.logger.info("Stopping server...")

        # Trigger cooperative shutdown across all loops.
        # Each thread checks _running periodically (via timeouts on accept/get)
        # and will exit on its next iteration without needing explicit joins here.
        self._running = False

        # Abort any active sweep (cooperative cancellation).
        # run_sweep() receives this Event and is expected to check it periodically
        # to stop early when requested (e.g., client sends "abort" or disconnects).
        self._stop_event.set()

        # None as a "flag" to unblock the main thread if it is waiting on queue_in.get().
        # The main loop treats None as a shutdown marker and exits.
        self.queue_in.put(None)  # Unblock main loop
        
        # Best-effort socket close to release network resources promptly.
        # These closes may race with network threads (or sockets may already be closed),
        # so we swallow exceptions here and rely on the sender/receiver loops to exit
        # when they observe connection errors.

        if self._server_socket:
            try:
                self._server_socket.close()
            except (OSError, Exception):
                pass

        if self._client_socket:
            try:
                self._client_socket.close()
            except (OSError, Exception):
                pass
    
    # =========================================================================
    # MAIN THREAD - experiment execution
    # =========================================================================
    
    def _main_loop(self):
        """
        Consume commands from ``queue_in`` and execute them.

        This loop runs on the main thread to keep hardware-related operations
        stable.

        The loop exits when it receives ``None`` from ``queue_in`` or when
        ``self._running`` becomes False.
        """
        self.logger.info("Main loop started, waiting for commands...")
        
        while self._running:
            try:
                msg = self.queue_in.get(timeout=1.0)
            except Empty:
                continue
            
            if msg is None:
                break
            
            self._process_message(msg)
        
        self.logger.info("Main loop exited")

    def _process_message(self, msg: dict):
        """
        Execute one command message and enqueue a response.

        Parameters
        ----------
        msg:
            Parsed command dictionary from the client.

        Supported commands
        ------------------
        - upload_envelopes
        - compile_waves
        - run_experiment
        - run_sweep
        - ping
        - status
        - logout
        - reset_waves
        - reset_envelopes
        - reset_all

        Notes
        -----
        Command ``abort`` is handled directly in the receiver loop (network thread)
        to be immediate; it bypasses ``queue_in``.
        """

        def ensure_dict(res):
            """Convert handler results to dict if they expose ``to_dict()``."""
            return res.to_dict() if hasattr(res, "to_dict") else res
        
        cmd = msg.get("cmd", "")
        session_id = msg.get("session_id", "")
        
        self.logger.info(f"Processing command: {cmd}")
        
        try:
            if cmd == "upload_envelopes":
                total_envelopes = 0
                invalid_metadata = False

                for gen_index_str, envelopes in msg.get("envelopes", {}).items():
                    for e in envelopes:
                        total_envelopes += 1
                        if "samples_iq" in e or "num_samples" not in e:
                            invalid_metadata = True

                if invalid_metadata:
                    raise ValueError(
                        "Envelope metadata must include num_samples."
                    )

                envelope_data = msg.get("envelope_data")
                if total_envelopes > 0 and envelope_data is None:
                    raise ValueError("Missing binary envelope frames.")

                # Call handler with binary data
                result = self.handler.env_h.upload(msg, envelope_data)
                self.queue_out.put({
                    "cmd": cmd,
                    "session_id": session_id,
                    **ensure_dict(result)
                })
            
            elif cmd == "compile_waves":
                result = self.handler.wave_h.compile(msg)
                self.queue_out.put({
                    "cmd": cmd,
                    "session_id": session_id,
                    **ensure_dict(result)
                })
            
            elif cmd == "run_experiment":
                t_start_server = time.perf_counter()

                result = self.handler.run(msg)

                t_end_server = time.perf_counter()
                server_duration_ms = (t_end_server - t_start_server) * 1000

                # Binary transmission mode: metadata + binary frames
                metadata = result.to_metadata_dict()
                metadata["cmd"] = cmd
                metadata["session_id"] = session_id
                metadata["type"] = "acquisition_metadata"

                # Inject timing information
                if "debug_timing" not in metadata:
                    metadata["debug_timing"] = {}
                metadata["debug_timing"]["total_server_time_ms"] = server_duration_ms
                if hasattr(self.handler.adapter, "last_timing_stats"):
                    metadata["debug_timing"].update(self.handler.adapter.last_timing_stats)

                # Enqueue both metadata and binary data
                self.queue_out.put({
                    "type": "metadata_with_binary",
                    "metadata": metadata,
                    "binary_data": result.get_binary_data()
                })
            
            elif cmd == "run_sweep":
                sweep_id = msg.get("sweep_id", "unnamed")

                # ============================================================
                # BATCHING CONFIGURATION
                # Client can override batch_size per-request for benchmarking
                # batch_size=1 effectively disables batching (legacy behavior)
                # ============================================================
                batch_size = msg.get("batch_size", SWEEP_BATCH_SIZE)
                if batch_size < 1:
                    batch_size = 1

                self.logger.info(
                    f"Sweep '{sweep_id}' with batch_size={batch_size}, stream_mode=header_binary"
                )

                t_start_sweep = time.perf_counter()

                # ============================================================
                # BATCHING: Local buffers for accumulating points
                # ============================================================
                binary_buffer: List[Dict[int, np.ndarray]] = []
                timing_buffer: List[Tuple[float, float]] = []
                batch_index = [0]  # Use list to allow mutation in nested function
                header_sent = [False]
                sweep_points_plan: List[Dict[str, Any]] = []

                def on_plan(points: List[Dict[str, Any]]):
                    sweep_points_plan.clear()
                    sweep_points_plan.extend(points)

                def flush_binary_batch():
                    """Send accumulated sweep points as binary-only batch."""
                    if not binary_buffer:
                        return

                    self.queue_out.put({
                        "type": "sweep_binary_batch",
                        "binary_data": list(binary_buffer),
                        "timing": list(timing_buffer)
                    })

                    self.logger.debug(
                        f"Sent binary-only batch {batch_index[0]} with {len(binary_buffer)} points"
                    )

                    binary_buffer.clear()
                    timing_buffer.clear()
                    batch_index[0] += 1

                def on_point(r: SweepPointResult):
                    """Callback for each sweep point."""
                    point_payload = r.to_metadata_dict()

                    # --- [TIMING] Inject per-point timing stats ---
                    if hasattr(self.handler.adapter, "last_timing_stats"):
                        if "debug_timing" not in point_payload:
                            point_payload["debug_timing"] = {}
                        point_payload["debug_timing"].update(
                            self.handler.adapter.last_timing_stats
                        )

                    if not header_sent[0]:
                        points_metadata = [
                            {"point_index": i, "variables": p}
                            for i, p in enumerate(sweep_points_plan)
                        ]
                        header_payload = {
                            "type": "sweep_header",
                            "cmd": cmd,
                            "session_id": session_id,
                            "sweep_id": sweep_id,
                            "stream_mode": "header_binary",
                            "batch_size": batch_size,
                            "n_points": r.n_total,
                            "points": points_metadata,
                            "adc_metadata": point_payload.get("adc_metadata", {})
                        }
                        self.queue_out.put({
                            "type": "sweep_header",
                            "metadata": header_payload
                        })
                        header_sent[0] = True

                    bin_data = r.get_binary_data()
                    if not bin_data:
                        return
                    binary_buffer.append(bin_data)
                    timing_stats = point_payload.get("debug_timing", {})
                    timing_buffer.append((
                        float(timing_stats.get("fpga_active_ms", 0.0)),
                        float(timing_stats.get("sw_overhead_ms", 0.0))
                    ))
                    if len(binary_buffer) >= batch_size:
                        flush_binary_batch()
                
                # Clear stop event for new sweep
                self._stop_event.clear()
                
                # Execute sweep
                status = self.handler.run_sweep(
                    msg,
                    on_point=on_point,
                    stop_event=self._stop_event,
                    on_plan=on_plan
                )
                
                # ============================================================
                # CRITICAL: Flush any remaining points in buffer
                # ============================================================
                flush_binary_batch()
                
                # --- [TIMING] Stop Sweep Timer ---
                t_end_sweep = time.perf_counter()
                total_sweep_duration_ms = (t_end_sweep - t_start_sweep) * 1000

                # Prepare final status payload
                status_payload = status.to_dict()

                # Inject total duration into the final report
                if "debug_timing" not in status_payload:
                    status_payload["debug_timing"] = {}
                status_payload["debug_timing"]["total_server_time_ms"] = total_sweep_duration_ms
                
                # Send final status
                self.queue_out.put({
                    "type": "sweep_status",
                    "cmd": cmd,
                    "session_id": session_id,
                    **status_payload
                })
            
            elif cmd == "ping":
                self.queue_out.put({
                    "ok": True,
                    "cmd": "pong",
                    "session_id": session_id
                })
            
            elif cmd == "status":
                self.queue_out.put({
                    "ok": True,
                    "cmd": cmd,
                    "session_id": session_id,
                    "generators": self.handler.status_h.get_all_generators_status()  # ✅
                })
            
            elif cmd == "logout":
                self._handle_logout()
            
            elif cmd == "reset_waves":
                gen_index = msg.get("gen_index", 0)
                preserve_specs = msg.get("preserve_specs", True)
                result = self.handler.reset_h.reset_waves(gen_index, preserve_specs)
                self.queue_out.put({
                    "cmd": cmd,
                    "session_id": session_id,
                    **ensure_dict(result)
                })
            
            elif cmd == "reset_envelopes":
                gen_index = msg.get("gen_index", 0)
                result = self.handler.reset_h.reset_envelopes(gen_index)
                self.queue_out.put({
                    "cmd": cmd,
                    "session_id": session_id,
                    **ensure_dict(result)
                })
            
            elif cmd == "reset_all":
                preserve_wave_specs = msg.get("preserve_wave_specs", False)
                results = self.handler.reset_h.reset_all_generators(preserve_wave_specs)
                self.queue_out.put({
                    "ok": True,
                    "cmd": cmd,
                    "session_id": session_id,
                    "results": results
                })
            
            else:
                self.queue_out.put({
                    "ok": False,
                    "cmd": cmd,
                    "session_id": session_id,
                    "error": f"Unknown command: {cmd}"
                })
        
        except Exception as e:
            self.logger.exception(f"Command '{cmd}' failed")
            self.queue_out.put({
                "ok": False,
                "cmd": cmd,
                "session_id": session_id,
                "error": str(e)
            })
    
    # =========================================================================
    # NETWORK RECEIVER THREAD - incoming connections and client handling
    # =========================================================================
    
    def _network_receiver_thread(self):
        """
        Accept TCP connections and handle one client at a time.

        Loop:
        1) Create/bind/listen socket.
        2) Accept a connection (timeout allows periodic checks of ``_running``).
        3) Handle the client session until it disconnects.
        4) Return to accept to wait for a new connection.

        The server supports a single concurrent client connection.
        """
        # Create server socket
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM) # TCP
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) # Reuse address
        self._server_socket.bind((self.host, self.port)) # Bind to address

        # Single-client design:
        # we accept and handle at most one client connection at a time. New connections
        # are only accepted after the current session ends (client disconnects or errors),
        # which keeps the threading model simple and avoids shared-state locks for multiple clients.
        self._server_socket.listen(1) 
        # accept() timeout is intentional:
        # it lets the accept loop periodically wake up to check self._running, enabling a
        # responsive shutdown without relying on external interrupts.
        self._server_socket.settimeout(1.0)

        self.logger.info(f"Listening on {self.host}:{self.port}")

        # Accept loop:
        # blocks on accept() waiting for a client; when a client connects we delegate the
        # entire session lifecycle to _handle_client() (handshake -> sender thread -> receiver loop -> teardown),
        # then return here to wait for the next connection.
        while self._running:
            try:
                try:
                    client_socket, addr = self._server_socket.accept() 
                except socket.timeout:
                    # Normal timeout, just check _running and continue
                    continue
                except OSError:
                    if self._running:
                        self.logger.error(f"Accept failed")
                        continue

                # Client connected: set up socket in keepalive mode
                # Keepalive: server is not shut down by network errors
                # avoid network error from crashing the server
                self.logger.info(f"Client connected from {addr}")
                client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                try:
                    # From this point the program enters "session mode": _handle_client() will not return until
                    # the client disconnects or a fatal session error occurs. This enforces the
                    # single-active-client invariant.
                    self._handle_client(client_socket)
                except Exception as e:
                    self.logger.error(f"Critical error handling client: {e}")
            except Exception as main_e:
                # Not directly exit the while loop, try to continue.
                # accept new connections without killing the server instance.
                # This is in general required to avoid overlay reset and so RF-DC phase-coherence loss.
                self.logger.critical(f"Unexpected error in network loop: {main_e}")
                
        
        # Final cleanup: esplicit closure request
        try:
            self._server_socket.close()
        except (OSError, Exception):
            pass
        
        self.logger.info("Network thread exited")

    def _handle_client(self, client_socket: socket.socket):
        """Handle a single client session.

        Steps:
        1) Configure socket mode and perform handshake authentication.
        2) Start the sender thread.
        3) Run the receiver loop (blocking) in the current thread.
        4) Cleanup and return to accept loop.
        """
        self._client_socket = client_socket
        self._authenticated = False

        # Configure the socket for an "Abortive Close" (hard reset).
        # Setting l_onoff=1 and l_linger=0 forces the TCP stack to send a RST (Reset)
        # segment immediately upon closing, rather than the standard FIN/ACK handshake.
        # This configuration achieves two engineering goals:
        # 1. Immediate Discard: Any unsent data in the kernel's transmit buffer is discarded.
        # 2. Resource Reclamation: Skips the TIME_WAIT state, freeing the port immediately.
        l_onoff = 1
        l_linger = 0  # 0 second waiting time
        client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, 
                                 struct.pack('ii', l_onoff, l_linger))
        
        # Set the socket to blocking mode (synchronous I/O).
        # This ensures the receiver loop waits for data arrival without consuming CPU cycles
        # in a busy-wait loop, delegating thread suspension to the OS scheduler
        client_socket.settimeout(None)  # Blocking mode

        try:
            # 1. Handshake
            # Handshake with a timeout: a silent client must not stall the network thread.
            # If authentication does not complete quickly, the connection is dropped.
            client_socket.settimeout(3.0)  # Handshake timeout
            if not self._do_handshake(client_socket):
                self.logger.warning("Handshake failed, closing connection")
                return
            client_socket.settimeout(None) # Back to blocking mode once authenticated

            # 2. Start sender thread
            # the receiver loop will block on recv(), so the responses are decoupled:
            # (queue_out -> socket) to avoid deadlocks and enable streaming (e.g., sweep batches).
            sender = Thread(target=self._sender_loop, daemon=True)
            sender.start()
            
            # 3. Receiver loop (runs in this thread)
            # blocks on socket reads, parses messages, and forwards commands to the main thread
            # via queue_in. Hardware execution never happens here.
            self._receiver_loop(client_socket)
            
            # 4. Cleanup
            # Gracefully stop the sender:
            # - None is a flag "end of session" for the sender loop
            # - join is best-effort (timeout) to avoid hanging during "teardown"
            self.queue_out.put(None)  # Signal sender to stop
            sender.join(timeout=2.0)
            
        except Exception as e:
            self.logger.error(f"Client handler error: {e}")
        
        finally:

            # Session "teardown":
            # ensure any in-flight sweep is cancelled and no stale messages leak into the next session.
            # The stop_event is set first so that long-running operations can terminate cooperatively.

            self._stop_event.set()
            with self.queue_in.mutex: self.queue_in.queue.clear()
            with self.queue_out.mutex: self.queue_out.queue.clear()
            
            # Close socket
            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass

            try:
                client_socket.close()
            except:
                pass
            
            # Reset per-session state so the next accept() starts from a clean slate.
            # (Sender/receiver loops reference these fields, so we clear them only at the end.)
            self._client_socket = None
            self._authenticated = False
            self.logger.info("Client disconnected, ready for new connection")

    def _receiver_loop(self, sock: socket.socket):
        """
        Receive client messages and enqueue commands for execution.

        Notes
        -----
        The ``abort`` command is handled immediately by setting ``_stop_event`` and
        sending an acknowledgement, bypassing the main execution queue.
        """
        while self._running:
            try:
                msg = self._receive_message(sock)
                
                # msg == None means the socket was closed (peer disconnect or read failure).
                # The receiver loop must exit to trigger session "teardown" in _handle_client().
                if msg is None:
                    self.logger.info("Client connection closed")
                    break
                
                cmd = msg.get("cmd", "")
                
                # Abort bypasses queue for immediate effect
                if cmd == "abort":
                    self.logger.info("Abort command received")

                    # Abort must be immediate:
                    # queue_in (main thread scheduling) is bypassed and stop_event is set here
                    # for cooperative cancellation of an ongoing sweep.
                    self._stop_event.set()
                    self._abort_in_progress.set()

                    # Asynchronous acknowledgement:
                    # an abort" response is enqueued so the sender thread can notify the client
                    # even while the main thread may still be unwinding the sweep.
                    with self.queue_out.mutex:
                        self.queue_out.queue.clear()
                    self.queue_out.put({
                        "ok": True,
                        "cmd": "abort",
                        "session_id": msg.get("session_id", ""),
                        "message": "Sweep aborted"
                    })
                else:
                    if cmd == "upload_envelopes":
                        invalid_metadata = False
                        total_envelopes = 0

                        for gen_index_str, envelopes in msg.get("envelopes", {}).items():
                            for e in envelopes:
                                total_envelopes += 1
                                if "samples_iq" in e or "num_samples" not in e:
                                    invalid_metadata = True

                        if (not invalid_metadata) and total_envelopes > 0:
                            envelope_data = self._recv_envelope_frames(sock, total_envelopes)
                            self.logger.info(f"Received {total_envelopes} binary envelope frames")
                            msg["envelope_data"] = envelope_data

                    # All non-abort commands go through the main thread.
                    # This preserves the single-writer invariant for handler/hardware operations.
                    self.queue_in.put(msg)
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                self.logger.warning("Connection lost inside receiver loop")
            except Exception as e:
                self.logger.error(f"Unexpected error in receiver: {e}")
                break

    def _sender_loop(self):
        """
        Send responses from ``queue_out`` to the connected client.

        Uses binary transmission mode for all data acquisitions.
        Exits when it receives ``None`` or when the client disconnects.
        """
        while self._running:
            try:
                msg = self.queue_out.get(timeout=1.0)
            except Empty:
                continue

            if msg is None:
                # None is the session "flag": it tells the sender loop to exit cleanly
                # when the client session ends.
                break

            try:
                # Socket writer thread:
                # this is the only place that writes to the client socket.
                # To write in a single thread avoids interleaving frames and simplifies error handling.
                msg_type = msg.get("type")

                # Handle binary transmission types
                if self._abort_in_progress.is_set() and msg.get("cmd") != "abort":
                    # Drop any pending data frames while abort is active.
                    continue

                if msg_type == "metadata_with_binary":
                    # Single experiment result: metadata + binary frames
                    self._send_message(self._client_socket, msg["metadata"])

                    # Send binary frames for each ADC
                    for adc_idx, arr in msg["binary_data"].items():
                        self._send_binary_frame(self._client_socket, adc_idx, arr)

                elif msg_type == "sweep_header":
                    # Single sweep header (metadata only)
                    self._send_message(self._client_socket, msg["metadata"], include_timing=True)

                elif msg_type == "sweep_binary_batch":
                    # Sweep data-only batch: binary frames only
                    for point_data in msg["binary_data"]:
                        for adc_idx, arr in point_data.items():
                            self._send_binary_frame(self._client_socket, adc_idx, arr)
                        timing = msg.get("timing", [])
                        if timing:
                            hw_ms, sw_ms = timing.pop(0)
                        else:
                            hw_ms, sw_ms = 0.0, 0.0
                        self._send_timing_trailer(self._client_socket, hw_ms, sw_ms)

                else:
                    # Other messages: handshake, status, errors (pure JSON)
                    include_timing = msg_type in ("sweep_status",)
                    self._send_message(self._client_socket, msg, include_timing=include_timing)
                    if msg.get("cmd") == "abort":
                        self._abort_in_progress.clear()

            except (BrokenPipeError, ConnectionResetError, OSError):
                self.logger.error("Client disconnected during send. Aborting experiment.")
                # If sending fails, assume the client is gone.
                # We set stop_event to abort any active sweep and exit;
                # "teardown" will close sockets.
                self._stop_event.set()
                break
            except Exception as e:
                self.logger.error(f"Send error: {e}")
                break

        self.logger.debug("Sender loop exited")
        
    # =========================================================================
    # HANDSHAKE & AUTHENTICATION
    # =========================================================================
    
    def _build_handshake_info(self) -> dict:
        """Build the server -> client handshake message."""
        return {
                "type": "handshake",
                "protocol_version": "2.3", # 2.0: binary acquisitions, 2.1: + binary envelopes, 2.2: sweep header stream, 2.3: timing trailer
                "hw_summary": self.handler.status_h.hw_summary,
            }

    def _do_handshake(self, sock: socket.socket) -> bool:
        """
        Perform handshake authentication with the client.

        Flow
        ----
        1) Server sends handshake info (hardware summary).
        2) Client responds with ``type="handshake_ack"`` and a token.
        3) Server validates the token.
        4) Server replies with either ``handshake_ok`` or ``handshake_error``.

        Returns
        -------
        bool
            True if authenticated successfully, False otherwise.
        """

        # Authentication model (minimal):
        # - Shared token sent by the client during handshake.
        # - If authentication fails, we reject immediately and keep the server "stateless"
        #   with respect to untrusted clients (no session state, no handler calls).
        #
        # Timeout policy:
        # - The handshake must complete quickly; a slow/silent client must not stall the
        #   accept/receiver thread. The timeout is enforced by _handle_client() via
        #   socket.settimeout(...) before calling this method.
        #
        # Threading:
        # - Handshake is performed in the network thread before starting sender/receiver loops.
        #   This prevents unauthenticated clients from enqueueing commands into queue_in.

        try:
            handshake_msg = self._build_handshake_info()

            # Step 1: Server -> Client handshake message.
            # Includes hardware summary so the client can validate compatibility / configuration
            # before issuing commands.
            self._send_message(sock, handshake_msg)
            self.logger.info("Handshake sent, waiting for client response...")
            
            # Step 2: Client -> Server handshake acknowledgement.
            # If the client disconnects or sends malformed data, we fail fast and close the session.
            response = self._receive_message(sock)
            if response is None:
                self.logger.warning("Client disconnected during handshake")
                return False
            
            # Strict message type check:
            # handshake is a small state machine; unexpected message types indicate either a
            # client bug or protocol mismatch: abort it early.
            if response.get("type") != "handshake_ack":
                self.logger.warning(f"Invalid handshake response: {response.get('type')}")
                return False
            
            # Step 3: verify token
            client_token = response.get("token", "")
            client_name = response.get("client_name", "unknown")
            
            if client_token != self.auth_token:
                self.logger.warning(f"Authentication failed for client '{client_name}'")
                self._send_message(sock, {
                    "type": "handshake_error",
                    "error": "Invalid token"
                })
                return False
            
            # 4: Authentication succeeded:
            # mark the session as authenticated and explicitly confirm to the client.
            # After this point, the receiver loop may start forwarding commands to queue_in.

            self._authenticated = True
            self._send_message(sock, {
                "type": "handshake_ok",
                "message": f"Welcome {client_name}"
            })
            self.logger.info(f"Client '{client_name}' authenticated successfully")
            return True
            
        except Exception as e:
            # handshake failures are treated as non-fatal for the server process; 
            # reject the current session and return to the accept loop.
            self.logger.error(f"Handshake failed: {e}")
            return False
    
    def _handle_logout(self):
        """
        Reset server-side caches and notify the client.

        This uses the handler reset subsystem to clear envelope/wave caches for
        all generators (preserving nothing by default) and then enqueues a logout
        response.
        """
        self.logger.info("Logout requested, resetting caches...")
        
        # Logout semantics:
        # this resets server-side caches (waves/envelopes) so the next session starts from
        # a clean state. Note that logout does not necessarily close the TCP connection;
        # the client may choose to disconnect after receiving this acknowledgement.
        try:
            # Reset is performed via the handler reset subsystem.
            # This call mutates server-side state and therefore must be executed by the main thread
            # (in the current architecture, _process_message runs on the main thread).
            results = self.handler.reset_h.reset_all_generators(preserve_wave_specs=False)
        
            # Log partial failures but still return an overall logout response.
            # The client can decide whether to treat warnings as fatal based on its policy. (for future developements)
            for r in results:
                if not r["waves"]["ok"]:
                    self.logger.warning(f"Wave reset failed for gen {r['gen_index']}")
                if not r["envelopes"]["ok"]:
                    self.logger.warning(f"Envelope reset failed for gen {r['gen_index']}")
            
            # Enqueue response for the sender thread (do not write to socket here).
            # Keeping all socket writes in the sender thread avoids interleaving and simplifies the "teardown" step.
            self.queue_out.put({
                "ok": True,
                "cmd": "logout", 
                "message": f"Logout successful, {len(results)} generator(s) reset"
            })
        except Exception as e:
            self.logger.exception("Logout failed")
            self.queue_out.put({
                "ok": False,
                "cmd": "logout",
                "error": str(e)
            })
    
    # =========================================================================
    # PROTOCOL - Length-prefixed JSON
    # =========================================================================
    
    def _receive_message(self, sock: socket.socket) -> Optional[dict]:
        """
        Receive one length-prefixed JSON message.
        Returning None indicates either a clean disconnect (EOF) or a protocol-level error.
        
        Parameters
        ----------
        sock:
            Connected socket.

        Returns
        -------
        dict or None
            Parsed JSON dictionary, or None if the client disconnects or an error
            occurs.

        Notes
        -----
        A hard cap is applied to protect against unexpectedly large payloads.
        """
        try:
            # TCP is a byte stream (no message boundaries).Thus protocol messages are framed as:
            # [4-byte big-endian length][UTF-8 JSON payload]
            # Therefore, the receiver knows exactly how many bytes to read for one complete message.

            length_bytes = self._recv_exact(sock, 4)
            # Length is trusted only within reasonable bounds.
            # Avoid huge allocation requests.
            if not length_bytes:
                return None
            length = int.from_bytes(length_bytes, 'big')
            
            # Hard payload cap (safety):
            # protects against unexpectedly large frames (buggy client/DoS attempt).
            # Current protocol does not require large messages; heavy payloads should be sent
            # via dedicated mechanisms, not inside a single JSON frame.
            if length > MAX_PAYLOAD_BYTES:
                self.logger.error(f"Payload too large: {length} bytes")
                return None
            
            # Read the full payload. _recv_exact loops until all bytes are received or the
            # connection closes.
            payload = self._recv_exact(sock, length)
            if not payload:
                return None
            return json.loads(payload.decode('utf-8'))
        
        except json.JSONDecodeError:
            # Malformed JSON is treated as a protocol error. Return None to signal failure
            # Session logic disconnect/action are left to the server-side.
            self.logger.error("Malformed JSON received")
            return None
        except Exception as e:
            self.logger.error(f"Receive error: {e}")
            return None
        
    def _send_message(self, sock: socket.socket, msg: dict, include_timing: bool = False):
        """Send one length-prefixed JSON message."""
        payload = json.dumps(msg).encode('utf-8')
        if include_timing and isinstance(msg, dict):
            debug_timing = msg.get("debug_timing")
            if debug_timing is None:
                debug_timing = {}
                msg["debug_timing"] = debug_timing

            t0 = time.perf_counter()
            payload = json.dumps(msg).encode('utf-8')
            t1 = time.perf_counter()
            debug_timing["server_encode_ms"] = (t1 - t0) * 1000.0
            debug_timing["payload_bytes"] = len(payload)

        length = len(payload).to_bytes(4, 'big')

        # sendall() function: ensures the full frame is transmitted (prefix + payload).
        # Writes are performed by the sender thread only: avoid interleaved frames.
        sock.sendall(length + payload)

    def _send_binary_frame(self, sock: socket.socket, adc_index: int, data: np.ndarray):
        """
        Send a single binary data frame for one ADC.

        Frame format:
        [4 bytes: ADC index (uint32 big-endian)]
        [4 bytes: data length (uint32 big-endian)]
        [N bytes: raw numpy data via .tobytes()]

        :param sock: Connected socket
        :param adc_index: ADC/acquisition index
        :param data: Complex numpy array to transmit
        """
        # Serialize numpy array to bytes
        data_bytes = data.tobytes()

        # Construct frame header: ADC index + data length
        header = struct.pack('>II', adc_index, len(data_bytes))

        # Send atomically
        sock.sendall(header + data_bytes)

        self.logger.debug(
            f"Sent binary frame: ADC {adc_index}, {len(data_bytes)} bytes, "
            f"dtype={data.dtype}, shape={data.shape}"
        )

    def _send_timing_trailer(self, sock: socket.socket, hw_ms: float, sw_ms: float):
        """Send a fixed-size timing trailer (2x float32, big-endian)."""
        payload = struct.pack('>ff', float(hw_ms), float(sw_ms))
        sock.sendall(payload)

    def _recv_envelope_frames(self, sock: socket.socket, total_count: int) -> Dict[Tuple[int, int], np.ndarray]:
        """
        Receive binary envelope sample frames.

        Frame format per envelope:
        [4 bytes: generator index (uint32 big-endian)]
        [4 bytes: envelope index in batch (uint32 big-endian)]
        [4 bytes: sample count (uint32 big-endian)]
        [N × 8 bytes: float32 I/Q pairs]

        :param sock: Connected socket
        :param total_count: Total number of envelopes to receive
        :return: Dict mapping (gen_idx, env_idx) to float32 I/Q array (shape: N×2)
        """
        envelope_data = {}

        for _ in range(total_count):
            # Read 12-byte header
            header = self._recv_exact(sock, 12)
            if header is None:
                raise ConnectionError("Incomplete envelope frame header")

            gen_idx, env_idx, num_samples = struct.unpack('>III', header)

            # Read sample data (num_samples × 8 bytes for float32 I/Q)
            sample_bytes = self._recv_exact(sock, num_samples * 8)
            if sample_bytes is None:
                raise ConnectionError(f"Incomplete envelope samples for gen={gen_idx}, env={env_idx}")

            # Convert to numpy array: shape (num_samples, 2) with dtype float32
            samples = np.frombuffer(sample_bytes, dtype=np.float32).reshape(num_samples, 2)
            envelope_data[(gen_idx, env_idx)] = samples

            self.logger.debug(
                f"Received envelope frame: gen={gen_idx}, env={env_idx}, "
                f"{num_samples} samples, {len(sample_bytes)} bytes"
            )

        return envelope_data

    def _recv_exact(self, sock: socket.socket, n: int) -> Optional[bytes]:
        """
        Receive exactly *n* bytes from the socket.

        Returns None if the connection is closed before the requested number of
        bytes is received.
        """
        data = b''

        # recv() may return fewer bytes than requested even on blocking sockets.
        # Loop until have exactly n bytes/closed connection is detected.
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data
