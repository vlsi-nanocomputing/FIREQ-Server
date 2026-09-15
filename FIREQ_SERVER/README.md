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
| [`execution/`](execution/README.md) | `SweepExperiment` | Parses sweepable callbacks and variables at construction time and executes multi-point experiments as nested loops. |
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
| `trigger_manually` | `ip_name` | Triggers the manual trigger of a child IP. Replies `{"type": "status", "msg": "ok"}`, or an `error` when the IP is unknown or cannot be triggered. |
| `reset_all` | — | Resets IP memories/registers and the system node state. |
| `logout` | — | Closes the current client connection. |

Unknown commands get an `{"type": "error", ...}` response; a `system` field
missing from a configuration message or a failed configuration produce an
error response as well. `trigger_manually` answers with an `error` when the
requested IP is unknown or does not expose a manual trigger.

## Handshake

1. Server sends `{"type": "handshake"}`.
2. Client replies with
   `{"type": "handshake_ack", "token": "<token>", "client_name": "<name>"}`.
3. The server validates the token against `auth_token`; on mismatch, timeout
   or any other failure the connection is closed and the server goes back to
   accepting.

## Experiment protocol

Every `config_and_run` follows the same lifecycle, whether it is a single
experiment or a sweep:

```text
<- {"type": "status", "msg": "ok"}                configuration applied
<- {"type": "status", "msg": "experiment_header"} + "shots" (always present)
                                                  + "variable_order" / "variable_values" (sweeps)
<- {"type": "dma_package", ...}                   zero or more per iteration
<- {"type": "status", "msg": "iteration_ended", "time": "<ns> ns"}
...                                               one iteration per sweep point
<- {"type": "status", "msg": "experiment_footer"} + "sweep_time" (sweeps)
```

`_config_and_run` owns the whole sequence: it applies the configuration, emits
the `experiment_header`, creates and runs the `SweepExperiment` when sweepable
callbacks and a `variables` object are present (otherwise it runs a single
`_run_experiment`), and emits the `experiment_footer`.

The sweep itself never writes to the network. At the innermost loop level it
calls `FIREQServer._run_experiment`, so each point streams its DMA payloads and
its `iteration_ended` status before the next point starts.

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
  server (`start_server.py`).
