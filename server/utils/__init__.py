# file: fireq-utils/server/models/__init__.py
"""Data structures and exceptions for FIREQ server.

This package contains:
- Configuration TypedDicts for IDE autocomplete support
- Result dataclasses for operation outcomes
- Custom exception hierarchy for hardware errors
"""

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
from .queue_items import BinaryChunk, SimpleMessage, StreamTiming

__all__ = [
    # Config types
    "GeneratorDriveConfig",
    "GeneratorReadoutConfig",
    "GeneratorConfig",
    "AcquisitionConfig",
    "TriggerDelayConfig",
    "TriggerConfig",
    "SweepVariableSpec",
    "ExperimentConfig",
    "SweepMessage",
    "SimpleMessage",
    # Hardware adapter types
    "Modulation",
    "TriggerCommand",
    "EnvelopeSpec",
    "WaveEntry",
    "WaveKind",
    # Queue items
    "StreamHeader",
    "BinaryChunk",
    "StreamTiming",
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
]
