# Server Package

Top-level FIREQ server package: network transport, execution orchestration, hardware control, and shared data models.

## Architecture

```
server/
 ├── network/      – TCP server, authentication, message framing, sender/receiver threads
 ├── execution/    – command orchestration for run/sweep flows and streaming metadata/chunks
 ├── hardware/     – OverlayAdapter + DMAEngine hardware abstraction layer
 ├── models/       – shared TypedDicts, dataclass results, queue items, exception hierarchy
 └── __init__.py   – package public API re-exports
```

## Runtime Flow

1. `network.FIREQServer` receives framed JSON requests and validates session/auth state.
2. Commands are routed to `execution.MessageHandler` (or specialized handlers) on the main thread.
3. `execution` configures and drives `hardware.OverlayAdapter` / `DMAEngine`.
4. Stream items (`StreamHeader`, `BinaryChunk`, `StreamTiming`) and command responses are returned through the network sender thread.

## Files

| File / Folder | Public API | Responsibility |
|---|---|---|
| [`network/`](network/README.md) | `FIREQServer` | Single-client TCP server and protocol boundary. Owns receiver/sender threads, handshake/auth, command dispatch, and queue-based streaming of experiment/sweep output. |
| [`execution/`](execution/README.md) | `MessageHandler`, `SweepPlan`, `plan_sweep`, `apply_sweep_point`, `StatusHandler`, `ResetHandler`, `EnvelopeHandler`, `WaveHandler`, ... | Execution layer that translates command payloads into hardware actions for single runs and sweeps, with fast-path reconfiguration and streaming metadata generation. |
| [`hardware/`](hardware/README.md) | `OverlayAdapter`, `DMAEngine`, `WaveEntry`, `EnvelopeSpec`, `Modulation`, `TriggerCommand` | Hardware abstraction layer over FIREQ low-level drivers: generator/readout/trigger/acquisition operations and DMA buffer/transfer orchestration. |
| [`models/`](models/README.md) | `ExperimentConfig`, `SweepMessage`, `HardwareStatusResult`, `ResetResult`, `SweepStatus`, `StreamHeader`, `BinaryChunk`, `StreamTiming`, `FireqHardwareError`, ... | Shared type system for configuration contracts, queue payloads, serialized result objects, and typed error handling across network/execution/hardware boundaries. |
| `__init__.py` | `FIREQServer`, `OverlayAdapter`, `DMAEngine`, `MessageHandler`, `WaveEntry`, `Modulation`, `TriggerCommand`, `EnvelopeSpec`, `HardwareStatusResult`, `ResetResult`, `SweepStatus`, `FireqHardwareError`, ... | Unified import surface for consumers (e.g., `API.py`) that re-exports core server classes, result types, and exception hierarchy. |
