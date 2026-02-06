# file: fireq-utils/server/hardware/__init__.py
"""Hardware abstraction layer for FIREQ.

This package provides:
- OverlayAdapter: Server-facing composition-based adapter for FIREQ hardware
- DMAEngine: High-level DMA acquisition manager
- Data structures: WaveEntry, Modulation, TriggerCommand, EnvelopeSpec

The OverlayAdapter composes four operation classes:
- GeneratorOps: Wave management and generator modulation/triggering
- TriggerGeneratorOps: Trigger generator control
- AcquisitionOps: DMA-based multi-ADC acquisition with chunking
- ExperimentOps: High-level experiment orchestration
"""

from ..models.config_types import Modulation, TriggerCommand
from .dma_engine import DMAEngine
from .ol_adapter import OverlayAdapter
from .ol_adapter.overlay_adapter_types import EnvelopeSpec, WaveEntry

__all__ = [
    "OverlayAdapter",
    "DMAEngine",
    # Re-export from types for backwards compatibility
    "WaveEntry",
    "Modulation",
    "TriggerCommand",
    "EnvelopeSpec",
]
