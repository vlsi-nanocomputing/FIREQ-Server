# Network Package

TCP server and inter-thread communication for FIREQ.

## Architecture

```text
network/
├── fireq_server.py   - single-client TCP server (main thread + receiver + sender)
├── memory_queue.py   - memory-bounded thread-safe queue
└── __init__.py       - re-exports FIREQServer
```

Three execution contexts communicate via `queue_in` / `queue_out`:

- Main thread: executes commands and interfaces hardware/execution components
- Receiver thread: accepts a client connection, parses framed JSON messages, and enqueues commands
- Sender thread: writes JSON responses and streamed binary data back to the client

## Files

| File | Public API | Responsibility |
|---|---|---|
| `fireq_server.py` | `FIREQServer` | Single-client TCP server handling connection lifecycle, command routing, authentication, and streamed binary responses. Uses a 4-byte big-endian length prefix for JSON messages and coordinates the execution layer through queues. |
| `memory_queue.py` | `MemoryBoundedQueue` | Thread-safe queue bounded by memory usage rather than item count, designed for streaming FPGA data where chunk sizes vary significantly. Internal to the package. |
| `__init__.py` | `FIREQServer` | Re-exports the network package public API. |
