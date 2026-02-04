"""Sweep mode optimization and state management.

This module provides the SweepOps class that handles:
- Sweep mode preparation (locking hardware configuration)
- Sweep mode finalization (releasing resources)
- ADC mode configuration during sweeps
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cache import AdapterContext


class SweepOps:
    """Operation class for sweep mode management.

    Handles sweep-specific optimizations including:
    - Pre-configuration of acquisition hardware for mode invariance
    - DMA engine preparation for optimized throughput
    - Memoization state reset between sweeps

    Attributes:
    -----------
    _ctx : AdapterContext
        Shared context containing ll, cache, logger, dma_engine, and other dependencies.
    """

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize SweepOps.

        :param ctx: Shared adapter context with all dependencies.
        :type ctx: AdapterContext
        """
        self._ctx = ctx

    def prepare_sweep(self, mode: str, adc_indices: list[int]) -> None:
        """Prepare acquisition IPs and DMA engine for sweep-optimized execution.

        This configuration locks the acquisition hardware into the specified mode to
        guarantee invariant behavior across the sweep duration.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :param adc_indices: List of active ADC indices involved in the sweep.
        """
        # Pre-config acquisition IPs
        for adc_i in adc_indices:
            acq = self._ctx.ll.get_acq(adc_i)
            if mode in ("decimated", "accumulated"):
                acq.set_decimated_output_type(mode)

        # Update active ADCs - frees buffers for ADCs not in use
        self._ctx.dma_engine.set_active_adcs(adc_indices)

        # Prepare DMA engine
        self._ctx.dma_engine.prepare_sweep(mode)
        self._ctx.cache.sweep_prepared = True
        # Reset memoized trigger shots so first acquisition in sweep configures trigger.
        self._ctx.cache.last_hw_shots = None

    def end_sweep(self) -> None:
        """Finalize the sweep execution and release DMA engine resources.

        This method must be called at the end of a sweep sequence to ensure the DMA
        engine correctly exits the optimized state and acquisition IPs are clean.
        """
        self._ctx.dma_engine.end_sweep()
        self._ctx.cache.sweep_prepared = False
        # Reset memorized trigger shots for next acquisition sequence.
        self._ctx.cache.last_hw_shots = None


__all__ = ["SweepOps"]
