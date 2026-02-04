# file: fireq-utils/server/hardware/__init__.py
"""Hardware abstraction layer for FIREQ.

This package provides:
- OverlayAdapter: Server-facing composition-based adapter for FIREQ hardware
- AcquisitionEngine: High-level DMA acquisition manager
- Data structures: WaveEntry, Modulation, TriggerCommand, EnvelopeSpec

The OverlayAdapter composes four operation classes:
- GeneratorOps: Wave management and generator modulation/triggering
- TriggerOps: Trigger generator control
- AcquisitionOps: DMA-based multi-ADC acquisition with chunking
- ExperimentOps: High-level experiment orchestration
"""

from ..models.config_types import Modulation, TriggerCommand
from .dma_engine import AcquisitionEngine
from .ol_adapter import OverlayAdapter
from .ol_adapter.types import EnvelopeSpec, WaveEntry

__all__ = [
    "OverlayAdapter",
    "AcquisitionEngine",
    # Re-export from types for backwards compatibility
    "WaveEntry",
    "Modulation",
    "TriggerCommand",
    "EnvelopeSpec",
]
