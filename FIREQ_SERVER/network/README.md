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
| `send_worker.py` | `SendWorker` | Background thread that pops objects from `queue_out` (a `MemoryBoundedQueue`) and sends them with `socket.sendmsg()`. Any item with a `to_buffers()` method is accepted. |

## Wire protocol

Each message has two parts:

- **HEADER** — a dictionary serialized with msgpack (`raw=False`). The header
  must always be present; it carries the command (`cmd`) or response type
  (`type`).
- **DATA** (optional) — raw bytes following the header. Its size in bytes is
  announced by the header under the `tsize` key.

The whole packet is: `4-byte big-endian header length` + `header bytes` +
(optional) `data bytes`.

Response header types include `status`, `warning`, `error`, `handshake`,
`sweep_experiment_header` and `dma_package`.

### Serialization

```python
packet = FIREQNetworkPacket({"type": "status", "msg": "experiment started"})
buffers = packet.to_buffers()      # (4-byte length, header bytes[, data])
```

`NetworkDMAPayload` (subclass of `FIREQ_SYSTEM.dma_node.DMAPayload`) is the
DMA payload injected into the system node by `FIREQServer`. Its `to_buffers()`
emits a `dma_package` header with `source`, `shots`, `format` (dtype spec) and
`tsize`, followed by the raw acquisition bytes — the buffer list is ready for
`sendmsg()`.

## Worker threads

Both workers are daemon threads started by `FIREQServer`:

- They wait on the shared `client_connected` event before touching the socket;
  the main thread sets the event only after assigning the accepted socket.
- `ReceiveWorker._recv_exact(n)` loops on `recv()` until `n` bytes are
  collected, raising `ClientDisconnectedError` on connection loss or
  `IncompleteTransferError` if the transfer ends early (e.g. worker stopped).
- `SendWorker._send_payload(item)` retries on `TimeoutError`, and converts
  connection failures into `ClientDisconnectedError`.
- Both workers clear `client_connected` when the connection is lost, which
  makes the main loop exit and go back to accepting a new client.

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
