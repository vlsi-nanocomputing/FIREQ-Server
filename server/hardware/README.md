# Hardware Package

Hardware abstraction layer for FIREQ.

## Architecture

```
hardware/
 ├── ol_adapter/          – OverlayAdapter: server-facing adapter
 │    └── generator_utils/ – pure utility functions for wave/envelope processing
 └── dma_engine.py        – low-level DMA buffer management
```

## Files

| File / Folder | Public API | Responsibility |
|---|---|---|
| [`ol_adapter/`](ol_adapter/README.md) | `OverlayAdapter` | Server-facing adapter that composes generator, acquisition, trigger, and experiment operations on top of the low-level FIREQ drivers. |
| `dma_engine.py` | `DMAEngine` | Low-level DMA acquisition utilities: AXI stream routing, DDR buffer allocation via PYNQ, DMA transfer management with timeout/recovery. |
| `__init__.py` | — | Re-exports `OverlayAdapter`, `DMAEngine`, `WaveEntry`, `EnvelopeSpec`, `Modulation`, `TriggerCommand`. |
