# FIREQ_SERVER.network

Network layer of the FIREQ server: framing, packet dataclasses, and the two
worker threads that own the client socket. This subpackage implements the
wire protocol between the server and its client and is the only place that
touches sockets.

## Architecture

```text
FIREQ_SERVER/network/
├── protocol.py         - FIREQNetworkPacket, NetworkDMAPayload, helpers
├── receive_worker.py   - ReceiveWorker: owns sock.recv(), fills queue_in
├── send_worker.py      - SendWorker: owns sock.sendmsg(), drains queue_out
└── __init__.py         - public exports
```

## Files

| File | Public API | Responsibility |
|---|---|---|
| `protocol.py` | `FIREQNetworkPacket`, `NetworkDMAPayload`, `unpack_header`, `get_command`, `get_sweep_variables` | Wire format: a msgpack-packed header dictionary preceded by a 4-byte big-endian length, with an optional binary payload whose size is announced in the header (`tsize`). Also `NetworkDMAPayload`, the network-serializable DMA payload used during experiments. |
| `receive_worker.py` | `ReceiveWorker` | Background thread that waits for the `client_connected` event, then reads framed messages from the client socket and puts them in `queue_in` (a bounded `Queue`). Clears the `client_connected` event on connection loss. |
| `send_worker.py` | `SendWorker` | Background thread that pops objects from `queue_out` (a `MemoryBoundedQueue`) and sends them with `socket.sendmsg()`, handling partial sends. Any item with a `to_buffers()` method is accepted. |

## Wire protocol

Each message has two parts:

- **HEADER** — a dictionary serialized with msgpack (`raw=False`). The header
  must always be present; it carries the command (`cmd`) or response type
  (`type`).
- **DATA** (optional) — raw bytes following the header. Its size in bytes is
  announced by the header under the `tsize` key.

The whole packet is: `4-byte big-endian header length` + `header bytes` +
(optional) `data bytes`.

Limits on incoming packets (`MAX_PAYLOAD_BYTES`, 10 MB):

- A header length or a `tsize` above the limit closes the connection, because
  the stream can no longer be realigned.
- A correctly framed header that is not a msgpack dict is logged and dropped,
  and the connection stays open.

Response header types include `status`, `warning`, `error`, `handshake` and
`dma_package`. See *Experiment protocol* below for the `status` messages that
bracket an experiment run.

### Serialization

```python
packet = FIREQNetworkPacket({"type": "status", "msg": "experiment_header"})
buffers = packet.to_buffers()      # (4-byte length, header bytes[, data])
```

`NetworkDMAPayload` (subclass of `FIREQ_SYSTEM.dma_node.DMAPayload`) is the
DMA payload injected into the system node by `FIREQServer`. Its `to_buffers()`
emits a `dma_package` header with `source`, `shots`, `format` (dtype spec) and
`tsize`, followed by the raw acquisition bytes — the buffer list is ready for
`sendmsg()`.

### Experiment protocol

Every `config_and_run` — single experiment or sweep — produces the same
sequence of packets:

- `{"type": "status", "msg": "ok"}` — the configuration was applied.
- `{"type": "status", "msg": "experiment_header", ...}` — it carries no shot
  count, because a swept `$shots` changes per point: each `dma_package` reports
  the shots it holds. Sweeps add `variable_order` (variables ordered from the outermost to
  the innermost loop) and `variable_values` (mapping of variable name to a
  plain list of values, converted from NumPy arrays so that the header stays
  msgpack-encodable).
- zero or more `dma_package` messages per iteration.
- `{"type": "status", "msg": "iteration_ended", "time": <ns>}` — one per
  executed point; `time` is an integer number of nanoseconds.
- `{"type": "status", "msg": "experiment_footer", "run_time": <ns>, "aborted": <bool>}`:
  - Always closes an experiment whose header was sent, for single runs and
    sweeps alike.
  - `run_time` is the total execution time in integer nanoseconds.
  - `aborted` is `true` when the run failed (an `error` packet precedes the
    footer), or when a sweep was stopped because the server stopped or the
    client disconnected.

## Worker threads

Both workers are daemon threads started by `FIREQServer`:

- They wait on the shared `client_connected` event before touching the socket;
  the main thread sets the event only after assigning the accepted socket.
- `ReceiveWorker._recv_exact(sock, n)` loops on `recv()` until `n` bytes are
  collected, raising `ClientDisconnectedError` on connection loss or
  `IncompleteTransferError` if the transfer ends early (e.g. worker stopped).
- `SendWorker._sendmsg_all(sock, buffers)` advances the buffers by the bytes
  actually sent, retries on `TimeoutError`, and converts connection failures
  into `ClientDisconnectedError`.
- Both workers clear `client_connected` when the connection is lost, which
  makes the main loop exit and go back to accepting a new client.
- Each worker captures the socket it is using. An error on a socket that the
  server has already replaced is ignored, so it cannot disconnect the next
  client.
- An unexpected error in the receive worker drops the client but keeps the
  thread alive for the next connection.

`stop()` joins the threads with a 2 s timeout. `clear_input_queue()` /
`clear_output_queue()` empty the queues when a client disconnects.

## Usage from a client

A minimal client loop:

```python
import msgpack, struct, socket

def send(sock, header, data=None):
    if data is not None:
        header["tsize"] = len(data)
    hdr = msgpack.packb(header)
    sock.sendall(struct.pack(">I", len(hdr)) + hdr + (data or b""))

sock = socket.create_connection(("host", 5000))
send(sock, {"type": "handshake_ack", "token": "fireq", "client_name": "lab"})
send(sock, {"cmd": "ping"})
```

## Related documentation

- [`../README.md`](../README.md) — server architecture and command table.
- [`../utils/README.md`](../utils/README.md) — `MemoryBoundedQueue` used for
  `queue_out` and the transport exceptions raised by the workers.
- [`../../FIREQ_SYSTEM/README.md`](../../FIREQ_SYSTEM/README.md) — where DMA
  payloads are produced.
