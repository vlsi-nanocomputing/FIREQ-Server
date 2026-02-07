"""DMA acquisition orchestrator with chunking and pipelining.

This module provides the DMAOrchestrator class that handles:
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


class DMAOrchestrator:
    """Orchestrator for DMA-based acquisition.

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
        """Initialize DMAOrchestrator.

        :param ctx: Shared adapter context with all dependencies.
        :type ctx: AdapterContext
        """
        self._ctx = ctx

    # ========================================================================
    # PUBLIC METHODS
    # ========================================================================

    def compute_max_hw_shots(
        self,
        mode: str,
        samp_per_shot: int,
        acq_index: int,
    ) -> int:
        """Compute the maximum number of shots executable in a single hardware run.

        This method determines the safe upper bound by calculating the intersection
        of two hardware constraints:
        1. The Trigger Generator's repetition counter limit (10-bit register = 1024 shots).
        2. The available DMA buffer capacity for the specified acquisition mode and length.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated').
        :param samp_per_shot: Number of samples per individual shot.
        :param acq_index: Index of the acquisition unit.
        :return: The maximum allowable shots for a single atomic execution.
        """
        TRIGGER_MAX_SHOTS = 1024  # 10-bit register limit, hardcoded
        buffer_max = self._ctx.dma_engine.get_max_shots(mode, samp_per_shot, acq_index)
        return min(TRIGGER_MAX_SHOTS, buffer_max)

    def run_multi_acquisition(
        self,
        *,
        acq_indices: list[int],
        mode: Literal["raw", "decimated", "accumulated"],
        shots: int,
        samp_per_shot: int,
        timeout: float | None = 1.0,
        validate_chunk: bool = True,
    ) -> Iterator[dict[int, np.ndarray]]:
        """Execute a multi-acquisition with automatic hardware chunking.

        If the requested number of shots exceeds hardware repetition capacity, the
        acquisition is transparently split into multiple hardware runs and reassembled.

        This method yields raw DMA buffers. Client computes valid_words from request params.

        :param acq_indices: List of acquisition unit indices to acquire from.
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
            if not acq_indices:
                raise ConfigurationError("No acquisition unit indices provided.")
            if len(acq_indices) > len(self._ctx.ll.overlay_driver.hw_specs["acquisitions"]):
                raise ConfigurationError(
                    f"Requested {len(acq_indices)} acquisition units, "
                    f"but only {len(self._ctx.ll.overlay_driver.hw_specs['acquisitions'])} available."
                )

        # Compute hardware buffer limits (Required for chunking logic)
        max_hw_shots = min(self.compute_max_hw_shots(mode, samp_per_shot, acq) for acq in acq_indices)

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
                    acq_indices=acq_indices,
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
                first_acq = acq_indices[0]

                # Pipelined state: buffer from pre-armed transfer (None if not pre-started)
                pending_buffer: object | None = None
                pending_shots: int = 0

                while remaining > 0:
                    hw_shots = min(max_hw_shots, remaining)
                    next_remaining = remaining - hw_shots
                    has_next = next_remaining > 0

                    # --- Start current chunk (if not pre-started) ---
                    if pending_buffer is None:
                        # First iteration: configure trigger, ARM first Acquisition IP, TRIGGER
                        if hw_shots != self._ctx.cache.last_hw_shots:
                            self._ctx.trigger.set_shots(hw_shots)
                            self._ctx.cache.last_hw_shots = hw_shots

                        self._configure_acq_output_mode(acq_indices, mode)

                        pending_buffer = self._ctx.dma_engine.arm_acquisition(
                            samp_per_shot=samp_per_shot,
                            shots_per_exp=hw_shots,
                            mode=mode,
                            acq_ip_index=first_acq,
                        )
                        self._ctx.trigger.trigger_experiment()
                        pending_shots = hw_shots

                    # --- Complete current chunk: WAIT + COPY all AcquisitionIPs ---
                    results: dict[int, np.ndarray] = {}

                    # First acquisition unit: wait on pre-armed buffer
                    buffer_data = self._ctx.dma_engine.retrieve_acquisition(
                        buffer=pending_buffer,
                        timeout=None,  # Managed by outer context
                        skip_timeout=True,
                    )
                    results[first_acq] = buffer_data.copy()
                    fpga_wait_accum += self._ctx.dma_engine.last_dma_wait_s
                    dma_overhead_accum += self._ctx.dma_engine.last_invalidate_s

                    # Remaining acquisition units: ARM + WAIT (data already in FIFO from trigger)
                    for acq_i in acq_indices[1:]:
                        buffer = self._ctx.dma_engine.arm_acquisition(
                            samp_per_shot=samp_per_shot,
                            shots_per_exp=pending_shots,
                            mode=mode,
                            acq_ip_index=acq_i,
                        )
                        buffer_data = self._ctx.dma_engine.retrieve_acquisition(
                            buffer=buffer,
                            timeout=None,
                            skip_timeout=True,
                        )
                        results[acq_i] = buffer_data.copy()
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
                            acq_ip_index=first_acq,
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

    # ========================================================================
    # INTERNAL HELPERS
    # ========================================================================

    def _configure_acq_output_mode(self, acq_indices: list[int], mode: str) -> None:
        """Pre-configure acquisition IP output type when not in sweep mode.

        In sweep mode the output type is already locked by SweepOps.prepare_sweep(),
        so this step is skipped to avoid redundant hardware writes.

        :param acq_indices: List of acquisition unit indices to configure.
        :type acq_indices: list[int]
        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :type mode: str
        """
        if not self._ctx.cache.sweep_prepared:
            for acq_i in acq_indices:
                acq = self._ctx.ll.get_acq(acq_i)
                if mode in ("decimated", "accumulated"):
                    acq.set_decimated_output_type(mode)

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

    def _run_single_hw_acquisition(
        self,
        *,
        acq_indices: list[int],
        mode: Literal["raw", "decimated", "accumulated"],
        shots: int,
        samp_per_shot: int,
        timeout: float | None = 1.0,
        skip_timeout: bool = False,
    ) -> tuple[dict[int, np.ndarray], float, float]:
        """Execute a single hardware acquisition cycle (ARM -> TRIGGER -> RETRIEVE).

        Returns raw DMA buffers. Client computes valid_words from request params.

        :param acq_indices: List of acquisition unit indices.
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

        self._configure_acq_output_mode(acq_indices, mode)

        # First acquisition unit: arm before trigger
        first_acq = acq_indices[0]
        first_buffer = self._ctx.dma_engine.arm_acquisition(
            samp_per_shot=samp_per_shot,
            shots_per_exp=shots,
            mode=mode,
            acq_ip_index=first_acq,
        )

        # Trigger
        self._ctx.trigger.trigger_experiment()

        # Retrieve first acquisition unit (blocking)
        buffer_data = self._ctx.dma_engine.retrieve_acquisition(
            buffer=first_buffer,
            timeout=timeout,
            skip_timeout=skip_timeout,
        )
        results[first_acq] = buffer_data.copy()
        fpga_wait_s += self._ctx.dma_engine.last_dma_wait_s
        dma_overhead_s += self._ctx.dma_engine.last_invalidate_s

        # Remaining acquisition units: arm + retrieve (data already captured in FIFO)
        for acq_i in acq_indices[1:]:
            buffer = self._ctx.dma_engine.arm_acquisition(
                samp_per_shot=samp_per_shot,
                shots_per_exp=shots,
                mode=mode,
                acq_ip_index=acq_i,
            )
            buffer_data = self._ctx.dma_engine.retrieve_acquisition(
                buffer=buffer,
                timeout=timeout,
                skip_timeout=skip_timeout,
            )
            results[acq_i] = buffer_data.copy()
            fpga_wait_s += self._ctx.dma_engine.last_dma_wait_s
            dma_overhead_s += self._ctx.dma_engine.last_invalidate_s

        return results, fpga_wait_s, dma_overhead_s


__all__ = ["DMAOrchestrator"]
