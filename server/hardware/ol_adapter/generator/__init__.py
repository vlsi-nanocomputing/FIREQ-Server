"""Generator control submodule.

Organizes generator operations into focused components:
- _wave_envelope_ops.py: Wave definition, compilation, envelope management
- _fifo_ops.py: Drive sequence FIFO programming
- _modulation_ops.py: DDS modulation and Nyquist zone configuration
- _trigger_ops.py: Generator trigger configuration
- _wave_utils.py: Pure utility functions for wave/envelope processing
- _iq_conversion.py: IQ signal conversion utilities
- _generator_ops.py: Main GeneratorOps facade

The GeneratorOps class provides the public API.
"""

from ._generator_ops import GeneratorOps

__all__ = ["GeneratorOps"]
