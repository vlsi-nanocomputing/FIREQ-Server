"""Experiment orchestration for OverlayAdapter.

This module provides the ExperimentOps class that handles:
- High-level experiment setup and cleanup
- Sweep mode coordination
- Multi-acquisition orchestration
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cache import AdapterContext


class ExperimentOps:
    """Operation class for high-level experiment coordination.

    This class provides experiment-level orchestration, coordinating between
    trigger, generator, and acquisition operations.

    Attributes:
    -----------
    _ctx : AdapterContext
        Shared context containing ll, cache, logger, trigger, acquisition, and other dependencies.
    """

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize the ExperimentOps class.

        :param ctx: Shared adapter context with all dependencies.
        :type ctx: AdapterContext
        """
        self._ctx = ctx

    def prepare_sweep(self, mode: str, adc_indices: list[int]) -> None:
        """Prepare the experiment for sweep-mode operation.

        This configuration optimizes both acquisition and trigger hardware for
        multiple repeated experiments with consistent settings.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :param adc_indices: List of active ADC indices for the sweep.
        """
        self._ctx.logger.debug("Preparing experiment for sweep mode: mode=%s, adc_indices=%s", mode, adc_indices)
        self._ctx.acquisition.prepare_sweep(mode=mode, adc_indices=adc_indices)
        self._ctx.logger.debug("Experiment sweep preparation complete")

    def end_sweep(self) -> None:
        """Finalize the sweep-mode experiment and release resources.

        This must be called at the end of a sweep sequence to ensure all
        hardware resources are properly cleaned up.
        """
        self._ctx.logger.debug("Finalizing sweep mode experiment")
        self._ctx.acquisition.end_sweep()
        self._ctx.logger.debug("Experiment sweep finalization complete")


__all__ = ["ExperimentOps"]
