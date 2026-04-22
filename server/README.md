# Server Package

Top-level FIREQ server package: network transport, execution orchestration,
hardware control, and shared data models.

## Architecture

```text
server/
├── network/      - TCP server, authentication, message framing, sender/receiver threads
├── execution/    - command orchestration for run/sweep flows and stream emission
├── hardware/     - OverlayAdapter and DMA engine abstraction layer
├── models/       - shared TypedDicts, dataclass results, queue items, exception hierarchy
└── __init__.py   - package public API re-exports
```

## Runtime Flow

1. `network.FIREQServer` receives framed JSON requests and validates connection/session state.
2. Commands are routed to `execution.MessageHandler` on the main thread.
3. `MessageHandler` delegates to the specialized execution collaborators:
   - `HardwareConfigurator` for full hardware setup
   - `AcquisitionStreamer` for acquisition stream parameters and chunk emission
   - `SweepRunner` for multi-point sweep lifecycle
   - `SweepUpdateApplier` for fast-path per-point hardware updates
4. Execution collaborates with `hardware.OverlayAdapter` and `DMAEngine`.
5. Stream items (`StreamHeader`, `BinaryChunk`, `StreamTiming`) and command responses are returned through the network sender thread.

## Files

| File / Folder | Public API | Responsibility |
|---|---|---|
| [`network/`](network/README.md) | `FIREQServer` | Single-client TCP server and protocol boundary. Owns receiver/sender threads, handshake/auth, command dispatch, and queue-based streaming of experiment/sweep output. |
| [`execution/`](execution/README.md) | `MessageHandler`, `HardwareConfigurator`, `SweepPlan`, `plan_sweep`, `apply_sweep_point`, `StatusHandler`, `ResetHandler`, `EnvelopeHandler`, `WaveHandler`, ... | Execution layer translating command payloads into hardware actions for single runs and sweeps, with explicit internal separation between hardware configuration, streaming, sweep planning, sweep updates, and sweep orchestration. |
| [`hardware/`](hardware/README.md) | `OverlayAdapter`, `DMAEngine`, `WaveEntry`, `EnvelopeSpec`, `Modulation`, `TriggerCommand` | Hardware abstraction layer over the low-level FIREQ drivers: generator/readout/trigger/acquisition operations and DMA buffer/transfer orchestration. |
| [`models/`](models/README.md) | `ExperimentConfig`, `SweepMessage`, `HardwareStatusResult`, `ResetResult`, `SweepStatus`, `StreamHeader`, `BinaryChunk`, `StreamTiming`, `FireqHardwareError`, ... | Shared type system for configuration contracts, queue payloads, serialized result objects, and typed error handling across network/execution/hardware boundaries. |
| `__init__.py` | `FIREQServer`, `OverlayAdapter`, `DMAEngine`, `MessageHandler`, `WaveEntry`, `Modulation`, `TriggerCommand`, `EnvelopeSpec`, `HardwareStatusResult`, `ResetResult`, `SweepStatus`, `FireqHardwareError`, ... | Unified import surface for consumers (for example, `API.py`) that re-exports core server classes, result types, and the exception hierarchy. |
