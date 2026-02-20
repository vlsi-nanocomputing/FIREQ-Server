# Generator Submodule

Generator IP configuration. Wave compilation, envelope management, FIFO sequencing, and modulation control of the FIREQ overlay adapter.

## Architecture

This module is a  **facade pattern**: `GeneratorOps` is the single public entry point, delegating to four operation classes that share a common `AdapterContext`.

```
GeneratorOps  (facade)
 ├── WaveEnvelopeOps   – wave compilation, envelope upload, readout config, memory reset
 ├── FIFOOps           – drive sequence programming and source selection
 ├── ModulationOps     – DDS frequency/phase and DAC Mix-Mode
 └── TriggerOps        – trigger channel assignment
```

## Files

| File | Class / Role | Responsibility |
|---|---|---|
| `generator_ops.py` | `GeneratorOps` | Public facade that collects, coordinate and expose all generator operations. Every call from outside this package goes through here. |
| `wave_envelope_ops.py` | `WaveEnvelopeOps` | Compiles high-level wave definitions into hardware Wave Definition Words (WDW), uploads envelopes, manages readout wave configuration, and resets wave/envelope memory with appropriate HL cache synchronization. |
| `fifo_ops.py` | `FIFOOps` | Programs the generator FIFO with drive wave sequences, supports partial reconfiguration from arbitrary start indices, and selects drive source (FIFO vs LFSR). |
| `modulation_ops.py` | `ModulationOps` | Configures DDS modulation parameters (frequency, phase) and DAC Mix-Mode for Nyquist zone selection. Provides both automatic zone detection via `set_modulation` and explicit zone override via `set_nyquist_zone`. This choice is meant to allow expert users test different configurations. |
| `trigger_ops.py` | `TriggerOps` | Configures which trigger channel each generator is bound to. |
| `wave_utils.py` | Pure functions | Utility functions for wave entry construction, replacement policy, envelope validation/processing, and readout cache checks. |
| `iq_conversion.py` | Pure function | Converts float I/Q sample arrays to hardware-native `cint16` format with scaling and clipping. This acts as a safety guardrail in case of not-appropriate envelope configurations. |
| `__init__.py` | — | Exports `GeneratorOps` as the package's public API. |
