"""DMA acquisition execution with chunking and pipelining.

This module provides the ExecutionOps class that handles:
- Single and multi-shot hardware acquisitions
- Automatic chunking for large acquisitions
- Pipelined DMA transfers for optimal throughput
- Timeout management using SIGALRM (Unix) or polling (Windows)
"""

from __future__ import annotations

import signal
import time
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, NoReturn

import numpy as np

from ....models.exceptions import ConfigurationError

if TYPE_CHECKING:
    from .cache import AdapterContext


class ExecutionOps:
    """Operation class for DMA acquisition execution.

    Handles all hardware-level acquisition operations including:
    - Single and multi-shot DMA transfers
    - Automatic hardware chunking for large acquisitions
    - Pipelined execution for optimal throughput
    - Timeout mechanisms (signal-based on Unix, polling on Windows)

    Attributes:
    -----------
    _ctx : AdapterContext
        Shared context containing ll, cache, logger, dma_engine, trigger, and other dependencies.
    """

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize ExecutionOps.

        :param ctx: Shared adapter context with all dependencies.
        :type ctx: AdapterContext
        """
        self._ctx = ctx

    def compute_max_hw_shots(
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
        buffer_max = self._ctx.dma_engine.get_max_shots(mode, samp_per_shot, adc_index)
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
            if len(adc_indices) > len(self._ctx.ll.ol.hw_specs["acquisitions"]):
                raise ConfigurationError(
                    f"Requested {len(adc_indices)} ADCs, "
                    f"but only {len(self._ctx.ll.ol.hw_specs['acquisitions'])} available."
                )

        # Compute hardware buffer limits (Required for chunking logic)
        max_hw_shots = min(self.compute_max_hw_shots(mode, samp_per_shot, adc) for adc in adc_indices)

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
                self._ctx.logger.debug(f"Splitting {shots} shots into chunks of {max_hw_shots}")
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
                        if hw_shots != self._ctx.cache.last_hw_shots:
                            self._ctx.trigger.set_shots(hw_shots)
                            self._ctx.cache.last_hw_shots = hw_shots

                        # Pre-config ADCs (only needed outside sweep mode)
                        if not self._ctx.cache.sweep_prepared:
                            for adc_i in adc_indices:
                                acq = self._ctx.ll.get_acq(adc_i)
                                if mode in ("decimated", "accumulated"):
                                    acq.set_decimated_output_type(mode)

                        pending_buffer = self._ctx.dma_engine.arm_acquisition(
                            samp_per_shot=samp_per_shot,
                            shots_per_exp=hw_shots,
                            mode=mode,
                            adc_index=first_adc,
                        )
                        self._ctx.trigger.trigger_experiment()
                        pending_shots = hw_shots

                    # --- Complete current chunk: WAIT + COPY all ADCs ---
                    results: dict[int, np.ndarray] = {}

                    # First ADC: wait on pre-armed buffer
                    buffer_data = self._ctx.dma_engine.retrieve_acquisition(
                        buffer=pending_buffer,
                        timeout=None,  # Managed by outer context
                        skip_timeout=True,
                    )
                    results[first_adc] = buffer_data.copy()
                    fpga_wait_accum += self._ctx.dma_engine.last_dma_wait_s
                    dma_overhead_accum += self._ctx.dma_engine.last_invalidate_s

                    # Remaining ADCs: ARM + WAIT (data already in FIFO from trigger)
                    for adc_i in adc_indices[1:]:
                        buffer = self._ctx.dma_engine.arm_acquisition(
                            samp_per_shot=samp_per_shot,
                            shots_per_exp=pending_shots,
                            mode=mode,
                            adc_index=adc_i,
                        )
                        buffer_data = self._ctx.dma_engine.retrieve_acquisition(
                            buffer=buffer,
                            timeout=None,
                            skip_timeout=True,
                        )
                        results[adc_i] = buffer_data.copy()
                        fpga_wait_accum += self._ctx.dma_engine.last_dma_wait_s
                        dma_overhead_accum += self._ctx.dma_engine.last_invalidate_s

                    # --- Pre-start next chunk (before yield) for pipelined execution ---
                    if has_next:
                        next_hw_shots = min(max_hw_shots, next_remaining)
                        if next_hw_shots != self._ctx.cache.last_hw_shots:
                            self._ctx.trigger.set_shots(next_hw_shots)
                            self._ctx.cache.last_hw_shots = next_hw_shots

                        pending_buffer = self._ctx.dma_engine.arm_acquisition(
                            samp_per_shot=samp_per_shot,
                            shots_per_exp=next_hw_shots,
                            mode=mode,
                            adc_index=first_adc,
                        )
                        self._ctx.trigger.trigger_experiment()
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
        self._ctx.cache.last_timing_stats = {
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
        if shots != self._ctx.cache.last_hw_shots:
            self._ctx.trigger.set_shots(shots)
            self._ctx.cache.last_hw_shots = shots

        # Pre-config ADCs (only needed outside sweep mode)
        if not self._ctx.cache.sweep_prepared:
            for adc_i in adc_indices:
                acq = self._ctx.ll.get_acq(adc_i)
                if mode in ("decimated", "accumulated"):
                    acq.set_decimated_output_type(mode)

        # First ADC: arm before trigger
        first_adc = adc_indices[0]
        first_buffer = self._ctx.dma_engine.arm_acquisition(
            samp_per_shot=samp_per_shot,
            shots_per_exp=shots,
            mode=mode,
            adc_index=first_adc,
        )

        # Trigger
        self._ctx.trigger.trigger_experiment()

        # Retrieve first ADC (blocking)
        buffer_data = self._ctx.dma_engine.retrieve_acquisition(
            buffer=first_buffer,
            timeout=timeout,
            skip_timeout=skip_timeout,
        )
        results[first_adc] = buffer_data.copy()
        fpga_wait_s += self._ctx.dma_engine.last_dma_wait_s
        dma_overhead_s += self._ctx.dma_engine.last_invalidate_s

        # Remaining ADCs: arm + retrieve (data already captured in FIFO)
        for adc_i in adc_indices[1:]:
            buffer = self._ctx.dma_engine.arm_acquisition(
                samp_per_shot=samp_per_shot,
                shots_per_exp=shots,
                mode=mode,
                adc_index=adc_i,
            )
            buffer_data = self._ctx.dma_engine.retrieve_acquisition(
                buffer=buffer,
                timeout=timeout,
                skip_timeout=skip_timeout,
            )
            results[adc_i] = buffer_data.copy()
            fpga_wait_s += self._ctx.dma_engine.last_dma_wait_s
            dma_overhead_s += self._ctx.dma_engine.last_invalidate_s

        return results, fpga_wait_s, dma_overhead_s


__all__ = ["ExecutionOps"]
