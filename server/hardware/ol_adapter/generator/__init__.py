"""Generator control submodule.

Organizes generator operations into focused components:
- waves.py: Wave definition, compilation, envelope management
- fifo.py: Drive sequence FIFO programming
- modulation.py: DDS modulation and trigger configuration
- ops.py: Main GeneratorOps orchestrator

The GeneratorOps class provides the public API.
"""

from .ops import GeneratorOps

__all__ = ["GeneratorOps"]
