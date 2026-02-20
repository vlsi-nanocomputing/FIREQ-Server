# Network Package

TCP server and inter-thread communication for FIREQ.

## Architecture

```
network/
 ├── fireq_server.py   – single-client TCP server (3-thread model)
 └── memory_queue.py   – memory-bounded thread-safe queue
```

Three threads communicate via `queue_in` / `queue_out`:
- **Main thread** — executes commands, interfaces hardware
- **Receiver thread** — accepts connections, parses JSON messages
- **Sender thread** — writes responses and streamed binary data to client

## Files

| File | Public API | Responsibility |
|---|---|---|
| `fireq_server.py` | `FIREQServer` | Single-client TCP server handling connection lifecycle, command routing, authentication, and streamed binary responses. Protocol: 4-byte big-endian length prefix + UTF-8 JSON. |
| `memory_queue.py` | `MemoryBoundedQueue` | Thread-safe queue bounded by memory usage (not item count), designed for streaming FPGA data where chunk sizes vary. Internal to the package. |
| `__init__.py` | — | Re-exports `FIREQServer`. |
