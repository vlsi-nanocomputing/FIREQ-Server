"""Low-level driver access helpers for the hardware adapter.

This module provides standardized access to low-level FIREQ drivers with
integrated error handling and validation, including:
- Safe generator/acquisition/trigger driver access with bounds checking
- Hardware specification queries (sample rates, memory sizes)
- Unified error handling via the errors module
"""

import logging

from ...models.exceptions import ConfigurationError
from ._errors import ERROR_HINTS, handle_error_result


class LowLevelAccess:
    """Unified interface for accessing low-level FIREQ_SoC drivers with error handling.

    Each ops class receives its own ``LowLevelAccess`` instance configured with the
    appropriate ``driver_name`` (e.g. ``"GeneratorDriver"``), so that error messages
    automatically include the correct driver context.

    - Invalid indices raise ConfigurationError immediately
    - All driver return codes are normalized through handle_error_result
    - Hardware specifications are accessible via the ``hw_specs`` property
    """

    def __init__(
        self,
        overlay_driver: object,
        logger: logging.Logger,
        *,
        driver_name: str = "Driver",
    ) -> None:
        """Initialize the low-level access helper.

        :param overlay_driver: The low-level overlay driver (FIREQ_SoC instance).
        :type overlay_driver: object
        :param logger: Logger instance for error reporting.
        :type logger: logging.Logger
        :param driver_name: Name of the driver class for error messages
            (e.g. ``"GeneratorDriver"``).
        :type driver_name: str
        """
        self.overlay_driver = overlay_driver
        self.logger = logger
        self._driver_name = driver_name

    # ========================================================================
    # DEVICE GETTERS
    # ========================================================================

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

    # ========================================================================
    # HARDWARE SPECIFICATION QUERIES
    # ========================================================================

    @property
    def hw_specs(self) -> dict:
        """Hardware specification dictionary from the overlay driver.

        :return: The full hw_specs dictionary.
        :rtype: dict
        """
        return self.overlay_driver.hw_specs

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

    # ========================================================================
    # MIX-MODE CONFIGURATION
    # ========================================================================

    def configure_dac_mix_mode(self, gen_index: int, label: str, freq_mhz: float) -> dict | None:
        """Configure the DAC Mix-Mode (Nyquist zone) for a generator.

        Silently returns ``None`` if the driver does not support mix-mode
        configuration (e.g. on mock overlays or older bitstreams).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param label: Modulation context ('drive' or 'readout').
        :type label: str
        :param freq_mhz: Target frequency in MHz.
        :type freq_mhz: float
        :return: Mix-mode info dictionary from the driver, or ``None`` if unavailable.
        :rtype: dict | None
        """
        try:
            mix_info = self.overlay_driver.configure_dac_mix_mode(gen_index, label, freq_mhz)
            if mix_info.get("changed"):
                self.logger.debug(
                    "DAC Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                    mix_info["nyquist_zone"],
                    mix_info["amd_zone"],
                    mix_info["tile"],
                    mix_info["block"],
                )
            return mix_info
        except (ValueError, AttributeError, TypeError, KeyError) as e:
            self.logger.debug("DAC Mix-mode config skipped: %s", e)
            return None

    def configure_adc_mix_mode(self, acq_index: int, freq_mhz: float) -> None:
        """Configure the ADC Mix-Mode (Nyquist zone) for an acquisition unit.

        Logs a warning and continues if the driver does not support mix-mode
        configuration.

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param freq_mhz: Target frequency in MHz.
        :type freq_mhz: float
        """
        try:
            mix_info = self.overlay_driver.configure_adc_mix_mode(acq_index=acq_index, freq_mhz=freq_mhz)
            if mix_info.get("changed"):
                self.logger.debug(
                    "ADC Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                    mix_info["nyquist_zone"],
                    mix_info["amd_zone"],
                    mix_info["tile"],
                    mix_info["block"],
                )
        except ValueError as e:
            self.logger.warning("ADC Mix-mode config skipped: %s", e)

    # ========================================================================
    # ERROR-CHECKED RESULT HANDLING
    # ========================================================================

    def check_result(
        self,
        result: object,
        *,
        operation: str,
        hint: str | None = None,
    ) -> object:
        """Check a driver return code and raise on error.

        Inspects the integer return code from a low-level driver method. Non-negative
        values pass through unchanged; negative values trigger a ``ConfigurationError``
        with a diagnostic hint looked up from ``ERROR_HINTS``.

        :param result: Raw return value from the low-level driver method (typically an
            integer status code).
        :type result: object
        :param operation: The name of the driver operation (used for error messages
            and hint lookups).
        :type operation: str
        :param hint: An explicit diagnostic hint to append to the error message,
            overriding the default ``ERROR_HINTS`` lookup.
        :type hint: str | None
        :return: The original ``result`` passed through unchanged on success.
        :rtype: object
        :raises ConfigurationError: If the result is a negative integer.
        """
        return handle_error_result(
            result,
            operation=operation,
            driver_name=self._driver_name,
            logger=self.logger,
            config_error=True,
            hint=hint,
            error_hints=ERROR_HINTS,
            error_exc=None,
        )


__all__ = ["LowLevelAccess"]
