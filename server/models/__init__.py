# file: fireq-utils/server/models/__init__.py
"""Data structures and exceptions for FIREQ server.

This package contains:
- Configuration TypedDicts for IDE autocomplete support
- Result dataclasses for operation outcomes
- Custom exception hierarchy for hardware errors
"""

from ..hardware.ol_adapter.overlay_adapter_types import (
    EnvelopeSpec,
    WaveEntry,
    WaveKind,
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
from .results import HardwareStatusResult, ResetResult, SweepStatus, SweepTimingStats

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
    # Results
    "HardwareStatusResult",
    "ResetResult",
    "SweepStatus",
    "SweepTimingStats",
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
