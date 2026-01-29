"""FIREQ Server Module.

This package exposes the main adapter, data structures, exceptions, and the TCP server
class for the FIREQ system.
"""

from .execution.message_handler import MessageHandler
from .hardware import OverlayAdapter
from .hardware.dma_engine import AcquisitionEngine
from .models import EnvelopeSpec, Modulation, TriggerCommand, WaveEntry
from .models.exceptions import (
    ConfigurationError,
    DMAError,
    DMATimeoutError,
    DriverError,
    EnvelopeUploadError,
    FireqHardwareError,
    HardwareResourceError,
    HardwareStateError,
    TimingError,
    WaveCompilationError,
)
from .models.results import HardwareStatusResult, ResetResult, SweepStatus
from .network.tcp_server import FIREQServer

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
    "EnvelopeUploadError",
    "WaveCompilationError",
]
