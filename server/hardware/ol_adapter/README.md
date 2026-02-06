# Overlay Adapter

Server-oriented adapter layer on top of the low-level FIREQ hardware drivers (`FIREQ_SoC`).

## Architecture

`OverlayAdapter`integrates four operation classes sharing a common `AdapterContext` for dependency injection.

```
OverlayAdapter
 ├── TriggerGeneratorOps – trigger generator: shots, duration, delays, experiment start
 ├── GeneratorOps        – wave/envelope management, FIFO, modulation  (generator/)
 ├── AcquisitionOps      – DMA-based multi-ADC acquisition, sweep mode (acquisition/)
 └── ExperimentOps       – high-level sweep orchestration
```

`GeneratorOps` and `AcquisitionOps` are facades that delegate to specialized `*Ops`(Operations) classes inside their respective subpackages.

## Files

| File | Class / Role | Responsibility |
|---|---|---|
| `overlay_adapter.py` | `OverlayAdapter` | Main entry point. Composes operation classes, exposes proxy access to the low-level driver, and provides timing statistics. |
| `overlay_adapter_types.py` | `EnvelopeSpec`, `WaveEntry`, `same_spec` | Data structures for envelope specs, wave cache entries, and wave equivalence checks. |
| `cache.py` | `CacheContainers`, `AdapterContext` | Shared mutable state (wave store, FIFO tracking, sweep flags) and the context dataclass injected into every operation class. |
| `low_level_access.py` | `LowLevelAccess` | Safe access to generator/acquisition/trigger drivers with bounds checking and centralized error handling. |
| `errors.py` | `handle_error_result`, `ERROR_HINTS` | Translates low-level integer return codes into Python exceptions with hints. |
| `trigger_generator_ops.py` | `TriggerGeneratorOps` | Trigger generator control: shot count, experiment duration, drive/readout delay programming, experiment start. |
| `experiment_ops.py` | `ExperimentOps` | High-level sweep coordination: prepare and finalize sweep-mode experiments. |
| `__init__.py` | — | Exports `OverlayAdapter` as the package's public API. |

## Subpackages

| Folder | Facade | Purpose |
|--------|--------|---------|
| [`generator/`](generator/README.md) | `GeneratorOps` | Wave compilation, envelope upload, FIFO sequencing, modulation, trigger channel assignment. |
| [`acquisition/`](acquisition/README.md) | `AcquisitionOps` | DMA acquisition orchestration, sweep optimization, modulation, trigger channel assignment, timing configuration. |

## Note on `trigger_ops.py` naming

Two subpackage files share the name `trigger_ops.py` — this is intentional, as they handle trigger *listener* configuration at different levels. The root-level file has a distinct name to clarify it controls the Trigger Generator IP itself.

| Location | Class | Controls |
|----------|-------|----------|
| `ol_adapter/trigger_generator_ops.py` | `TriggerGeneratorOps` | The **Trigger Generator** IP (shots, duration, delays, experiment start) |
| `acquisition/trigger_ops.py` | `TriggerOps` | Which trigger channel each **acquisition unit** listens to |
| `generator/trigger_ops.py` | `TriggerOps` | Which trigger channel each **generator** listens to |
