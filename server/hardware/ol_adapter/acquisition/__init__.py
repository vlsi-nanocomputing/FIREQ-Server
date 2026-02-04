"""Acquisition control submodule.

Organizes acquisition operations into focused components:
- execution.py: DMA acquisition execution with chunking and pipelining
- sweep.py: Sweep mode optimization and state management
- modulation.py: DDS modulation and trigger listener configuration
- timing.py: Time-of-flight and duration timing configuration
- ops.py: Main AcquisitionOps orchestrator

The AcquisitionOps class provides the public API for all acquisition-related operations.
"""

from .ops import AcquisitionOps

__all__ = ["AcquisitionOps"]
