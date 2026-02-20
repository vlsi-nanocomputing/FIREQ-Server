# Models Package

Data structures and exceptions shared across the FIREQ server.

## Architecture

```
models/
 ├── config_types.py   – TypedDict definitions for experiment configuration
 ├── results.py        – dataclass containers for operation outcomes
 ├── queue_items.py    – dataclass items for inter-thread queue communication
 └── exceptions.py     – custom exception hierarchy for hardware errors
```

## Files

| File | Public API | Responsibility |
|---|---|---|
| `config_types.py` | `ExperimentConfig`, `SweepMessage`, `GeneratorConfig`, … | TypedDict definitions documenting the expected JSON configuration structure. All fields optional (`total=False`) to support partial configs and sweep placeholders. Also hosts `Modulation` and `TriggerCommand` used by the hardware layer. |
| `results.py` | `HardwareStatusResult`, `ResetResult`, `SweepStatus`, `SweepTimingStats` | Dataclass containers with `to_dict()` for JSON serialization of operation outcomes. |
| `queue_items.py` | `StreamHeader`, `BinaryChunk`, `StreamTiming` | Typed dataclasses for items passed through `queue_out` from the main thread to the sender thread during streaming commands. |
| `exceptions.py` | `FireqHardwareError` and 10 subclasses | Typed exception hierarchy isolating the orchestrator from inconsistent low-level driver error handling. See the class docstring for the full tree. |
| `__init__.py` | — | Re-exports all public symbols from the submodules plus `EnvelopeSpec`, `WaveEntry`, `WaveKind`, `same_spec` from the hardware layer. |
