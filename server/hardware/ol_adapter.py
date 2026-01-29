# file: fireq-utils/server/hardware/ol_adapter.py
"""Server-facing adapter layer for FIREQ hardware.

fireq_utils.server.hardware.ol_adapter
======================================

Purpose
-------
This module implements a *server-facing adapter layer* on top of the low-level
FIREQ hardware drivers exposed by `FIREQ_LL_API.overlay_driver.FIREQ_SoC`.

It applies the Adapter pattern to:
- expose an API for experiment execution,
- expose meaningful error logs,
- keep a coherent High-Level (HL) cache synchronized with Low-Level (LL) driver state.

Key design principles
---------------------
- Fail fast on configuration errors (never let invalid states reach hardware).
- Make HL-LL synchronization explicit and verifiable.
- Expose JSON-serializable outputs suitable for logging and remote control.

Limitations
-----------
- It assumes exclusive ownership of the underlying overlay.
- It assumes hardware configuration does not change outside this API.

Architecture
------------
The OverlayAdapter is composed from four mixin classes:
- ModulationMixin: Generator/acquisition modulation and trigger configuration
- TriggerMixin: Trigger generator operations (shots, delays, triggering)
- WaveMixin: Wave and envelope management, compilation, FIFO programming
- AcquisitionMixin: DMA acquisition execution, sweep mode, chunking
"""

import logging

from ..models.adapter_types import WaveEntry
from ..models.exceptions import ConfigurationError, HardwareStateError
from .adapter import AcquisitionMixin, ModulationMixin, TriggerMixin, WaveMixin
from .dma_engine import AcquisitionEngine
from .utils import ERROR_HINTS, handle_error_result


class OverlayAdapter(
    WaveMixin,
    ModulationMixin,
    TriggerMixin,
    AcquisitionMixin,
):
    """High-level adapter for FIREQ hardware control.

    This class provides a "server" interface on top of FIREQSoC,
    bundling together multiple low-level driver calls into coherent macro-operations.

    Responsibilities
    ----------------
    - Translate server commands into ordered hardware actions.
    - Maintain a High-Level (HL) cache of waves, envelopes, and FIFOs.
    - Enforce invariants before programming hardware.
    - Synchronize HL cache state with Low-Level (LL) driver state.
    - Centralize error handling and diagnostics.

    Statefulness
    ------------
    This adapter is intentionally stateful:
    - caches WaveEntry objects per generator,
    - tracks last programmed FIFO sequences,
    - remembers readout wave configuration,
    - accumulates timing statistics.

    This state is required to:
    - detect redundant operations safely,
    - enable fast paths correctly,
    - detect and stop inconsistencies early.

    Notes:
    - Wave IDs are treated as 128-bit Wave Definition Words (WDW) serialized in hex.
    - To program FIFO sequences, we ensure each WDW is present in wave memory (wave_id-based).
    - wave_id uniquely identifies a logical wave within a generator.
    - Replacement of existing waves is always explicit (replace=True).
    """

    # ERROR HINTS
    # user-friendly hints for common negative codes
    _ERROR_HINTS = ERROR_HINTS

    def __init__(self, ol: object, *, logger: logging.Logger | None = None) -> None:
        """Initialize the High-Level Adapter.

        :param ol: The low-level overlay driver instance.
        :type ol: fireq_soc
        :param logger: Optional logger instance for telemetry. If None, a default logger
            is created.
        :type logger: Optional[logging.Logger]
        """
        # Fail-fast sanity check:
        if not ol.is_healthy:
            raise HardwareStateError("Unexpected Error: overlay upload failed!")

        self.ol = ol
        self.logger = logger or logging.getLogger(__name__)

        # DMA engine (needed for run_experiment)
        if self.ol.dma is None or self.ol.axis_switch is None:
            raise HardwareStateError("DMA or AXI-Stream switch missing in overlay")

        # The DMA engine is constructed once as a long-lived resource.
        self.dma_engine = AcquisitionEngine(
            self.ol.dma,
            self.ol.axis_switch,
            logger=self.logger,
            hw_specs=self.ol.hw_specs,
        )

        # per-generator caches
        # Create a memory of the compiled WDW. Each wdw is accessible via the wave_id as key
        self._wave_store: dict[int, dict[str, WaveEntry]] = {}
        # Create a memory of the last used experiment
        self._last_fifo: dict[int, list[str]] = {}
        # Create a memory for readout waves (one per generator)
        self._readout_wave_store: dict[int, WaveEntry] = {}

        # Timing for statistics (detailed breakdown).
        self.last_timing_stats = {
            "total_ms": 0.0,
            "fpga_wait_ms": 0.0,
            "dma_overhead_ms": 0.0,
            "sw_overhead_ms": 0.0,
        }
        self._sweep_prepared = False
        # Memoization for trigger shots to avoid redundant HW writes in chunked acquisitions.
        self._last_hw_shots: int | None = None
        # Track acquisition trigger channels for diagnostics.
        self._acq_trigger_channel: dict[int, int] = {}
        # Track high water mark for trigger generator drive FIFOs (per channel).
        # Maps channel_index -> last written FIFO index (1-based, inclusive).
        # Used for lazy FIFO cleanup optimization in tg_program_delays.
        self._tg_drive_hwm: dict[int, int] = {}

    # ------------------------------------------------------------
    # Pass-through: everything not defined here goes to self.ol
    # ------------------------------------------------------------
    def __getattr__(self, name: str) -> object:
        """Delegate attribute access to the underlying low-level overlay driver.

        This method implements the Proxy pattern, allowing the adapter to transparently
        expose the full API of the wrapped ``fireq_soc`` instance. Any attribute or method
        not explicitly defined in this adapter is automatically forwarded to the hardware driver.

        Therefore, the "expert" user can directly use the underlying driver methods. The only purpose
        is to speedup debugging operation and ease developers' work.

        :param name: The name of the attribute to retrieve.
        :type name: str
        :return: The attribute value from the low-level driver.
        :rtype: object
        :raises AttributeError: If the attribute is not found in either the adapter or the underlying driver.
        """
        return getattr(self.ol, name)

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------
    def _call(
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
            error_hints=self._ERROR_HINTS,
            error_exc=None,
        )

    def _get_gen(self, gen_index: int) -> object:
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
            return self.ol.generators[int(gen_index)]
        except Exception as e:
            raise ConfigurationError(f"Invalid gen_index={gen_index}") from e

    def _get_acq(self, acq_index: int) -> object:
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
            return self.ol.acquisitions[int(acq_index)]
        except Exception as e:
            raise ConfigurationError(f"Invalid acq_index={acq_index}") from e

    def _get_trig(self) -> object:
        """Retrieve the low-level Trigger Generator driver.

        Validates the existence of the trigger IP in the current overlay configuration
        before returning it.

        :return: The low-level trigger driver instance.
        :rtype: object
        :raises ConfigurationError: If no trigger generator is available.
        """
        if self.ol.trigger is None:
            raise ConfigurationError("No trigger generator available in overlay")
        return self.ol.trigger

    def _dac_sr_mhz(self) -> float:
        """Retrieve the DAC sampling rate from hardware specifications and convert it to MHz.

        :return: The DAC sampling rate in MHz.
        :rtype: float
        """
        return float(self.ol.hw_specs["summary"]["dac_sr_hz"]) / 1e6

    def _adc_sr_mhz(self) -> float:
        """Retrieve the ADC sampling rate from hardware specifications and convert it to MHz.

        :return: The ADC sampling rate in MHz.
        :rtype: float
        """
        return float(self.ol.hw_specs["summary"]["adc_sr_hz"]) / 1e6


# Re-export for backwards compatibility
__all__ = ["OverlayAdapter"]
