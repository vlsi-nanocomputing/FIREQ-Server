# Changelog

All notable changes to FIREQ-Server are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-10-22

### Added

- `trigger_manually` server command (`ip_name` field) to fire a child IP's
  `manual_trigger()`, answering `{"type": "status", "msg": "ok"}` or an `error`.
- `mts_sync` server command and `FIREQSystemNode.synchronize_dac_tiles()`: multi-tile
  synchronization of the active DAC tiles.
- `set_nyquist` server command (`tile`, `block_id`, `zone`) and
  `FIREQSystemNode.set_nyquist_zone()`: sets the Nyquist zone of a DAC or ADC block,
  with validation (the low-level driver ignores unknown blocks silently).
- `FIREQSystemNode.trigger_ip_manually(ip_name)`, replacing `trigger_manually()`.
  It works on any child IP and raises `ValueError` when the IP does not expose a
  manual trigger.
- `{"type": "status", "msg": "ok"}` acknowledgement once a configuration has been
  applied.
- Explicit experiment framing: sweeps now report their variable order and
  variable values up front in the `experiment_header`. Every run
  reports its total time (`run_time`) and an `aborted` flag at the end, in the
  `experiment_footer`.
- `SweepExperiment` takes an optional `should_abort` callable. The server uses
  it to stop a sweep when it is stopping or when the client disconnects;
  `SweepAbortedError` is raised in that case.
- Incoming packets are limited to 10 MB (`MAX_PAYLOAD_BYTES`) for both the
  header and the trailing data. An oversized size closes the connection.
- `DMANode.reset_transfer_state()`, also called by `reset_all`.
- `docs/` protocol documentation for the `config_and_run` lifecycle.

### Changed

- **Breaking:** the experiment protocol is now uniform across all runs —
  `experiment_header` → `dma_package`(s) + `iteration_ended` → `experiment_footer`.
  The `sweep_experiment_header` packet and the `"experiment started"` /
  `"experiment ended"` status strings were removed.
- **Breaking:** `SweepExperiment` parses its callbacks and variables in the
  constructor (`SweepExperiment(server, sweep_callbacks, variables)`); `run()`
  takes no arguments and no longer emits anything on the network.
- **Breaking:** the `const` variable mode was removed — use
  `{"mode": "list", "values": [x]}` to sweep a single value.
- **Breaking:** `FIREQSystemNode.trigger_manually()` was renamed to
  `trigger_ip_manually()`.
- **Breaking:** `iteration_ended` carries `time` as an integer number of
  nanoseconds instead of the string `"<ns> ns"`.
- **Breaking:** the `experiment_footer` always carries `run_time` (int ns) and
  `aborted` (bool). They replace the sweep-only `sweep_time`.
- **Breaking:** once an `experiment_header` has been sent, an
  `experiment_footer` always follows. A failed run sends an `error` packet and
  then a footer with `"aborted": true`. A hardware error now stops a sweep
  instead of continuing with the next point.
- **Breaking:** errors detected before a run are reported as `error` packets
  instead of `warning` packets: sweepable parameters without `variables`, and
  an invalid sweep definition. In both cases no header is sent. A failed
  configuration sends only its `error` packet; the extra warning was removed.
- An unexpected exception while executing any command is now reported to the
  client as an `error` packet. Previously it was only logged.
- `apply_configuration` with sweepable parameters now also sends a `warning`
  packet after the `ok` status.
- The handshake must be answered within 3 s (`HANDSHAKE_TIMEOUT_SECONDS`). A
  silent client used to block the server forever.
- The listening socket is bound once and reused for every client connection.
- Sweep expressions are validated by parsing instead of a character whitelist. Decimal and
  scientific-notation numbers are now allowed (`#x + 0.5`, `#1e9 - x`). Only numbers,
  variables, `+`, `-` and parentheses are accepted; calls, attribute access and other
  operators are rejected with a clear error.
- `$shots` is cast to int (truncated), so floats such as the values of a `lin` sweep
  are accepted instead of crashing the DMA transfer. A value below 1 after the cast is
  rejected.
- The overlay base path is resolved relative to `start_server.py` instead of a
  hardcoded `/home/xilinx/`, so the server runs from any install location.

### Fixed

- Sweeps crashed the sender thread: the `experiment_header` carried NumPy arrays,
  which cannot be msgpack-encoded. Variable values are now converted to lists.
- `AttributeError` when closing the client before any connection was accepted
  (`_server_socket` and `_client_socket` are now initialised to `None`).
- Duplicate, inconsistent sweep headers emitted by both `SweepExperiment` and the
  server; the header is now emitted once, by the server.
- Type-hint typo in `SweepExperiment._compute_variable_values`.
- Packets queued for a disconnected client were delivered to the next client.
  The client socket is now closed and both queues are cleared whenever a client
  session ends.
- A late socket error from a worker on an already closed client could
  disconnect the next client.
- `SendWorker` ignored partial `sendmsg()` sends, and a timeout re-sent the
  whole packet. Large DMA payloads could corrupt the stream.
- An invalid msgpack header or any unexpected error killed the receive thread
  permanently.
- `MemoryBoundedQueue` counted network packets as about 50 bytes, ignoring
  their payload, so the 1 GB bound had no effect.
- An item larger than the queue limit blocked `put()` forever.
- A run could hang forever after the client disconnected: nobody drained the
  output queue, so the DMA transfer blocked in `put()` once it was full and the
  server never accepted another client. Packets are now dropped while no client
  is connected, and a blocked `put()` gives up within 0.5 s.
- A packet queued for a new client, e.g. its handshake, could be sent on the
  previous client's closed socket and lost, so the new client timed out. Packets
  are now tagged with the session they were queued in and sent on that
  session's socket.
- A message from a previous client could reach the main thread after the input
  queue had been cleared, e.g. as the next client's handshake answer. Messages
  are now tagged with their socket and dropped if it has been replaced.
- A sweep expression that used an undefined variable failed after the
  `experiment_header` had been sent; it is now rejected up front.
- A sweep whose expressions reference no variable (e.g. `#0.5`) raised
  `IndexError` after the `experiment_header` had been sent. It is now rejected
  up front, as is a swept variable with no values.
- Sweep callbacks that returned an error code were silently ignored.
- `apply_configuration` raised `TypeError` (`len(None)`) when the configuration
  failed.
- `log_and_send_error` sent a `warning` packet.
- `run_experiment` looped forever when the number of hw shots was 0, for
  example when an acquisition payload does not fit in a FIFO. It also raised
  `TypeError` when no acquisition was configured. Both cases now raise a
  descriptive `RuntimeError`.
- DMA transfer errors were ignored, and a failed run left the DMA nodes in the
  "transferring" state. Every later run then failed, even after `reset_all`.
- `trigger_ip_manually` raised `KeyError` for unknown IPs instead of the
  documented `ValueError`, and it ignored the trigger's error code.
- `ZeroDivisionError` in the FIFO node for an empty input payload.
- A pulse gain of ±1.0 was written as 0, which produced a silent pulse. Full scale now maps to
  the maximum gain code.
- The interpolation increment lost its fractional part, so a stretched envelope stayed on
  its first sample. A duration of 1 sample raised `ZeroDivisionError`.
- Envelope I/Q packing corrupted the I half when Q was negative.
- Envelope samples are now scaled from ±1 to the signed 16-bit range before being written.
  Previously they were truncated to 0/±1. Samples outside [-1, 1] are rejected.
- The debug MMIO wrapper closed its log file after the first write, ignored writes when
  reading back, logged the same address for every word of a block write, and had no
  `length`.
- Lint: unused variables and imports, missing annotations and docstrings.
- Stale documentation referring to `API.py`, which was renamed to
  `start_server.py`.

### Removed

- `SweepExperiment._put_sweep_info_in_network()` and the unused `copy`, `Queue`
  and `FIREQNetworkPacket` imports.
- The unused `FIREQServer._abort_in_progress` event and the unused
  `SWEEP_WAIT_TIMEOUT_SECONDS` constant.
- Commented-out deprecated command branches in the server's message dispatcher.

## [0.1.0] - 2026-08-29

- Initial public release.

[0.1.1]: https://github.com/vlsi-nanocomputing/FIREQ-Server/compare/version-0.1.0...version-0.1.1
[0.1.0]: https://github.com/vlsi-nanocomputing/FIREQ-Server/releases/tag/version-0.1.0
