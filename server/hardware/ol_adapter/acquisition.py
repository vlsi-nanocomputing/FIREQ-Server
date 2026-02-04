"""Acquisition operations for OverlayAdapter.

This module provides the AcquisitionOps class that handles:
- Single and multi-shot DMA acquisition execution
- Automatic hardware chunking for large acquisitions
- Sweep mode optimization
- Pipelined data retrieval
- Acquisition DDS modulation
- Timing configuration
"""

from __future__ import annotations

import signal
import time
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, NoReturn

import numpy as np

from ...models.config_types import Modulation, TriggerCommand
from ...models.exceptions import ConfigurationError
from .ll_access import LowLevelAccess

if TYPE_CHECKING:
    import logging

    from .cache import CacheContainers
    from .trigger import TriggerOps


class AcquisitionOps:
    """Operation class for DMA acquisition control.

    This class handles all acquisition-related operations, including:
    - DMA-based multi-ADC data acquisition
    - Automatic hardware chunking for large acquisitions
    - Pipelined execution for optimal throughput
    - Sweep mode optimization
    - Acquisition DDS modulation and timing

    Attributes:
    -----------
    _ll : LowLevelAccess
        Unified interface for low-level driver access and error handling.
    _logger : logging.Logger
        Logger instance for debug/error reporting.
    _cache : CacheContainers
        Shared cache with acquisition and modulation state.
    _trigger : TriggerOps
        Trigger operations for shot configuration and experiment triggering.
    """

    def __init__(
        self,
        ll: LowLevelAccess,
        cache: CacheContainers,
        logger: logging.Logger,
        dma_engine: object,
        trigger: TriggerOps,
    ) -> None:
        """Initialize the AcquisitionOps class.

        :param ll: Low-level driver access helper.
        :type ll: LowLevelAccess
        :param cache: Shared cache containers.
        :type cache: CacheContainers
        :param logger: Logger instance.
        :type logger: logging.Logger
        :param dma_engine: AcquisitionEngine instance for DMA control.
        :type dma_engine: object
        :param trigger: TriggerOps instance for trigger coordination.
        :type trigger: TriggerOps
        """
        self._ll = ll
        self._cache = cache
        self._logger = logger
        self.dma_engine = dma_engine
        self._trigger = trigger

    # ========== Acquisition Execution ==========

    def _compute_max_hw_shots(
        self,
        mode: str,
        samp_per_shot: int,
        adc_index: int,
    ) -> int:
        """Compute the maximum number of shots executable in a single hardware run.

        This method determines the safe upper bound by calculating the intersection
        of two hardware constraints:
        1. The Trigger Generator's repetition counter limit (10-bit register = 1024 shots).
        2. The available DMA buffer capacity for the specified acquisition mode and length.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated').
        :param samp_per_shot: Number of samples per individual shot.
        :param adc_index: Index of the target ADC.
        :return: The maximum allowable shots for a single atomic execution.
        """
        TRIGGER_MAX_SHOTS = 1024  # 10-bit register limit
        buffer_max = self.dma_engine.get_max_shots(mode, samp_per_shot, adc_index)
        return min(TRIGGER_MAX_SHOTS, buffer_max)

    @contextmanager
    def _dma_timeout_context(self, timeout_sec: float | None) -> Iterator[None]:
        """Context manager for DMA timeout using SIGALRM (Unix only).

        Sets up a signal-based timeout once for an entire multi-acquisition
        operation, avoiding per-chunk syscall overhead.

        :param timeout_sec: Total timeout in seconds. If None or <= 0, no timeout.
        :yields: None
        :raises TimeoutError: If the timeout expires during the context.
        """
        if timeout_sec is None or timeout_sec <= 0:
            yield
            return

        if not hasattr(signal, "SIGALRM"):
            yield
            return

        def _timeout_handler(signum: int, frame: types.FrameType | None) -> NoReturn:
            raise TimeoutError("DMA acquisition timeout")

        old_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout_sec)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, old_handler)

    def prepare_sweep(self, mode: str, adc_indices: list[int]) -> None:
        """Prepare acquisition IPs and DMA engine for sweep-optimized execution.

        This configuration locks the acquisition hardware into the specified mode to
        guarantee invariant behavior across the sweep duration.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :param adc_indices: List of active ADC indices involved in the sweep.
        """
        # Pre-config acquisition IPs
        for adc_i in adc_indices:
            acq = self._ll.get_acq(adc_i)
            if mode in ("decimated", "accumulated"):
                acq.set_decimated_output_type(mode)

        # Update active ADCs - frees buffers for ADCs not in use
        self.dma_engine.set_active_adcs(adc_indices)

        # Prepare DMA engine
        self.dma_engine.prepare_sweep(mode)
        self._cache.sweep_prepared = True
        # Reset memoized trigger shots so first acquisition in sweep configures trigger.
        self._cache.last_hw_shots = None

    def end_sweep(self) -> None:
        """Finalize the sweep execution and release DMA engine resources.

        This method must be called at the end of a sweep sequence to ensure the DMA
        engine correctly exits the optimized state and acquisition IPs are clean.
        """
        self.dma_engine.end_sweep()
        self._cache.sweep_prepared = False
        # Reset memoized trigger shots for next acquisition sequence.
        self._cache.last_hw_shots = None

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

        If the requested number of shots exceeds hardware repetition capacity, the
        acquisition is transparently split into multiple hardware runs and reassembled.

        This method yields raw DMA buffers. Client computes valid_words from request params.

        :param adc_indices: List of ADC indices to acquire from.
        :param mode: Acquisition mode.
        :param shots: Total number of shots to acquire.
        :param samp_per_shot: Number of samples per shot.
        :param timeout: Timeout for each hardware acquisition chunk in seconds.
        :param validate_chunk: If True, perform input validation and compute chunk sizes.
        :return: Iterator yielding data_dict for each chunk.
        """
        # Start total routine timer for performance analysis
        t_start_routine = time.perf_counter()
        fpga_wait_accum = 0.0
        dma_overhead_accum = 0.0

        if validate_chunk:
            # --- Input Validation ---
            if not adc_indices:
                raise ConfigurationError("No ADC indices provided.")
            if len(adc_indices) > len(self._ll.ol.hw_specs["acquisitions"]):
                raise ConfigurationError(
                    f"Requested {len(adc_indices)} ADCs, "
                    f"but only {len(self._ll.ol.hw_specs['acquisitions'])} available."
                )

        # Compute hardware buffer limits (Required for chunking logic)
        max_hw_shots = min(self._compute_max_hw_shots(mode, samp_per_shot, adc) for adc in adc_indices)

        if validate_chunk:
            if max_hw_shots < 1:
                raise ConfigurationError(
                    f"Impossible configuration: The requested single shot duration ({samp_per_shot} samples) "
                    f"is larger than the entire hardware buffer available for mode '{mode}'. "
                    "Try reducing the acquisition duration."
                )

        # Calculate total timeout for all chunks
        num_chunks = (shots + max_hw_shots - 1) // max_hw_shots
        total_timeout = timeout * num_chunks if timeout else None

        # Wrap all acquisitions with single timeout context
        with self._dma_timeout_context(total_timeout):
            # --- Case 1: Single Hardware Acquisition ---
            if shots <= max_hw_shots:
                data, fpga_s, dma_s = self._run_single_hw_acquisition(
                    adc_indices=adc_indices,
                    mode=mode,
                    shots=shots,
                    samp_per_shot=samp_per_shot,
                    timeout=None,  # Managed by outer context
                    skip_timeout=True,
                )
                fpga_wait_accum += fpga_s
                dma_overhead_accum += dma_s
                yield data

            # --- Case 2: Multiple Hardware Acquisitions (Pipelined Chunking) ---
            else:
                self._logger.debug(f"Splitting {shots} shots into chunks of {max_hw_shots}")
                remaining = shots
                first_adc = adc_indices[0]

                # Pipelined state: buffer from pre-armed transfer (None if not pre-started)
                pending_buffer: object | None = None
                pending_shots: int = 0

                while remaining > 0:
                    hw_shots = min(max_hw_shots, remaining)
                    next_remaining = remaining - hw_shots
                    has_next = next_remaining > 0

                    # --- Start current chunk (if not pre-started) ---
                    if pending_buffer is None:
                        # First iteration: configure trigger, ARM first ADC, TRIGGER
                        if hw_shots != self._cache.last_hw_shots:
                            self._trigger.set_shots(hw_shots)
                            self._cache.last_hw_shots = hw_shots

                        # Pre-config ADCs (only needed outside sweep mode)
                        if not self._cache.sweep_prepared:
                            for adc_i in adc_indices:
                                acq = self._ll.get_acq(adc_i)
                                if mode in ("decimated", "accumulated"):
                                    acq.set_decimated_output_type(mode)

                        pending_buffer = self.dma_engine.arm_acquisition(
                            samp_per_shot=samp_per_shot,
                            shots_per_exp=hw_shots,
                            mode=mode,
                            adc_index=first_adc,
                        )
                        self._trigger.trigger_experiment()
                        pending_shots = hw_shots

                    # --- Complete current chunk: WAIT + COPY all ADCs ---
                    results: dict[int, np.ndarray] = {}

                    # First ADC: wait on pre-armed buffer
                    buffer_data = self.dma_engine.retrieve_acquisition(
                        buffer=pending_buffer,
                        timeout=None,  # Managed by outer context
                        skip_timeout=True,
                    )
                    results[first_adc] = buffer_data.copy()
                    fpga_wait_accum += self.dma_engine.last_dma_wait_s
                    dma_overhead_accum += self.dma_engine.last_invalidate_s

                    # Remaining ADCs: ARM + WAIT (data already in FIFO from trigger)
                    for adc_i in adc_indices[1:]:
                        buffer = self.dma_engine.arm_acquisition(
                            samp_per_shot=samp_per_shot,
                            shots_per_exp=pending_shots,
                            mode=mode,
                            adc_index=adc_i,
                        )
                        buffer_data = self.dma_engine.retrieve_acquisition(
                            buffer=buffer,
                            timeout=None,
                            skip_timeout=True,
                        )
                        results[adc_i] = buffer_data.copy()
                        fpga_wait_accum += self.dma_engine.last_dma_wait_s
                        dma_overhead_accum += self.dma_engine.last_invalidate_s

                    # --- Pre-start next chunk (before yield) for pipelined execution ---
                    if has_next:
                        next_hw_shots = min(max_hw_shots, next_remaining)
                        if next_hw_shots != self._cache.last_hw_shots:
                            self._trigger.set_shots(next_hw_shots)
                            self._cache.last_hw_shots = next_hw_shots

                        pending_buffer = self.dma_engine.arm_acquisition(
                            samp_per_shot=samp_per_shot,
                            shots_per_exp=next_hw_shots,
                            mode=mode,
                            adc_index=first_adc,
                        )
                        self._trigger.trigger_experiment()
                        pending_shots = next_hw_shots
                    else:
                        pending_buffer = None  # No more chunks

                    # --- Yield current chunk (DMA for next chunk already running) ---
                    yield results

                    remaining = next_remaining

        # --- Performance Calculation ---
        t_end_routine = time.perf_counter()
        total_duration = t_end_routine - t_start_routine
        sw_overhead = total_duration - fpga_wait_accum - dma_overhead_accum

        # Update statistics for telemetry (detailed breakdown)
        self._cache.last_timing_stats = {
            "total_ms": total_duration * 1000.0,
            "fpga_wait_ms": fpga_wait_accum * 1000.0,
            "dma_overhead_ms": dma_overhead_accum * 1000.0,
            "sw_overhead_ms": sw_overhead * 1000.0,
        }

    def _run_single_hw_acquisition(
        self,
        *,
        adc_indices: list[int],
        mode: Literal["raw", "decimated", "accumulated"],
        shots: int,
        samp_per_shot: int,
        timeout: float | None = 1.0,
        skip_timeout: bool = False,
    ) -> tuple[dict[int, np.ndarray], float, float]:
        """Execute a single hardware acquisition cycle (ARM → TRIGGER → RETRIEVE).

        Returns raw DMA buffers. Client computes valid_words from request params.

        :param adc_indices: List of ADC indices.
        :param mode: Acquisition mode.
        :param shots: Number of shots for this specific hardware run.
        :param samp_per_shot: Samples per shot.
        :param timeout: Timeout in seconds (used only if skip_timeout=False).
        :param skip_timeout: If True, skip internal DMA timeout (caller manages externally).
        :return: (data_dict, fpga_wait_s, dma_overhead_s).
        """
        results: dict[int, np.ndarray] = {}
        fpga_wait_s = 0.0
        dma_overhead_s = 0.0

        # Memoize trigger shots to skip redundant HW writes in chunked acquisitions.
        if shots != self._cache.last_hw_shots:
            self._trigger.set_shots(shots)
            self._cache.last_hw_shots = shots

        # Pre-config ADCs (only needed outside sweep mode)
        if not self._cache.sweep_prepared:
            for adc_i in adc_indices:
                acq = self._ll.get_acq(adc_i)
                if mode in ("decimated", "accumulated"):
                    acq.set_decimated_output_type(mode)

        # First ADC: arm before trigger
        first_adc = adc_indices[0]
        first_buffer = self.dma_engine.arm_acquisition(
            samp_per_shot=samp_per_shot,
            shots_per_exp=shots,
            mode=mode,
            adc_index=first_adc,
        )

        # Trigger
        self._trigger.trigger_experiment()

        # Retrieve first ADC (blocking)
        buffer_data = self.dma_engine.retrieve_acquisition(
            buffer=first_buffer,
            timeout=timeout,
            skip_timeout=skip_timeout,
        )
        results[first_adc] = buffer_data.copy()
        fpga_wait_s += self.dma_engine.last_dma_wait_s
        dma_overhead_s += self.dma_engine.last_invalidate_s

        # Remaining ADCs: arm + retrieve (data already captured in FIFO)
        for adc_i in adc_indices[1:]:
            buffer = self.dma_engine.arm_acquisition(
                samp_per_shot=samp_per_shot,
                shots_per_exp=shots,
                mode=mode,
                adc_index=adc_i,
            )
            buffer_data = self.dma_engine.retrieve_acquisition(
                buffer=buffer,
                timeout=timeout,
                skip_timeout=skip_timeout,
            )
            results[adc_i] = buffer_data.copy()
            fpga_wait_s += self.dma_engine.last_dma_wait_s
            dma_overhead_s += self.dma_engine.last_invalidate_s

        return results, fpga_wait_s, dma_overhead_s

    # ========== Acquisition Modulation ==========

    def set_modulation(self, acq_index: int, mod: Modulation) -> dict:
        """Configure the DDS modulation parameters for an acquisition unit.

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param mod: Dictionary containing frequency and phase parameters.
        :type mod: Modulation
        :return: The applied configuration.
        :rtype: dict
        """
        freq_mhz = mod["frequency_mhz"]
        phase = mod["phase"]

        self._logger.debug(
            "set_modulation: acq=%d frequency=%s phase=%s",
            acq_index,
            freq_mhz,
            phase,
        )
        unit = self._ll.get_acq(acq_index)

        # Configure Mix-Mode via overlay
        try:
            mix_info = self._ll.ol.configure_adc_mix_mode(acq_index=acq_index, freq_mhz=freq_mhz)
            if mix_info.get("changed"):
                self._logger.debug(
                    "ADC Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                    mix_info["nyquist_zone"],
                    mix_info["amd_zone"],
                    mix_info["tile"],
                    mix_info["block"],
                )
        except ValueError as e:
            self._logger.warning(f"ADC Mix-mode config skipped: {e}")

        self._ll.call(
            unit.set_acquisition_dds_parameters(
                frequency=freq_mhz,
                phase=phase,
                adc_samplerate=self._ll.adc_sr_mhz(),
            ),
            operation="set_acquisition_dds_parameters",
            driver_name="AcquisitionDriver",
            config_error=True,
        )
        self._logger.debug("set_modulation: done acq=%d", acq_index)

        return {
            "acq_index": acq_index,
            "frequency_mhz": freq_mhz,
            "phase": phase,
        }

    def set_trigger_listener(self, acq_index: int, trig: TriggerCommand) -> dict:
        """Configure which trigger channel the acquisition should listen to.

        :param acq_index: Index of the target acquisition unit.
        :type acq_index: int
        :param trig: Dictionary defining the trigger type and source channel.
        :type trig: TriggerCommand
        :return: The applied trigger configuration.
        :rtype: dict
        """
        channel = trig["channel"]

        self._logger.debug("set_trigger_listener: acq=%d channel=%s", acq_index, channel)
        unit = self._ll.get_acq(acq_index)

        self._ll.call(
            unit.set_trigger_channel(channel=channel),
            operation="set_trigger_channel",
            driver_name="AcquisitionDriver",
            config_error=True,
        )

        if channel == 0:
            self._logger.debug("Acquisition %d is deaf to any trigger!", acq_index)
        else:
            self._logger.debug(
                "Acquisition %d listens to trigger_word channel %d",
                acq_index,
                channel,
            )
        self._cache.acq_trigger_channel[int(acq_index)] = int(channel)

        return {
            "acq_index": acq_index,
            "channel": channel,
        }

    def set_timing(self, acq_index: int, tof: int, duration: int) -> dict:
        """Configure the timing parameters (Time of Flight and Duration).

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param tof: Time of Flight delay in clock cycles.
        :type tof: int
        :param duration: Acquisition duration in clock cycles.
        :type duration: int
        :return: The applied timing configuration.
        :rtype: dict
        """
        self._logger.debug("set_timing: acq_index=%d tof=%d duration=%d", acq_index, tof, duration)
        acq = self._ll.get_acq(acq_index)

        self._ll.call(
            acq.set_acquisition_duration(duration),
            operation="set_acquisition_duration",
            driver_name="AcquisitionDriver",
            config_error=True,
        )

        self._ll.call(
            acq.set_time_of_flight(tof),
            operation="set_time_of_flight",
            driver_name="AcquisitionDriver",
            config_error=True,
        )
        return {
            "acq_index": acq_index,
            "tof": tof,
            "duration": duration,
        }


__all__ = ["AcquisitionOps"]
