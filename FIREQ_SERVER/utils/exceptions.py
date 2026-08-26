# file: fireq-utils/server/models/exceptions.py
"""Custom exceptions for the hardware layer.

These exceptions provide a clean, typed error interface for the orchestrator, isolating
it from the inconsistent error handling in the low-level drivers.
"""


class FireqHardwareError(Exception):
    """Base exception for all hardware-related errors.

    All exceptions in the hardware layer inherit from this,
    allowing callers to catch all hardware errors with a single except clause.

    Exception Hierarchy::

        FireqHardwareError
        ├── DriverError           (low-level driver failed)
        ├── TimingError           (timing constraints violated)
        ├── ConfigurationError    (invalid configuration)
        │   ├── FrequencyError    (frequency out of range)
        │   ├── EnvelopeUploadError (envelope upload failed)
        │   └── WaveCompilationError (wave compilation failed)
        ├── DMAError              (DMA operation failed)
        │   ├── DMATimeoutError   (DMA transfer timeout)
        │   └── RecoverableDMAError (error but data may be valid)
        ├── HardwareResourceError (resource access failed)
        └── HardwareStateError    (unexpected hardware state)
    """

    pass


class DriverError(FireqHardwareError):
    """Error originating from a low-level driver.

    This wraps the inconsistent return codes (-3, None, etc.) from the
    FIREQ_LL_API drivers into proper Python exceptions.

    Example::

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
        """Initialize a DriverError with optional driver context.

        :param message: Human-readable error description.
        :type message: str
        :param driver_name: Name of the driver that failed.
        :type driver_name: str | None
        :param operation: The operation that was attempted.
        :type operation: str | None
        :param return_code: The original return code from the driver.
        :type return_code: int | None
        """
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

    Example::

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
        """Initialize a TimingError with constraint details.

        :param message: Human-readable error description.
        :type message: str
        :param parameter: Name of the timing parameter that violated constraints.
        :type parameter: str | None
        :param value: The invalid value that was provided.
        :type value: float | None
        :param min_required: Minimum valid value.
        :type min_required: float | None
        :param max_allowed: Maximum valid value.
        :type max_allowed: float | None
        """
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

    Example::

        >>> try:
        ...     gen.set_drive_frequency(-100)
        ... except ConfigurationError as e:
        ...     print(f"Error: {e}")
    """

    pass


class FrequencyError(ConfigurationError):
    """Raised when frequency is out of valid range.

    Provides information about valid Nyquist zones and frequency ranges.

    Example::

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
        """Initialize a FrequencyError with optional valid ranges.

        :param freq_mhz: The frequency that was rejected.
        :type freq_mhz: float
        :param reason: Human-readable rejection reason.
        :type reason: str
        :param valid_ranges: List of (min_mhz, max_mhz) tuples showing valid ranges.
        :type valid_ranges: list[tuple[float, float]] | None
        """
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
    """

    def __init__(
        self,
        message: str,
        recovery_strategy: str = "fatal",
    ) -> None:
        """Initialize a DMAError with recovery strategy.

        :param message: Human-readable error description.
        :type message: str
        :param recovery_strategy: ``'fatal'`` or ``'recoverable'``.
        :type recovery_strategy: str
        """
        super().__init__(message)
        self.recovery_strategy = recovery_strategy


class DMATimeoutError(DMAError):
    """Exception raised when a DMA transfer exceeds the allowed time limit.

    This is a specific, recoverable case of DMA failure where the transfer
    simply took too long.
    """

    def __init__(
        self,
        message: str,
        timeout_seconds: float,
        recovery_strategy: str = "fatal",
    ) -> None:
        """Initialize a DMATimeoutError with timeout details.

        :param message: Human-readable error description.
        :type message: str
        :param timeout_seconds: The timeout that was exceeded.
        :type timeout_seconds: float
        :param recovery_strategy: ``'fatal'`` or ``'recoverable'``.
        :type recovery_strategy: str
        """
        super().__init__(message, recovery_strategy=recovery_strategy)
        self.timeout_seconds = timeout_seconds


class RecoverableDMAError(DMAError):
    """DMA error that occurred but data may still be recoverable.

    Example: Internal error bit set but TLAST (transfer last) received,
    indicating the transfer actually completed.

    Recovery strategy: RECOVERABLE — attempt to parse data anyway.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
    ) -> None:
        """Initialize a RecoverableDMAError with status code.

        :param message: Human-readable error description.
        :type message: str
        :param status_code: The DMA status register value.
        :type status_code: int | None
        """
        super().__init__(message, recovery_strategy="recoverable")
        self.status_code = status_code


class HardwareResourceError(FireqHardwareError):
    """Raised when a hardware resource cannot be accessed or allocated.

    Examples:
    - Invalid generator/acquisition index
    - Buffer allocation failure
    - Clock not locked
    - Channel not available

    Recovery strategy: FATAL — fix configuration and retry.
    """

    def __init__(
        self,
        message: str,
        resource_type: str | None = None,
        resource_id: object | None = None,
    ) -> None:
        """Initialize a HardwareResourceError with resource metadata.

        :param message: Human-readable error description.
        :type message: str
        :param resource_type: Type of resource that failed (e.g., 'generator', 'buffer').
        :type resource_type: str | None
        :param resource_id: Identifier of the resource.
        :type resource_id: object | None
        """
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

    Recovery strategy: Usually FATAL — indicates firmware or hardware issue.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        expected_state: str | None = None,
        actual_state: str | None = None,
    ) -> None:
        """Initialize a HardwareStateError with state details.

        :param message: Human-readable error description.
        :type message: str
        :param status_code: Hardware status register value.
        :type status_code: int | None
        :param expected_state: Description of expected state.
        :type expected_state: str | None
        :param actual_state: Description of actual state.
        :type actual_state: str | None
        """
        super().__init__(message)
        self.status_code = status_code
        self.expected_state = expected_state
        self.actual_state = actual_state


class ClientDisconnectedError(Exception):
    """Raised when the remote peer disconnects (gracefully or abruptly)."""

    pass


class IncompleteTransferError(Exception):
    """Raised when a socket transfer was not completed successfully."""

    pass


class InvalidPayloadError(Exception):
    """Raised when payload parsing fails."""

    pass


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
    "ClientDisconnectedError",
    "IncompleteTransferError",
    "InvalidPayloadError",
]
