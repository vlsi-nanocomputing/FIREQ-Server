"""
FIREQ Server Module.

This package exposes the main adapter, data structures, exceptions,
and the TCP server class for the FIREQ system.
"""

# Import exceptions
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

# Import OL_adapter class and related data structures
from .ol_adapter import (
    OL_adapter, 
    WaveEntry, 
    modulation,      
    trigger_command,
    EnvelopeSpec
)

# Import DMA engine
from .dma_engine import AcquisitionEngine

# Import Message Handler and Result structures
from .message_handler import (
    MessageHandler,
    HardwareStatusResult,
    ResetResult,
    EnvelopeResult,
    WaveResult,
    ExperimentResult,
    SweepPointResult,
    SweepStatus
)

# Import TCP Server
from .tcp_server import FIREQServer

__all__ = [
    # Main Server Class
    'FIREQServer',

    # Adapters and Engines
    'OL_adapter',
    'AcquisitionEngine',
    'MessageHandler',

    # Data Structures & Enums
    'WaveEntry',
    'modulation',
    'trigger_command',
    'EnvelopeSpec',

    # Results & Status
    'HardwareStatusResult',
    'ResetResult',
    'EnvelopeResult',
    'WaveResult',
    'ExperimentResult',
    'SweepPointResult',
    'SweepStatus',

    # Exceptions
    'FireqHardwareError',
    'DriverError',
    'ConfigurationError',
    'HardwareStateError',
    'DMAError',
    'DMATimeoutError',       
    'HardwareResourceError',  
    'TimingError',            
]