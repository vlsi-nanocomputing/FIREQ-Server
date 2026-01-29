# file: fireq-utils/server/hardware/__init__.py
"""Hardware abstraction layer for FIREQ.

This package provides:
- OverlayAdapter: Server-facing adapter for FIREQ hardware (composed from mixins)
- AcquisitionEngine: High-level DMA acquisition manager
- Data structures: WaveEntry, Modulation, TriggerCommand, EnvelopeSpec (from server.models)
- Utility functions: handle_error_result, iq_float_to_cint16

The OverlayAdapter is composed from four mixin classes in the adapter/ subpackage:
- ModulationMixin: Generator/acquisition modulation and trigger configuration
- TriggerMixin: Trigger generator operations
- WaveMixin: Wave and envelope management
- AcquisitionMixin: DMA acquisition execution
"""

from ..models import EnvelopeSpec, Modulation, TriggerCommand, WaveEntry
from .dma_engine import AcquisitionEngine
from .ol_adapter import OverlayAdapter

__all__ = [
    "OverlayAdapter",
    "AcquisitionEngine",
    # Re-export from models for backwards compatibility
    "WaveEntry",
    "Modulation",
    "TriggerCommand",
    "EnvelopeSpec",
]
