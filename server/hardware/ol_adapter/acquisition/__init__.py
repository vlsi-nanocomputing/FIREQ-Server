"""Acquisition control submodule.

Organizes acquisition operations into focused components:
- _dma_orchestrator.py: DMA acquisition orchestration with chunking and pipelining
- _sweep_ops.py: Sweep mode optimization and state management
- _modulation_ops.py: DDS modulation and Mix-Mode configuration
- _trigger_ops.py: Acquisition trigger configuration
- _timing_ops.py: Time-of-flight and duration timing configuration
- _acquisition_ops.py: Main AcquisitionOps facade

The AcquisitionOps class provides the public API for all acquisition-related operations.
"""

from ._acquisition_ops import AcquisitionOps

__all__ = ["AcquisitionOps"]
