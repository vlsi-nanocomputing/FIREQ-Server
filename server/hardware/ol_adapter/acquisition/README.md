# Acquisition Submodule

DMA-based multi-ADC acquisition control for the FIREQ overlay adapter.

## Architecture

This module follows a  **facade pattern**: `AcquisitionOps` is the single public
entry point, delegating to five operation classes that share a common
`AdapterContext`.

```
AcquisitionOps  (facade)
 ├── DMAOrchestrator      – data retrieval methods, chunking, pipelining
 ├── SweepOps             – sweep-mode optimizations and teardown
 ├── ModulationOps        – DDS frequency/phase and Mix-Mode
 ├── TriggerOps           – trigger channel assignment
 └── TimingOps            – time-of-flight and duration configuration
```

## Files

| File | Class | Responsibility |
|---|---|---|
| `acquisition_ops.py` | `AcquisitionOps` | Public facade that coordinates all acquisition operations. Every call from outside this package goes through here. |
| `dma_orchestrator.py` | `DMAOrchestrator` | Orchestrates DMA data transfer: automatic chunking when shots exceed hardware limits (for current version,1024 hardware shots), pipelined ARM/TRIGGER/RETRIEVE cycles, and timeout management. |
| `sweep_ops.py` | `SweepOps` | Prepares and tears down sweep-optimized execution. Locks acquisition IP configuration and pre-allocates DMA buffers so repeated points avoid redundant setup. |
| `modulation_ops.py` | `ModulationOps` | Configures DDS modulation parameters (frequency, phase) and ADC Mix-Mode for Nyquist zone selection. Remark: Nyquist zone setting is explicitly specified in order to activate RFSoC filters for even/odd Nyquist windows. |
| `trigger_ops.py` | `TriggerOps` | Configures which trigger channel each acquisition unit listens to. |
| `timing_ops.py` | `TimingOps` | Sets time-of-flight delay and acquisition duration for each acquisition unit. |
| `__init__.py` | — | Exports `AcquisitionOps` as the package's public API. |
