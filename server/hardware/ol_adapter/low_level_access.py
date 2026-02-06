"""Low-level driver access helpers for the hardware adapter.

This module provides standardized access to low-level FIREQ drivers with
integrated error handling and validation, including:
- Safe generator/acquisition/trigger driver access with bounds checking
- Hardware specification queries (sample rates, memory sizes)
- Unified error handling via the errors module
"""

import logging

from ...models.exceptions import ConfigurationError
from .errors import ERROR_HINTS, handle_error_result


class LowLevelAccess:
    """Unified interface for accessing low-level FIREQ_SoC drivers with error handling.

    - Invalid indices raise ConfigurationError immediately
    - All driver return codes are normalized through handle_error_result
    - Hardware specifications are accessible in a standard format
    """

    def __init__(self, overlay_driver: object, logger: logging.Logger) -> None:
        """Initialize the low-level access helper.

        :param overlay_driver: The low-level overlay driver (FIREQ_SoC instance).
        :type overlay_driver: object
        :param logger: Logger instance for error reporting.
        :type logger: logging.Logger
        """
        self.overlay_driver = overlay_driver
        self.logger = logger

    def get_gen(self, gen_index: int) -> object:
        """Retrieve the low-level driver for a specific generator.

        Wraps the list access to ensure that invalid indices raise a high-level
        configuration error rather than a generic Python lookup error.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: The low-level generator driver instance.
        :rtype: object
        :raises ConfigurationError: If the index is out of bounds or invalid.
        """
        try:
            return self.overlay_driver.generators[int(gen_index)]
        except Exception as e:
            raise ConfigurationError(f"Invalid gen_index={gen_index}") from e

    def get_acq(self, acq_index: int) -> object:
        """Retrieve the low-level driver for a specific acquisition unit.

        Wraps the list access to ensure that invalid indices raise a high-level
        configuration error rather than a generic Python lookup error.

        :param acq_index: Index of the target acquisition unit.
        :type acq_index: int
        :return: The low-level acquisition driver instance.
        :rtype: object
        :raises ConfigurationError: If the index is out of bounds or invalid.
        """
        try:
            return self.overlay_driver.acquisitions[int(acq_index)]
        except Exception as e:
            raise ConfigurationError(f"Invalid acq_index={acq_index}") from e

    def get_trig(self) -> object:
        """Retrieve the low-level Trigger Generator driver.

        Validates the existence of the trigger IP in the current overlay configuration
        before returning it.

        :return: The low-level trigger driver instance.
        :rtype: object
        :raises ConfigurationError: If no trigger generator is available.
        """
        if self.overlay_driver.trigger is None:
            raise ConfigurationError("No trigger generator available in overlay")
        return self.overlay_driver.trigger

    def dac_sr_mhz(self) -> float:
        """Retrieve the DAC sampling rate from hardware specifications and convert it to MHz.

        :return: The DAC sampling rate in MHz.
        :rtype: float
        """
        return float(self.overlay_driver.hw_specs["summary"]["dac_sr_hz"]) / 1e6

    def adc_sr_mhz(self) -> float:
        """Retrieve the ADC sampling rate from hardware specifications and convert it to MHz.

        :return: The ADC sampling rate in MHz.
        :rtype: float
        """
        return float(self.overlay_driver.hw_specs["summary"]["adc_sr_hz"]) / 1e6

    def call(
        self,
        result: object,
        *,
        operation: str,
        driver_name: str,
        config_error: bool = False,
        hint: str | None = None,
    ) -> object:
        """Uniform wrapper for low-level driver calls with centralized error handling.

        This method acts as a middleware that intercepts the integer return codes from
        the Low-Level API. It standardizes logging, injects diagnostic hints, and
        converts error codes into semantic Python exceptions.

        :param result: Raw return value from the low-level driver method (typically an
            integer status code).
        :type result: object
        :param operation: The name of the specific operation being performed (used for
            logging context).
        :type operation: str
        :param driver_name: The name of the low-level driver class (used for logging
            context).
        :type driver_name: str
        :param config_error: Strategy flag. If True, maps failures to
            ``ConfigurationError`` (invalid user input). If False, maps failures to
            ``DriverError`` (hardware/runtime failure).
        :type config_error: bool
        :param hint: An explicit diagnostic hint to append to the error message if the
            call fails, overriding default lookups.
        :type hint: Optional[str]
        :return: The original ``result`` passed through unchanged if it indicates
            success (non-negative).
        :rtype: object
        :raises ConfigurationError: If the result is negative and ``config_error`` is True.
        :raises DriverError: If the result is negative and ``config_error`` is False.
        """
        return handle_error_result(
            result,
            operation=operation,
            driver_name=driver_name,
            logger=self.logger,
            config_error=config_error,
            hint=hint,
            error_hints=ERROR_HINTS,
            error_exc=None,
        )


__all__ = ["LowLevelAccess"]
