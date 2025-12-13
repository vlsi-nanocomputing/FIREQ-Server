# file: fireq_orchestrator/hardware/__init__.py
"""
FIREQ Hardware Abstraction Layer.

This package provides a clean interface between the high-level orchestrator
and the low-level FPGA drivers. It handles:

- Hardware discovery and validation (HardwareInventory)
- DMA transfer management (AcquisitionEngine)
- Driver wrapping with error handling (adapters)
- Timing validation (TimingValidator)
- Unified exception hierarchy

Main Entry Point:
    FireqHardwareBackend - High-level facade for all hardware operations

Example:
    >>> from fireq_orchestrator.hardware import FireqHardwareBackend
    >>> backend = FireqHardwareBackend(overlay, debug=True)
    >>> data = backend.start_experiment(duration_cycles=1000, readout_cfg={...})

Exception Handling:
    >>> from fireq_orchestrator.hardware import DMATimeoutError, TimingError
    >>> try:
    ...     data = backend.start_experiment(...)
    ... except DMATimeoutError:
    ...     print("Acquisition timed out")
    ... except TimingError as e:
    ...     print(f"Invalid timing: {e}")
"""


# Exceptions (for error handling)
from .exceptions import (
    FireqHardwareError,
    DriverError,
    TimingError,
    ConfigurationError,
    DMAError
)

# Sub-components (for advanced users)
from .dma_engine import AcquisitionEngine
from .timing import TimingValidator
from .ol_adapter import OL_adapter

__all__ = [
    
    # Exceptions
    'DMATimeoutError',
    'FireqHardwareError',
    'DriverError',
    'TimingError',
    'ConfigurationError',
    'DMAError',
    
    # Sub-components (advanced)
    'HardwareInventory',
    'AcquisitionEngine',
    'TimingValidator',
    'GeneratorAdapter',

]
