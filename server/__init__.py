"""
FIREQ Server Module.
Exposes the main adapter and data structures.
"""

from .exceptions import (
    FireqHardwareError,
    DriverError,
    ConfigurationError,
    HardwareStateError,
    DMAError,
    DMATimeoutError,
    HardwareResourceError,
    TimingError
)

# Importa la classe OL_adapter e le strutture dati dal file ol_adapter.py
from .ol_adapter import (
    OL_adapter, 
    WaveEntry, 
    modulation,      
    trigger_command,
    EnvelopeSpec
)

from .dma_engine import AcquisitionEngine

__all__ = [
    'OL_adapter',
    'WaveEntry',
    'modulation',
    'trigger_command',
    'EnvelopeSpec',
    'FireqHardwareError',
    'DriverError',
    'ConfigurationError',
    'HardwareStateError',
    'DMAError',
    'AcquisitionEngine'
]