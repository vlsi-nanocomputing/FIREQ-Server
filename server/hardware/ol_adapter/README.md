# Overlay Adapter

Server-oriented adapter layer on top of the low-level FIREQ hardware drivers (`FIREQ_SoC`).

## Architecture

`OverlayAdapter` composes three flat operation classes, each receiving its own `LowLevelAccess` instance for driver interaction.

```
OverlayAdapter
 ├── GeneratorOps        – wave/envelope management, FIFO, modulation, triggering
 ├── TriggerGeneratorOps – trigger generator: shots, duration, delays, experiment start
 ├── AcquisitionOps      – DMA-based multi-acquisition, sweep mode, timing
 └── _ExperimentProxy    – thin proxy for sweep prepare/finalize (delegates to AcquisitionOps)
```

Each ops class owns its own mutable state (wave caches, FIFO tracking, sweep flags). There are no shared mutable containers.

`LowLevelAccess` is instantiated three times (one per ops class) with a per-class `driver_name` so that error messages automatically identify the originating subsystem.

## Files

| File | Class / Role | Responsibility |
|---|---|---|
| `overlay_adapter.py` | `OverlayAdapter`, `_ExperimentProxy` | Main entry point. Composes operation classes, exposes proxy access to the low-level driver, provides timing statistics, and hosts the sweep proxy. |
| `overlay_adapter_types.py` | `EnvelopeSpec`, `WaveEntry`, `same_spec` | Data structures for envelope specs, wave cache entries, and wave equivalence checks. |
| `_low_level_access.py` | `LowLevelAccess` | Bounds-checked device getters, hardware spec queries, Mix-Mode configuration, and unified return-code validation via `check_result()`. |
| `_errors.py` | `handle_error_result`, `ERROR_HINTS` | Translates low-level integer return codes into Python exceptions with diagnostic hints. |
| `_gen_ops.py` | `GeneratorOps` | Wave compilation, envelope upload, readout wave config, FIFO sequencing, DDS modulation, Nyquist zone, trigger channel assignment, and memory reset. |
| `_acq_ops.py` | `AcquisitionOps` | DMA acquisition orchestration with automatic chunking, sweep-mode fast path, DDS modulation, trigger channel assignment, and timing configuration. |
| `_trigger_gen_ops.py` | `TriggerGeneratorOps` | Trigger generator control: shot count, experiment duration, drive/readout delay programming, and experiment start. |
| `__init__.py` | — | Exports `OverlayAdapter` as the package's public API. |

## Subpackages

| Folder | Purpose |
|--------|---------|
| [`generator_utils/`](generator_utils/README.md) | Pure utility functions for wave entry building, envelope validation/processing, and IQ conversion. No classes — used by `GeneratorOps`. |
