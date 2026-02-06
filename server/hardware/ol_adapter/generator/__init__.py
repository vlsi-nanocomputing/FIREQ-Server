"""Generator control submodule.

Organizes generator operations into focused components:
- wave_envelope_ops.py: Wave definition, compilation, envelope management
- fifo_ops.py: Drive sequence FIFO programming
- modulation_ops.py: DDS modulation and Nyquist zone configuration
- trigger_listener_ops.py: Generator trigger listener configuration
- wave_utils.py: Pure utility functions for wave/envelope processing
- iq_conversion.py: IQ signal conversion utilities
- generator_ops.py: Main GeneratorOps facade

The GeneratorOps class provides the public API.
"""

from .generator_ops import GeneratorOps

__all__ = ["GeneratorOps"]
