"""Error handling and translation for the hardware adapter layer.

This module centralizes error handling for FIREQ low-level driver return codes,
providing:
- Normalization of integer error codes to Python exceptions
- User-friendly hints for common error patterns
- Uniform error reporting across all hardware operations
"""

import logging

from ...models.exceptions import ConfigurationError

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
    ("AcquisitionDriver", "set_decimated_output_type", -3): (
        "Check: output_type must be 'decimated' or 'accumulated'."
    ),
    ("TriggerGeneratorDriver", "set_number_of_shots", -3): ("Check: number of shots in range [1..max_hw_repetitions]."),
}


def check_driver_result(
    result: object,
    *,
    operation: str,
    driver_name: str,
    logger: logging.Logger,
    hint: str | None = None,
) -> object:
    """Check a low-level driver return code and raise on error.

    Non-negative results (or non-integers) pass through unchanged.
    Negative integers are interpreted as error codes and raise
    ``ConfigurationError`` with a diagnostic hint from ``ERROR_HINTS``.

    :param result: Return value from a driver method.
    :type result: object
    :param operation: Name of the driver operation (for error messages and hint lookups).
    :type operation: str
    :param driver_name: Name of the driver class (for error messages and hint lookups).
    :type driver_name: str
    :param logger: Logger instance for error reporting.
    :type logger: logging.Logger
    :param hint: Explicit diagnostic hint, overriding the ``ERROR_HINTS`` lookup.
    :type hint: str | None
    :return: The original ``result`` on success.
    :rtype: object
    :raises ConfigurationError: If the result is a negative integer.
    """
    if not (isinstance(result, int) and result < 0):
        return result

    code = int(result)
    message = (
        hint
        or ERROR_HINTS.get((driver_name, operation, code))
        or (f"{driver_name}.{operation} failed with code {code}")
    )
    logger.error(message)
    raise ConfigurationError(message)


__all__ = [
    "ERROR_HINTS",
    "check_driver_result",
]
