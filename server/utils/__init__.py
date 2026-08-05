# file: fireq-utils/server/models/__init__.py
"""Data structures and exceptions for FIREQ server."""

from .exceptions import (
    ClientDisconnectedError,
    ConfigurationError,
    DMAError,
    DMATimeoutError,
    DriverError,
    EnvelopeUploadError,
    FireqHardwareError,
    FrequencyError,
    HardwareResourceError,
    HardwareStateError,
    IncompleteTransferError,
    InvalidPayloadError,
    RecoverableDMAError,
    TimingError,
    WaveCompilationError,
)
from .memory_queue import MemoryBoundedQueue

__all__ = [
    # Exceptions
    "FireqHardwareError",
    "DriverError",
    "TimingError",
    "ConfigurationError",
    "FrequencyError",
    "EnvelopeUploadError",
    "WaveCompilationError",
    "DMAError",
    "DMATimeoutError",
    "RecoverableDMAError",
    "HardwareResourceError",
    "HardwareStateError",
    "ClientDisconnectedError",
    "IncompleteTransferError",
    "InvalidPayloadError",
    # memory limited queue
    "MemoryBoundedQueue",
]
