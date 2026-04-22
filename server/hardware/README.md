# Hardware Package

Server-facing hardware abstraction layer for FIREQ.

## Architecture

```text
hardware/
├── ol_adapter/    - OverlayAdapter and flat operation classes over FIREQ low-level drivers
├── dma_engine.py  - DMA buffer allocation, transfer management, timeout/recovery
└── __init__.py    - public hardware exports
```

The `hardware` package separates two concerns:

- `OverlayAdapter`: server-friendly control surface for generators, acquisitions, and trigger programming.
- `DMAEngine`: high-level DMA buffer and transfer orchestration for acquisition data movement.

## Files

| File / Folder | Public API | Responsibility |
|---|---|---|
| [`ol_adapter/`](ol_adapter/README.md) | `OverlayAdapter` | Server-facing adapter that composes generator, acquisition, trigger, and experiment operations on top of the low-level FIREQ drivers. |
| `dma_engine.py` | `DMAEngine` | DMA acquisition utilities: AXI stream routing, DDR buffer allocation via PYNQ, DMA transfer management with timeout/recovery, and chunked acquisition support. |
| `__init__.py` | `OverlayAdapter`, `DMAEngine`, `WaveEntry`, `EnvelopeSpec`, `Modulation`, `TriggerCommand` | Re-exports the hardware package public surface used by the server and models packages. |
