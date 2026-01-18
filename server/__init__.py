"""FIREQ Server Module.

This package exposes the main adapter, data structures, exceptions, and the TCP server
class for the FIREQ system.
"""

# Import DMA engine
from .dma_engine import AcquisitionEngine

# Import exceptions
from .exceptions import (
    ConfigurationError,
    DMAError,
    DMATimeoutError,
    DriverError,
    FireqHardwareError,
    HardwareResourceError,
    HardwareStateError,
    TimingError,
)

# Import Message Handler and Result structures
from .message_handler import (
    EnvelopeResult,
    ExperimentResult,
    HardwareStatusResult,
    MessageHandler,
    ResetResult,
    SweepPointResult,
    SweepStatus,
    WaveResult,
)

# Import OverlayAdapter class and related data structures
from .ol_adapter import EnvelopeSpec, Modulation, OverlayAdapter, TriggerCommand, WaveEntry

# Import TCP Server
from .tcp_server import FIREQServer

__all__ = [
    # Main Server Class
    "FIREQServer",
    # Adapters and Engines
    "OverlayAdapter",
    "AcquisitionEngine",
    "MessageHandler",
    # Data Structures & Enums
    "WaveEntry",
    "Modulation",
    "TriggerCommand",
    "EnvelopeSpec",
    # Results & Status
    "HardwareStatusResult",
    "ResetResult",
    "EnvelopeResult",
    "WaveResult",
    "ExperimentResult",
    "SweepPointResult",
    "SweepStatus",
    # Exceptions
    "FireqHardwareError",
    "DriverError",
    "ConfigurationError",
    "HardwareStateError",
    "DMAError",
    "DMATimeoutError",
    "HardwareResourceError",
    "TimingError",
]
