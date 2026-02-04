# file: fireq-utils/server/models/__init__.py
"""Data structures and exceptions for FIREQ server.

This package contains:
- Configuration TypedDicts for IDE autocomplete support
- Result dataclasses for operation outcomes
- Custom exception hierarchy for hardware errors
"""

from ..hardware.ol_adapter.types import (
    EnvelopeSpec,
    WaveEntry,
    WaveKind,
    same_spec,
)
from .config_types import (
    AcquisitionConfig,
    ExperimentConfig,
    GeneratorConfig,
    GeneratorDriveConfig,
    GeneratorReadoutConfig,
    Modulation,
    SweepMessage,
    SweepVariableSpec,
    TriggerCommand,
    TriggerConfig,
    TriggerDelayConfig,
)
from .exceptions import (
    ConfigurationError,
    DMAError,
    DMATimeoutError,
    DriverError,
    EnvelopeUploadError,
    FireqHardwareError,
    FrequencyError,
    HardwareResourceError,
    HardwareStateError,
    RecoverableDMAError,
    TimingError,
    WaveCompilationError,
)
from .queue_items import BinaryChunk, StreamHeader, StreamTiming
from .results import HardwareStatusResult, ResetResult, SweepStatus

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
    # Hardware adapter types
    "Modulation",
    "TriggerCommand",
    "EnvelopeSpec",
    "WaveEntry",
    "WaveKind",
    "same_spec",
    # Results
    "HardwareStatusResult",
    "ResetResult",
    "SweepStatus",
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
]
