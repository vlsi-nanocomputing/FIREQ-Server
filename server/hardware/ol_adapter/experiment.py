"""Experiment orchestration for OverlayAdapter.

This module provides the ExperimentOps class that handles:
- High-level experiment setup and cleanup
- Sweep mode coordination
- Multi-acquisition orchestration
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging

    from .acquisition import AcquisitionOps
    from .cache import CacheContainers
    from .ll_access import LowLevelAccess
    from .trigger import TriggerOps


class ExperimentOps:
    """Operation class for high-level experiment coordination.

    This class provides experiment-level orchestration, coordinating between
    trigger, generator, and acquisition operations.

    Attributes:
    -----------
    _ll : LowLevelAccess
        Unified interface for low-level driver access and error handling.
    _logger : logging.Logger
        Logger instance for debug/error reporting.
    _cache : CacheContainers
        Shared cache with experiment state.
    _trigger : TriggerOps
        Trigger operations for experiment control.
    _acquisition : AcquisitionOps
        Acquisition operations for data retrieval.
    """

    def __init__(
        self,
        ll: LowLevelAccess,
        cache: CacheContainers,
        logger: logging.Logger,
        trigger: TriggerOps,
        acquisition: AcquisitionOps,
    ) -> None:
        """Initialize the ExperimentOps class.

        :param ll: Low-level driver access helper.
        :type ll: LowLevelAccess
        :param cache: Shared cache containers.
        :type cache: CacheContainers
        :param logger: Logger instance.
        :type logger: logging.Logger
        :param trigger: TriggerOps instance for trigger coordination.
        :type trigger: TriggerOps
        :param acquisition: AcquisitionOps instance for acquisition coordination.
        :type acquisition: AcquisitionOps
        """
        self._ll = ll
        self._cache = cache
        self._logger = logger
        self._trigger = trigger
        self._acquisition = acquisition

    def prepare_sweep(self, mode: str, adc_indices: list[int]) -> None:
        """Prepare the experiment for sweep-mode operation.

        This configuration optimizes both acquisition and trigger hardware for
        multiple repeated experiments with consistent settings.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :param adc_indices: List of active ADC indices for the sweep.
        """
        self._logger.debug("Preparing experiment for sweep mode: mode=%s, adc_indices=%s", mode, adc_indices)
        self._acquisition.prepare_sweep(mode=mode, adc_indices=adc_indices)
        self._logger.debug("Experiment sweep preparation complete")

    def end_sweep(self) -> None:
        """Finalize the sweep-mode experiment and release resources.

        This must be called at the end of a sweep sequence to ensure all
        hardware resources are properly cleaned up.
        """
        self._logger.debug("Finalizing sweep mode experiment")
        self._acquisition.end_sweep()
        self._logger.debug("Experiment sweep finalization complete")


__all__ = ["ExperimentOps"]
