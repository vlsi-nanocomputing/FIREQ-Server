# Overlay Adapter

Server-oriented adapter layer on top of the low-level FIREQ hardware drivers (`FIREQ_SoC`).

## Architecture

`OverlayAdapter` composes three flat operation classes, each receiving the overlay driver directly.

```
OverlayAdapter
 ├── GeneratorOps        – wave/envelope management, FIFO, modulation, triggering
 ├── TriggerGeneratorOps – trigger generator: shots, duration, delays, experiment start
 └── AcquisitionOps      – DMA-based multi-acquisition, sweep mode, timing
```

Each ops class owns its own mutable state (wave caches, FIFO tracking, sweep flags). There are no shared mutable containers. Each class defines a `_DRIVER_NAME` constant for error context and a `_check()` helper that routes through `check_driver_result()` in `_errors.py`.

## Files

| File | Class / Role | Responsibility |
|---|---|---|
| `overlay_adapter.py` | `OverlayAdapter` | Main entry point. Composes operation classes and provides explicit delegations to the overlay driver for `summary()`, `rf_mapping()`, and `hw_specs`. |
| `overlay_adapter_types.py` | `EnvelopeSpec`, `WaveEntry`, `same_spec` | Data structures for envelope specs, wave cache entries, and wave equivalence checks. |
| `_errors.py` | `check_driver_result`, `ERROR_HINTS` | Translates low-level integer return codes into Python exceptions with diagnostic hints. Shared by all ops classes via their `_check()` helper. |
| `_gen_ops.py` | `GeneratorOps` | Wave compilation, envelope upload, readout wave config, FIFO sequencing, DDS modulation, Nyquist zone, trigger channel assignment, and memory reset. |
| `_acq_ops.py` | `AcquisitionOps` | DMA acquisition orchestration with automatic chunking, sweep-mode fast path, DDS modulation, trigger channel assignment, and timing configuration. |
| `_trigger_gen_ops.py` | `TriggerGeneratorOps` | Trigger generator control: shot count, experiment duration, drive/readout delay programming, and experiment start. |
| `__init__.py` | — | Exports `OverlayAdapter` as the package's public API. |

## Subpackages

| Folder | Purpose |
|--------|---------|
| [`generator_utils/`](generator_utils/README.md) | Pure utility functions for wave entry building, envelope validation/processing, and IQ conversion. No classes — used by `GeneratorOps`. |
