"""High-level composition-based adapter for FIREQ hardware.

This module implements the OverlayAdapter using flat operation classes.
"""

import logging

from ...models.exceptions import HardwareStateError
from ..dma_engine import DMAEngine
from ._acq_ops import AcquisitionOps
from ._gen_ops import GeneratorOps
from ._low_level_access import LowLevelAccess
from ._trigger_gen_ops import TriggerGeneratorOps


class _ExperimentProxy:
    """Thin namespace to preserve ``adapter.experiment.prepare_sweep()`` API.

    Delegates to AcquisitionOps methods with added logging.
    """

    def __init__(self, acq: AcquisitionOps, logger: logging.Logger) -> None:
        self._acq = acq
        self._logger = logger

    def prepare_sweep(self, mode: str, acq_indices: list[int]) -> None:
        """Prepare the experiment for sweep-mode operation.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :type mode: str
        :param acq_indices: List of active acquisition unit indices for the sweep.
        :type acq_indices: list[int]
        """
        self._logger.debug("Preparing experiment for sweep mode: mode=%s, acq_indices=%s", mode, acq_indices)
        self._acq.prepare_sweep(mode=mode, acq_indices=acq_indices)
        self._logger.debug("Experiment sweep preparation complete")

    def end_sweep(self) -> None:
        """Finalize the sweep-mode experiment and release resources."""
        self._logger.debug("Finalizing sweep mode experiment")
        self._acq.end_sweep()
        self._logger.debug("Experiment sweep finalization complete")


class OverlayAdapter:
    """High-level adapter for FIREQ hardware control using flat operation classes.

    This class composes three operation classes and an experiment proxy to provide
    a server-facing interface on top of the low-level FIREQ hardware drivers:

    - GeneratorOps: Wave management, envelope upload, FIFO, modulation, trigger
    - TriggerGeneratorOps: Trigger generator control
    - AcquisitionOps: DMA acquisition, sweep, modulation, trigger, timing

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

    Each operation class owns its own state. No shared mutable containers.

    Attributes
    ----------
    generator : GeneratorOps
        Wave and generator modulation operations.
    trigger : TriggerGeneratorOps
        Trigger generator operations.
    acquisition : AcquisitionOps
        DMA acquisition operations.
    experiment : _ExperimentProxy
        High-level experiment orchestration.
    """

    def __init__(self, overlay_driver: object, *, logger: logging.Logger | None = None) -> None:
        """Initialize the High-Level Adapter with flat operation classes.

        Each operation class receives only its required dependencies (no shared
        context object). Cross-dependencies are resolved via explicit constructor
        parameters.

        :param overlay_driver: The low-level overlay driver instance.
        :type overlay_driver: fireq_soc
        :param logger: Optional logger instance for telemetry. If None, a default logger
            is created.
        :type logger: logging.Logger | None
        """
        # Fail-fast sanity check:
        if not overlay_driver.is_healthy:
            raise HardwareStateError("Unexpected Error: overlay upload failed!")

        self.overlay_driver = overlay_driver
        self.logger = logger or logging.getLogger(__name__)

        # DMA engine (needed for acquisition)
        if self.overlay_driver.dma is None or self.overlay_driver.axis_switch is None:
            raise HardwareStateError("DMA or AXI-Stream switch missing in overlay")

        dma_engine = DMAEngine(
            self.overlay_driver.dma,
            self.overlay_driver.axis_switch,
            logger=self.logger,
            hw_specs=self.overlay_driver.hw_specs,
        )

        # Each ops class gets its own LowLevelAccess with the correct driver_name
        # so that error messages automatically include the right driver context.
        ll_gen = LowLevelAccess(self.overlay_driver, self.logger, driver_name="GeneratorDriver")
        ll_acq = LowLevelAccess(self.overlay_driver, self.logger, driver_name="AcquisitionDriver")
        ll_trig = LowLevelAccess(self.overlay_driver, self.logger, driver_name="TriggerGeneratorDriver")

        # Compose flat operation classes with explicit dependencies
        self.trigger = TriggerGeneratorOps(ll_trig, self.logger)
        self.generator = GeneratorOps(ll_gen, self.logger)
        self.acquisition = AcquisitionOps(ll_acq, self.logger, dma_engine, self.trigger)
        self.experiment = _ExperimentProxy(self.acquisition, self.logger)

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
        return getattr(self.overlay_driver, name)

    # ========== Public Properties ==========

    @property
    def last_timing_stats(self) -> dict:
        """Retrieve the last timing statistics from an acquisition.

        :return: Dictionary with timing breakdown (total_ms, fpga_wait_ms, dma_overhead_ms, sw_overhead_ms).
        :rtype: dict
        """
        return self.acquisition.last_timing_stats

    @property
    def acq_trigger_channels(self) -> dict[int, int]:
        """Mapping of acquisition IP index to its currently assigned trigger channel.

        A channel value of 0 means the acquisition unit is deaf (not listening).

        :return: Copy of the trigger channel assignment map.
        :rtype: dict[int, int]
        """
        return self.acquisition.acq_trigger_channels


__all__ = ["OverlayAdapter"]
