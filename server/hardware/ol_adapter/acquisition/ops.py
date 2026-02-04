"""Main orchestrator for acquisition operations.

This module provides the AcquisitionOps class that coordinates all acquisition-related
operations by delegating to specialized submodule classes (ExecutionOps, SweepOps,
ModulationOps, TimingOps).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import numpy as np

from ....models.config_types import Modulation, TriggerCommand
from .execution import ExecutionOps
from .modulation import ModulationOps
from .sweep import SweepOps
from .timing import TimingOps

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .cache import AdapterContext


class AcquisitionOps:
    """Operation class for DMA acquisition control.

    This class handles all acquisition-related operations by orchestrating
    four specialized operation classes:
    - ExecutionOps: DMA execution with chunking and pipelining
    - SweepOps: Sweep mode optimization and state management
    - ModulationOps: DDS modulation and trigger listener configuration
    - TimingOps: Time-of-flight and duration timing configuration

    The public API delegates to these submodules transparently.

    Attributes:
    -----------
    _ctx : AdapterContext
        Shared context containing ll, cache, logger, dma_engine, trigger, and other dependencies.
    _execution : ExecutionOps
        Handles DMA execution and chunking.
    _sweep : SweepOps
        Handles sweep mode preparation and finalization.
    _modulation : ModulationOps
        Handles DDS and trigger configuration.
    _timing : TimingOps
        Handles timing parameter configuration.
    """

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize the AcquisitionOps orchestrator.

        :param ctx: Shared adapter context with all dependencies.
        :type ctx: AdapterContext
        """
        self._ctx = ctx
        self._execution = ExecutionOps(ctx)
        self._sweep = SweepOps(ctx)
        self._modulation = ModulationOps(ctx)
        self._timing = TimingOps(ctx)

    # ========== Acquisition Execution Delegation ==========

    def run_multi_acquisition(
        self,
        *,
        adc_indices: list[int],
        mode: Literal["raw", "decimated", "accumulated"],
        shots: int,
        samp_per_shot: int,
        timeout: float | None = 1.0,
        validate_chunk: bool = True,
    ) -> Iterator[dict[int, np.ndarray]]:
        """Execute a multi-ADC acquisition with automatic hardware chunking.

        Delegates to ExecutionOps.run_multi_acquisition().

        :param adc_indices: List of ADC indices to acquire from.
        :param mode: Acquisition mode.
        :param shots: Total number of shots to acquire.
        :param samp_per_shot: Number of samples per shot.
        :param timeout: Timeout for each hardware acquisition chunk in seconds.
        :param validate_chunk: If True, perform input validation and compute chunk sizes.
        :return: Iterator yielding data_dict for each chunk.
        """
        return self._execution.run_multi_acquisition(
            adc_indices=adc_indices,
            mode=mode,
            shots=shots,
            samp_per_shot=samp_per_shot,
            timeout=timeout,
            validate_chunk=validate_chunk,
        )

    # ========== Sweep Mode Delegation ==========

    def prepare_sweep(self, mode: str, adc_indices: list[int]) -> None:
        """Prepare acquisition IPs and DMA engine for sweep-optimized execution.

        Delegates to SweepOps.prepare_sweep().

        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :param adc_indices: List of active ADC indices involved in the sweep.
        """
        return self._sweep.prepare_sweep(mode, adc_indices)

    def end_sweep(self) -> None:
        """Finalize the sweep execution and release DMA engine resources.

        Delegates to SweepOps.end_sweep().
        """
        return self._sweep.end_sweep()

    # ========== Acquisition Modulation Delegation ==========

    def set_modulation(self, acq_index: int, mod: Modulation) -> dict:
        """Configure the DDS modulation parameters for an acquisition unit.

        Delegates to ModulationOps.set_modulation().

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param mod: Dictionary containing frequency and phase parameters.
        :type mod: Modulation
        :return: The applied configuration.
        :rtype: dict
        """
        return self._modulation.set_modulation(acq_index, mod)

    def set_trigger_listener(self, acq_index: int, trig: TriggerCommand) -> dict:
        """Configure which trigger channel the acquisition should listen to.

        Delegates to ModulationOps.set_trigger_listener().

        :param acq_index: Index of the target acquisition unit.
        :type acq_index: int
        :param trig: Dictionary defining the trigger type and source channel.
        :type trig: TriggerCommand
        :return: The applied trigger configuration.
        :rtype: dict
        """
        return self._modulation.set_trigger_listener(acq_index, trig)

    # ========== Timing Delegation ==========

    def set_timing(self, acq_index: int, tof: int, duration: int) -> dict:
        """Configure the timing parameters (Time of Flight and Duration).

        Delegates to TimingOps.set_timing().

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param tof: Time of Flight delay in clock cycles.
        :type tof: int
        :param duration: Acquisition duration in clock cycles.
        :type duration: int
        :return: The applied timing configuration.
        :rtype: dict
        """
        return self._timing.set_timing(acq_index, tof, duration)


__all__ = ["AcquisitionOps"]
