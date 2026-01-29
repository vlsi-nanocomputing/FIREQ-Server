# file: fireq-utils/server/hardware/adapter/__init__.py
"""Adapter component mixins for OverlayAdapter.

This package contains mixin classes that compose the OverlayAdapter:
- ModulationMixin: Generator/acquisition modulation and trigger configuration
- TriggerMixin: Trigger generator operations (shots, delays, triggering)
- WaveMixin: Wave and envelope management, compilation, FIFO programming
- AcquisitionMixin: DMA acquisition execution, sweep mode, chunking
"""

from .acquisition import AcquisitionMixin
from .modulation import ModulationMixin
from .trigger import TriggerMixin
from .wave import WaveMixin

__all__ = [
    "ModulationMixin",
    "TriggerMixin",
    "WaveMixin",
    "AcquisitionMixin",
]
