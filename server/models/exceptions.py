# file: fireq-utils/server/exceptions.py
"""Custom exceptions for the hardware layer.

These exceptions provide a clean, typed error interface for the orchestrator, isolating
it from the inconsistent error handling in the low-level drivers.
"""


class FireqHardwareError(Exception):
    """Base exception for all hardware-related errors.

    All exceptions in the hardware layer inherit from this,
    allowing callers to catch all hardware errors with a single except clause.

    Exception Hierarchy:
        FireqHardwareError
        ├── DriverError           (low-level driver failed)
        ├── TimingError           (timing constraints violated)
        ├── ConfigurationError    (invalid configuration)
        │   └── FrequencyError    (frequency out of range)
        └── DMAError              (DMA operation failed)
            ├── DMATimeoutError   (DMA transfer timeout)
            └── RecoverableDMAError (error but data may be valid)

    Additional:
        ├── HardwareResourceError (resource access failed)
        └── HardwareStateError    (unexpected hardware state)
    """

    pass


class DriverError(FireqHardwareError):
    """Error originating from a low-level driver.

    This wraps the inconsistent return codes (-3, None, etc.) from the
    FIREQ_LL_API drivers into proper Python exceptions.

    Attributes:
        driver_name: Name of the driver that failed (if available)
        operation: The operation that was attempted
        return_code: The original return code from the driver (if available)

    Example:
        >>> try:
        ...     gen.create_waveform("invalid_name", 100, 0.5)
        ... except DriverError as e:
        ...     print(f"Driver {e.driver_name} failed in {e.operation}")
        ...     print(f"Return code: {e.return_code}")
    """

    def __init__(
        self,
        message: str,
        driver_name: str | None = None,
        operation: str | None = None,
        return_code: int | None = None,
    ) -> None:
        """Initialize a DriverError with optional driver context."""
        super().__init__(message)
        self.driver_name = driver_name
        self.operation = operation
        self.return_code = return_code


class TimingError(FireqHardwareError):
    """Invalid timing parameters for an experiment.

    Raised when experiment parameters violate hardware constraints such as:
    - Duration too short/long
    - Trigger delay >= experiment duration
    - Not enough time for acquisition to complete
    - TOF exceeds hardware limit

    These errors should be caught and parameters adjusted before retrying.

    Attributes:
        parameter: Name of the timing parameter that violated constraints
        value: The invalid value that was provided
        min_required: Minimum valid value (if applicable)
        max_allowed: Maximum valid value (if applicable)

    Example:
        >>> try:
        ...     backend.start_experiment(duration_cycles=10)
        ... except TimingError as e:
        ...     print(f"Need at least: {e.min_required}")
        ...     backend.start_experiment(duration_cycles=e.min_required)
    """

    def __init__(
        self,
        message: str,
        parameter: str | None = None,
        value: float | None = None,
        min_required: float | None = None,
        max_allowed: float | None = None,
    ) -> None:
        """Initialize a TimingError with constraint details."""
        super().__init__(message)
        self.parameter = parameter
        self.value = value
        self.min_required = min_required
        self.max_allowed = max_allowed


class ConfigurationError(FireqHardwareError):
    """Invalid hardware configuration.

    Raised when attempting to configure hardware with invalid parameters:
    - Frequency out of range
    - Gain out of [-1, 1] range
    - Channel index out of range
    - Invalid mode selection

    These errors should be caught and corrected before retrying.

    Example:
        >>> try:
        ...     gen.set_drive_frequency(-100)
        ... except ConfigurationError as e:
        ...     print(f"Error: {e}")
    """

    pass


class FrequencyError(ConfigurationError):
    """Raised when frequency is out of valid range.

    Provides information about valid Nyquist zones and frequency ranges.

    Attributes:
        freq_mhz: The frequency that was rejected
        valid_ranges: List of (min_mhz, max_mhz) tuples showing valid ranges

    Example:
        >>> try:
        ...     gen.set_drive_frequency(10000)
        ... except FrequencyError as e:
        ...     print(f"Frequency {e.freq_mhz} MHz")
        ...     print(f"Valid ranges: {e.valid_ranges}")
    """

    def __init__(
        self,
        freq_mhz: float,
        reason: str,
        valid_ranges: list[tuple[float, float]] | None = None,
    ) -> None:
        """Initialize a FrequencyError with optional valid ranges."""
        self.freq_mhz = freq_mhz
        self.valid_ranges = valid_ranges or []
        message = f"Frequency {freq_mhz} MHz: {reason}"
        if valid_ranges:
            message += f"\nValid ranges (MHz): {valid_ranges}"
        super().__init__(message)


class EnvelopeUploadError(ConfigurationError):
    """Raised when envelope upload fails.

    This exception wraps errors during the envelope upload stage, providing
    context about which generator and envelope failed.

    :param gen_index: Generator index where upload failed.
    :type gen_index: int
    :param envelope_name: Name of the envelope that failed.
    :type envelope_name: str
    :param reason: Human-readable failure reason.
    :type reason: str
    """

    def __init__(self, gen_index: int, envelope_name: str, reason: str) -> None:
        """Initialize an EnvelopeUploadError with context.

        :param gen_index: Generator index where upload failed.
        :type gen_index: int
        :param envelope_name: Name of the envelope that failed.
        :type envelope_name: str
        :param reason: Human-readable failure reason.
        :type reason: str
        """
        self.gen_index = gen_index
        self.envelope_name = envelope_name
        message = f"Envelope '{envelope_name}' upload failed on gen {gen_index}: {reason}"
        super().__init__(message)


class WaveCompilationError(ConfigurationError):
    """Raised when wave compilation fails.

    This exception wraps errors during the wave compilation stage, providing
    context about which generator and wave failed.

    :param gen_index: Generator index where compilation failed.
    :type gen_index: int
    :param wave_id: Identifier of the wave that failed.
    :type wave_id: str
    :param reason: Human-readable failure reason.
    :type reason: str
    """

    def __init__(self, gen_index: int, wave_id: str, reason: str) -> None:
        """Initialize a WaveCompilationError with context.

        :param gen_index: Generator index where compilation failed.
        :type gen_index: int
        :param wave_id: Identifier of the wave that failed.
        :type wave_id: str
        :param reason: Human-readable failure reason.
        :type reason: str
        """
        self.gen_index = gen_index
        self.wave_id = wave_id
        message = f"Wave '{wave_id}' compilation failed on gen {gen_index}: {reason}"
        super().__init__(message)


class DMAError(FireqHardwareError):
    """DMA-related error.

    Raised when DMA operations fail, such as:
    - DMA channel stuck/busy
    - Buffer allocation failure
    - Transfer timeout (see DMATimeoutError for specific timeout case)

    Attributes:
        recovery_strategy: 'fatal' or 'recoverable' indicating if recovery is possible
    """

    def __init__(
        self,
        message: str,
        recovery_strategy: str = "fatal",
    ) -> None:
        """Initialize a DMAError with recovery strategy."""
        super().__init__(message)
        self.recovery_strategy = recovery_strategy


class DMATimeoutError(DMAError):
    """Exception raised when a DMA transfer exceeds the allowed time limit.

    This is a specific, recoverable case of DMA failure where the transfer
    simply took too long.

    Attributes:
        timeout_seconds: The timeout that was exceeded
        recovery_strategy: 'fatal' or 'recoverable'
    """

    def __init__(
        self,
        message: str,
        timeout_seconds: float,
        recovery_strategy: str = "fatal",
    ) -> None:
        """Initialize a DMATimeoutError with timeout details."""
        super().__init__(message, recovery_strategy=recovery_strategy)
        self.timeout_seconds = timeout_seconds


class RecoverableDMAError(DMAError):
    """DMA error that occurred but data may still be recoverable.

    Example: Internal error bit set but TLAST (transfer last) received,
    indicating the transfer actually completed.

    Recovery strategy: RECOVERABLE - attempt to parse data anyway.

    Attributes:
        status_code: The DMA status register value
        recovery_strategy: Always 'recoverable' for this exception
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
    ) -> None:
        """Initialize a RecoverableDMAError with status code."""
        super().__init__(message, recovery_strategy="recoverable")
        self.status_code = status_code


class HardwareResourceError(FireqHardwareError):
    """Raised when a hardware resource cannot be accessed or allocated.

    Examples:
    - Invalid generator/acquisition index
    - Buffer allocation failure
    - Clock not locked
    - Channel not available

    Recovery strategy: FATAL - fix configuration and retry.

    Attributes:
        resource_type: Type of resource that failed (e.g., 'generator', 'buffer')
        resource_id: Identifier of the resource
    """

    def __init__(
        self,
        message: str,
        resource_type: str | None = None,
        resource_id: object | None = None,
    ) -> None:
        """Initialize a HardwareResourceError with resource metadata."""
        super().__init__(message)
        self.resource_type = resource_type
        self.resource_id = resource_id


class HardwareStateError(FireqHardwareError):
    """Raised when hardware is in unexpected state.

    Examples:
    - DMA not idle after stop command
    - Clock not locked
    - Switch routing failed
    - RF-DC tile not responding

    Recovery strategy: Usually FATAL - indicates firmware or hardware issue.

    Attributes:
        status_code: Hardware status register value (if available)
        expected_state: Description of expected state
        actual_state: Description of actual state
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        expected_state: str | None = None,
        actual_state: str | None = None,
    ) -> None:
        """Initialize a HardwareStateError with state details."""
        super().__init__(message)
        self.status_code = status_code
        self.expected_state = expected_state
        self.actual_state = actual_state


__all__ = [
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
