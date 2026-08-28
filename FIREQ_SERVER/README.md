# FIREQ_SERVER

Top-level server package: single-client TCP transport, command execution,
experiment/sweep orchestration, and shared utilities. `FIREQServer` owns a
`FIREQSystemNode` (from `FIREQ_SYSTEM`), accepts one TCP client at a time,
authenticates it with a token handshake, and executes commands on the main
thread while dedicated threads receive and send network traffic.

## Architecture

```text
FIREQ_SERVER/
├── fireq_server.py      - FIREQServer: main thread, connection handling, command dispatch
├── network/             - msgpack framing, packet dataclasses, receive/send worker threads
├── execution/           - SweepExperiment: multi-point sweep orchestration
├── utils/               - memory-bounded queue and typed exception hierarchy
└── __init__.py          - public exports
```

The server is built around three threads communicating through two queues:

```text
ReceiveWorker ── queue_in  ──► main thread (FIREQServer)
SendWorker     ◄─ queue_out ── main thread (FIREQServer) / DMA payloads
```

## Files

| File / Folder | Public API | Responsibility |
|---|---|---|
| `fireq_server.py` | `FIREQServer` | Loads the FIREQ system node, owns the client socket, performs the handshake and dispatches commands on the main thread. |
| [`network/`](network/README.md) | `FIREQNetworkPacket`, `NetworkDMAPayload`, `ReceiveWorker`, `SendWorker`, `get_command`, `get_sweep_variables` | Framed msgpack protocol, packet serialization, and the receive/send worker threads. |
| [`execution/`](execution/README.md) | `SweepExperiment` | Parses sweepable callbacks and variables and executes multi-point experiments as nested loops. |
| [`utils/`](utils/README.md) | `MemoryBoundedQueue`, `FireqHardwareError`, `ClientDisconnectedError`, ... | Memory-bounded thread-safe queue and the typed exception hierarchy. |
| `__init__.py` | `FIREQServer` | Package public API re-export. |

## Runtime flow

1. `FIREQServer(overlay_file, host, port, auth_token, logger)` creates a
   `FIREQSystemNode` from the overlay, swaps the DMA payload class for
   `NetworkDMAPayload`, and constructs the `ReceiveWorker`/`SendWorker` pair.
2. `start()` launches the two worker threads and enters the main loop on the
   calling thread.
3. The main loop accepts a client, assigns the socket to both workers, and
   performs the token handshake (3 s timeout).
4. While connected, the main thread pops command messages from `queue_in` and
   dispatches them; responses and streamed DMA payloads go into `queue_out`.
5. `stop()` sets the stop event, closes both sockets and joins the workers.

## Commands

| Command | Message fields | Behaviour |
|---|---|---|
| `ping` | `cmd` | Replies `{"resp": "pong"}`. |
| `apply_configuration` | `system` | Applies the nested system configuration to `FIREQSystemNode`. Warns if the configuration contains sweepable parameters. |
| `config_and_run` | `system`, optional `variables` | Applies the configuration; runs a single experiment, or a sweep when sweepable callbacks are present and a `variables` object is given. |
| `reset_all` | — | Resets IP memories/registers and the system node state. |
| `logout` | — | Closes the current client connection. |

Unknown commands get an `{"type": "error", ...}` response; a `system` field
missing from a configuration message or a failed configuration produce an
error response as well.

## Handshake

1. Server sends `{"type": "handshake"}`.
2. Client replies with
   `{"type": "handshake_ack", "token": "<token>", "client_name": "<name>"}`.
3. The server validates the token against `auth_token`; on mismatch, timeout
   or any other failure the connection is closed and the server goes back to
   accepting.

## Sweep execution

When `config_and_run` yields sweepable callbacks and a `variables` object, the
server creates a `SweepExperiment` and runs it. The sweep emits a
`sweep_experiment_header` message before the points, executes each point as a
regular experiment (streaming `dma_package` messages), and finally emits a
`status` message `"sweep ended"` with the execution time in ns.

See [`execution/README.md`](execution/README.md) for the sweep algorithm and
variable specification.

## Configuration errors

Invalid configurations are reported to the client as
`{"type": "error", "msg": "system dict for run experiment command is invalid: ..."}`
and the run is aborted.

## Related documentation

- [`network/README.md`](network/README.md) — framing, packet layout, workers.
- [`execution/README.md`](execution/README.md) — sweeps.
- [`utils/README.md`](utils/README.md) — queue and exceptions.
- [`../FIREQ_SYSTEM/README.md`](../FIREQ_SYSTEM/README.md) — the hardware model
  the server drives.
- [`../README.md`](../README.md) — repository overview and how to run the
  server (`API.py`).
