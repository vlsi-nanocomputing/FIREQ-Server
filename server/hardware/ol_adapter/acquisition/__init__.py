"""Acquisition control submodule.

Organizes acquisition operations into focused components:
- dma_orchestrator.py: DMA acquisition orchestration with chunking and pipelining
- sweep_ops.py: Sweep mode optimization and state management
- modulation_ops.py: DDS modulation and trigger listener configuration
- timing_ops.py: Time-of-flight and duration timing configuration
- acquisition_ops.py: Main AcquisitionOps facade

The AcquisitionOps class provides the public API for all acquisition-related operations.
"""

from .acquisition_ops import AcquisitionOps

__all__ = ["AcquisitionOps"]
