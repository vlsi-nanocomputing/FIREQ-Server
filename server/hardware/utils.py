# file: fireq-utils/server/hardware/utils.py
"""Utility functions for the hardware adapter layer.

This module contains shared utilities used across the hardware adapter:
- Error handling and translation of driver return codes
- Data conversion functions for hardware communication
"""

import logging

import numpy as np

from ..models.exceptions import ConfigurationError, DriverError

# User-friendly hints for common negative error codes from low-level drivers
ERROR_HINTS: dict[tuple, str] = {
    ("GeneratorDriver", "add_envelope_to_envelope_memory", -3): (
        "Check: samples must be complex (I+jQ), size>=2, "
        "non-interp size multiple of NumberOfChannels, name not already used."
    ),
    ("GeneratorDriver", "create_wave_definition_word", -3): (
        "Check: envelope name exists, gain in [-1,1], " "duration=0 allowed for natural size (esp. non-interp)."
    ),
    ("GeneratorDriver", "add_wave_in_wave_memory", -3): (
        "Wave memory full or name already used. " "Consider reset_wave_memory_dict() if safe."
    ),
    ("GeneratorDriver", "add_wave_to_drive_wave_sequence", -3): (
        "Check: FIFO index valid, wave_name exists in WaveMemoryDict."
    ),
    ("GeneratorDriver", "write_readout_wave", -3): ("Check: wave_definition must be non-negative 128-bit integer."),
    ("GeneratorDriver", "create_vz_gate_definition_word", -3): (
        "Check: phase offset in radians is finite; driver expects a 48-bit "
        "signed value in WDW[47:0] and sets IS_VZ_GATE (bit 119)."
    ),
    ("AcquisitionDriver", "set_acquisition_dds_parameters", -3): (
        "Check: frequency>=0, duration in [1..MaximumDuration], adc_samplerate correct."
    ),
    ("TriggerGeneratorDriver", "insert_drive_delay", -3): (
        "Check: channel range, index range, delay range, generate_trigger is 0/1."
    ),
    ("TriggerGeneratorDriver", "set_readout_delay", -3): (
        "Check: readout channel range, delay non-negative and within HW limits."
    ),
}


def handle_error_result(
    result: object,
    *,  # next methods MUST be specified when the function is used
    operation: str,
    driver_name: str,
    logger: logging.Logger,
    config_error: bool = False,
    hint: str | None = None,
    error_hints: dict[tuple, str] | None = None,
    error_exc: dict[tuple, type[Exception]] | None = None,
) -> object:
    """Normalize FIREQ low-level driver return codes into Python exceptions.

    Low-level FIREQ drivers return integer error codes instead of raising
    exceptions. This function centralizes the translation logic to:

    - enforce a uniform error-handling policy across all drivers,
    - attach semantic context (driver name, operation),
    - optionally upgrade errors to configuration-time failures,
    - provide user-facing hints for known error patterns.

    Error policy
    ------------
    - Non-negative results (or non-integers) are treated as success and
    passed through unchanged.
    - Negative integers are interpreted as error codes.
    - Known error codes may be mapped to:
        * a specific exception class,
        * a human-readable diagnostic hint.
    - Unknown error codes fail fast with a generic DriverError.

    :param result: Return value from a driver method. Negative integers indicate errors.
    :type result: object
    :param operation: Name of the operation/method for error reporting.
    :type operation: str
    :param driver_name: Name of the driver class.
    :type driver_name: str
    :param logger: Logger instance for telemetry.
    :type logger: logging.Logger
    :param config_error: If True, raises ConfigurationError instead of DriverError.
    :type config_error: bool
    :param hint: Explicit hint message to override automatic hints.
    :type hint: Optional[str]
    :param error_hints: Mapping of (driver, op, code) to specific hint strings.
    :type error_hints: Optional[Dict[tuple, str]]
    :param error_exc: Mapping of (driver, op, code) to specific Exception classes.
    :type error_exc: Optional[Dict[tuple, type[Exception]]]
    :return: The original result if non-negative.
    :rtype: object
    :raises ConfigurationError: If result < 0 and config_error is True.
    :raises DriverError: If result < 0 and config_error is False.
    """
    # Success path: anything non-negative or non-int just passes through
    if not (isinstance(result, int) and result < 0):
        return result

    code = int(result)
    key = (driver_name, operation, code)

    # Resolve hint priority: explicit hint > mapping hint > generic
    mapping_hint = None
    if error_hints is not None:
        mapping_hint = error_hints.get(key)

    message = hint or mapping_hint or f"{driver_name}.{operation} failed with code {code}"

    logger.error(message)

    # Optional: raise a specific exception class for known cases
    if error_exc is not None:
        exc_cls = error_exc.get(key)
        if exc_cls is not None:
            raise exc_cls(message)

    # Default exceptions
    if config_error:
        raise ConfigurationError(message)

    raise DriverError(
        message,
        driver_name=driver_name,
        operation=operation,
        return_code=code,
    )


def iq_float_to_cint16(
    samples_iq: list[list[float]] | np.ndarray,
    sample_bits: int,
) -> np.ndarray:
    """Convert user-space floating-point IQ samples into fixed-point complex int16 format.

    This helper performs the necessary quantization for the FPGA, ensuring signal
    integrity via:
    1. **Scaling**: Maps the normalized input range [-1.0, 1.0] to the full
    dynamic range defined by ``sample_bits``.
    2. **Symmetric Rounding**: Uses standard rounding to the nearest integer to
    minimize quantization noise.
    3. **Hard Clipping**: Enforces explicit saturation limits to prevent
    arithmetic overflow.

    :param samples_iq: Either List[List[float]] or np.ndarray with shape (N, 2)
        and dtype float32 (binary input).
    :type samples_iq: Union[List[List[float]], np.ndarray]
    :param sample_bits: The resolution (bit depth) of the target DAC or memory
        (e.g., 16).
    :type sample_bits: int
    :return: A numpy array of complex16 numbers ready for hardware upload.
    :rtype: np.ndarray
    """
    vmax = (1 << (sample_bits - 1)) - 1

    # Fast path for binary input (numpy float32 arrays)
    if isinstance(samples_iq, np.ndarray) and samples_iq.dtype == np.float32:
        # Binary input: skip intermediate float64 conversion
        i = np.clip(np.rint(samples_iq[:, 0] * vmax), -vmax - 1, vmax).astype(np.int16)
        q = np.clip(np.rint(samples_iq[:, 1] * vmax), -vmax - 1, vmax).astype(np.int16)
    else:
        # Legacy JSON input or other numpy dtypes: convert to float64 first
        a = np.asarray(samples_iq, dtype=np.float64)
        i = np.clip(np.rint(a[:, 0] * vmax), -vmax - 1, vmax).astype(np.int16)
        q = np.clip(np.rint(a[:, 1] * vmax), -vmax - 1, vmax).astype(np.int16)

    return i + 1j * q


__all__ = [
    "ERROR_HINTS",
    "handle_error_result",
    "iq_float_to_cint16",
]
