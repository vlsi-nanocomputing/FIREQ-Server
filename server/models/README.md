# Models Package

Data structures and exceptions shared across the FIREQ server.

## Architecture

```text
models/
├── config_types.py   - TypedDict definitions for experiment configuration
├── results.py        - dataclass containers for operation outcomes
├── queue_items.py    - dataclass items for inter-thread queue communication
├── exceptions.py     - custom exception hierarchy for hardware errors
└── __init__.py       - re-exports shared types used across the server package
```

## Files

| File | Public API | Responsibility |
|---|---|---|
| `config_types.py` | `ExperimentConfig`, `SweepMessage`, `GeneratorConfig`, `AcquisitionConfig`, `TriggerConfig`, `Modulation`, `TriggerCommand`, ... | TypedDict definitions documenting the expected JSON configuration structure. All fields are optional (`total=False`) to support partial configs and sweep placeholders. |
| `results.py` | `HardwareStatusResult`, `ResetResult`, `SweepStatus`, `SweepTimingStats` | Dataclass containers with `to_dict()` helpers for JSON serialization of operation outcomes and timing summaries. |
| `queue_items.py` | `StreamHeader`, `BinaryChunk`, `StreamTiming` | Typed dataclasses for items passed through `queue_out` from the main thread to the sender thread during streaming commands. |
| `exceptions.py` | `FireqHardwareError` and subclasses | Exception hierarchy isolating higher-level orchestration from low-level driver and DMA error handling. |
| `__init__.py` | Config types, queue items, results, exceptions, plus `EnvelopeSpec`, `WaveEntry`, `WaveKind` | Re-exports the shared type surface used throughout `network`, `execution`, and `hardware`. |
