# FIREQ_SERVER.utils

Shared data structures and error types for the FIREQ server: a memory-bounded
thread-safe queue and a typed exception hierarchy used across the server
stack.

## Architecture

```text
FIREQ_SERVER/utils/
├── memory_queue.py  - MemoryBoundedQueue
├── exceptions.py    - FireqHardwareError hierarchy + transport errors
└── __init__.py      - public exports
```

## Files

| File | Public API | Responsibility |
|---|---|---|
| `memory_queue.py` | `MemoryBoundedQueue` | Thread-safe FIFO queue bounded by total memory usage (default 1 GB) instead of item count, with blocking `put` (backpressure) and `get`. Used as the server's output queue, where DMA chunks vary greatly in size. |
| `exceptions.py` | `FireqHardwareError`, `DriverError`, `TimingError`, `ConfigurationError`, `FrequencyError`, `EnvelopeUploadError`, `WaveCompilationError`, `DMAError`, `DMATimeoutError`, `RecoverableDMAError`, `HardwareResourceError`, `HardwareStateError`, `ClientDisconnectedError`, `IncompleteTransferError`, `InvalidPayloadError` | Typed exception hierarchy for hardware and transport errors. |

## MemoryBoundedQueue

The queue estimates the memory footprint of each item (recursively for nested
structures, using `nbytes` for NumPy arrays) and blocks on `put()` while the
total would exceed the limit — this applies backpressure on producers (the
DMA payload stream) instead of dropping data. `get()` blocks while empty.
FIFO order is preserved; items come out in the same order they went in. The
queue also supports `clear()` (discard all items) and `close()` (stop
blocking on `put`/`get`).

## Exceptions

### Hardware errors

All hardware-related exceptions derive from `FireqHardwareError`, so callers
can catch the whole hierarchy with a single `except` clause:

```text
FireqHardwareError
├── DriverError              (low-level driver failed; carries driver_name,
│                             operation, return_code)
├── TimingError              (timing constraints violated; carries parameter,
│                             value, min_required, max_allowed)
├── ConfigurationError       (invalid configuration)
│   ├── FrequencyError       (frequency out of range; carries valid ranges)
│   ├── EnvelopeUploadError  (envelope upload failed; gen_index, envelope_name)
│   └── WaveCompilationError (wave compilation failed; gen_index, wave_id)
├── DMAError                 (DMA operation failed; recovery_strategy)
│   ├── DMATimeoutError      (DMA transfer timeout)
│   └── RecoverableDMAError  (error but data may still be valid)
├── HardwareResourceError    (resource access failed)
└── HardwareStateError       (unexpected hardware state)
```

These exceptions were designed to wrap the inconsistent error handling of the
low-level drivers (integer return codes such as `-3`, `None`, ...) into proper
Python exceptions with context.

### Transport errors

- `ClientDisconnectedError` — the remote peer disconnected (gracefully or
  abruptly). Raised by the receive/send workers and cleared by the main loop.
- `IncompleteTransferError` — a socket transfer ended before all expected
  bytes were received.
- `InvalidPayloadError` — a payload could not be parsed.

## Related documentation

- [`../README.md`](../README.md) — server architecture.
- [`../network/README.md`](../network/README.md) — workers that raise the
  transport errors and the output queue that uses `MemoryBoundedQueue`.
