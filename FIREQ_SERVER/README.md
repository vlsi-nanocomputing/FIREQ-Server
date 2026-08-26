# FIREQ Server

Top-level server package: single-client TCP transport, command execution,
experiment/sweep orchestration, and utilities.

## Architecture

```text
FIREQ_SERVER/
├── fireq_server.py    - FIREQServer: main thread, connection handling, command dispatch
├── network/           - msgpack framing, receive/send worker threads
├── execution/         - sweep experiment orchestration
├── utils/             - memory-bounded queue and exceptions
└── __init__.py        - public exports
```

## Files

| File / Folder | Public API | Responsibility |
|---|---|---|
| `fireq_server.py` | `FIREQServer` | Owns the FIREQ system node and the network workers; accepts a client, performs the token handshake, and executes commands on the main thread. |
| [`network/`](network/README.md) | `ReceiveWorker`, `SendWorker`, `FIREQNetworkPacket`, `NetworkDMAPayload`, `get_command`, `get_sweep_variables` | TCP framing, worker threads, and packet serialization. |
| [`execution/`](execution/README.md) | `SweepExperiment` | Parses sweep callbacks/variables and executes multi-point experiments. |
| `utils/` | `MemoryBoundedQueue`, `FireqHardwareError`, `ClientDisconnectedError`, ... | Thread-safe memory-bounded queue and typed exception hierarchy. |
| `__init__.py` | `FIREQServer` | Package public API re-export. |

## Runtime flow

1. `FIREQServer.__init__(overlay_file, host, port, auth_token, logger)` creates
   a `FIREQSystemNode`, sets the DMA payload interface class to
   `NetworkDMAPayload`, and constructs `ReceiveWorker`/`SendWorker`.
2. `start()` launches the two worker threads and enters the main loop.
3. The main thread accepts a client, performs the handshake, then processes
   queued commands.
4. Responses and streamed DMA data are placed in the output queue and sent by
   `SendWorker`.

## Commands

| Command | Behaviour |
|---|---|
| `ping` | Replies `{"resp": "pong"}`. |
| `apply_configuration` | Applies `message["system"]` to the `FIREQSystemNode`. |
| `config_and_run` | Applies the system configuration and runs a single experiment or a sweep. |
| `reset_all` | Resets the system. |
| `logout` | Closes the client connection. |

## Handshake

1. Server sends `{"type": "handshake"}`.
2. Client replies with
   `{"type": "handshake_ack", "token": "<token>", "client_name": "<name>"}`.
3. Server validates the token. On failure (or timeout) the connection is closed.

## Sweep execution

When `config_and_run` finds sweepable callbacks and `variables` are present, it
creates `SweepExperiment` and calls `run(callbacks, variables)`. The sweep emits
a `sweep_experiment_header` before the points, runs each point as a regular
experiment, and finally emits a `sweep ended` status message.

See [`execution/README.md`](execution/README.md) for the sweep algorithm.

## Exceptions

`FIREQ_SERVER.utils.exceptions` defines a typed hierarchy with
`FireqHardwareError` as the base class, plus transport errors such as
`ClientDisconnectedError`, `IncompleteTransferError`, and
`InvalidPayloadError`. These are available for use across the server stack.

## Related documentation

- [`network/README.md`](network/README.md)
- [`execution/README.md`](execution/README.md)
