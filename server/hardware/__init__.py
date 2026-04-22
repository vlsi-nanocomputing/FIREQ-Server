# file: fireq-utils/server/hardware/__init__.py
"""Hardware abstraction layer for FIREQ.

This package provides:
- OverlayAdapter: Server-facing composition-based adapter for FIREQ hardware
- DMAEngine: High-level DMA acquisition manager
- Data structures: WaveEntry, Modulation, TriggerCommand, EnvelopeSpec

The OverlayAdapter composes three flat operation classes:
- GeneratorOps: Wave management, envelope upload, FIFO, modulation, triggering
- TriggerGeneratorOps: Trigger generator control (shots, duration, delays)
- AcquisitionOps: DMA-based multi-acquisition with chunking and sweep mode
"""

from ..models.config_types import Modulation, TriggerCommand
from .dma_engine import DMAEngine
from .ol_adapter import OverlayAdapter
from .ol_adapter.overlay_adapter_types import EnvelopeSpec, WaveEntry

__all__ = [
    "OverlayAdapter",
    "DMAEngine",
    "WaveEntry",
    "Modulation",
    "TriggerCommand",
    "EnvelopeSpec",
]
