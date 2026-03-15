"""Acquisition operations for OverlayAdapter.

This module provides the AcquisitionOps class that handles all acquisition-related
hardware operations in a single flat class:

- DMA-based multi-acquisition with automatic chunking and pipelining
- Sweep mode preparation and finalization
- DDS modulation and ADC Mix-Mode configuration
- Trigger channel assignment for acquisition units
- Time-of-flight and duration timing configuration
- Timing statistics tracking

The class owns its own state (sweep_prepared, last_hw_shots, timing_stats,
acq_trigger_channel) and receives explicit typed dependencies.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType
from typing import TYPE_CHECKING, Literal, NoReturn

import numpy as np

from ...models.config_types import Modulation, TriggerCommand
from ...models.exceptions import ConfigurationError
from ._errors import check_driver_result

if TYPE_CHECKING:
    from ..dma_engine import DMAEngine
    from ._trigger_gen_ops import TriggerGeneratorOps


def _dma_timeout_handler(_signum: int, _frame: FrameType | None) -> NoReturn:
    """SIGALRM handler that raises TimeoutError for DMA acquisitions."""
    raise TimeoutError("DMA acquisition timeout")


class AcquisitionOps:
    """Acquisition operations: DMA orchestration, sweep, modulation, trigger, timing.

    This class manages all acquisition-related hardware operations and owns
    its own state for sweep mode, shot memoization, timing statistics, and
    trigger channel tracking.

    :param fireq_soc: The FIREQ_SoC hardware driver instance.
    :type fireq_soc: FIREQ_SoC-compatible
    :param logger: Logger instance for debug/error reporting.
    :type logger: logging.Logger
    :param dma_engine: DMA engine for buffer management and transfer.
    :type dma_engine: DMAEngine
    :param trigger: Trigger generator operations (explicit dependency, not back-reference).
    :type trigger: TriggerGeneratorOps
    """

    _DRIVER_NAME = "AcquisitionDriver"

    def __init__(
        self,
        fireq_soc: object,
        logger: logging.Logger,
        dma_engine: DMAEngine,
        trigger: TriggerGeneratorOps,
    ) -> None:
        """Initialize AcquisitionOps with direct typed dependencies.

        :param fireq_soc: The FIREQ_SoC hardware driver instance.
        :type fireq_soc: FIREQ_SoC-compatible
        :param logger: Logger instance for debug/error reporting.
        :type logger: logging.Logger
        :param dma_engine: DMA engine for buffer management and transfer.
        :type dma_engine: DMAEngine
        :param trigger: Trigger generator operations instance.
        :type trigger: TriggerGeneratorOps
        """
        self._fireq_soc = fireq_soc
        self._logger = logger
        self._dma_engine = dma_engine
        self._trigger = trigger

        self._sweep_prepared: bool = False
        self._last_hw_shots: int | None = None
        self._last_timing_stats: dict[str, float] = {
            "total_ms": 0.0,
            "fpga_wait_ms": 0.0,
            "dma_overhead_ms": 0.0,
            "sw_overhead_ms": 0.0,
        }
        self._acq_trigger_channel: dict[int, int] = {}

    # ========================================================================
    # PRIVATE HELPERS
    # ========================================================================

    def _get_acq(self, acq_index: int) -> object:
        """Retrieve the low-level driver for a specific acquisition unit.

        :param acq_index: Index of the target acquisition unit.
        :type acq_index: int
        :return: The low-level acquisition driver instance.
        :rtype: object
        :raises ConfigurationError: If the index is out of bounds or invalid.
        """
        try:
            return self._fireq_soc.acquisitions[int(acq_index)]
        except Exception as e:
            raise ConfigurationError(f"Invalid acq_index={acq_index}") from e

    def _check(self, result: object, *, operation: str, hint: str | None = None) -> object:
        """Check a driver return code and raise on error.

        :param result: Raw return value from the driver method.
        :type result: object
        :param operation: Name of the driver operation.
        :type operation: str
        :param hint: Explicit diagnostic hint.
        :type hint: str | None
        :return: The original result on success.
        :rtype: object
        :raises ConfigurationError: If the result is a negative integer.
        """
        return check_driver_result(
            result,
            operation=operation,
            driver_name=self._DRIVER_NAME,
            logger=self._logger,
            hint=hint,
        )

    def _configure_adc_mix_mode(self, acq_index: int, freq_mhz: float) -> None:
        """Configure the ADC Mix-Mode (Nyquist zone) for an acquisition unit.

        Silently returns if the driver does not support mix-mode
        configuration (e.g. on mock overlays or older bitstreams).

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param freq_mhz: Target frequency in MHz.
        :type freq_mhz: float
        """
        try:
            mix_info = self._fireq_soc.configure_adc_mix_mode(acq_index=acq_index, freq_mhz=freq_mhz)
            if mix_info.get("changed"):
                self._logger.debug(
                    "ADC Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                    mix_info["nyquist_zone"],
                    mix_info["amd_zone"],
                    mix_info["tile"],
                    mix_info["block"],
                )
        except ValueError as e:
            self._logger.warning("ADC Mix-mode config skipped: %s", e)

    def _memoize_trigger_shots(self, hw_shots: int) -> None:
        """Set trigger shot count only if it changed (avoids redundant HW writes).

        :param hw_shots: Number of shots for the hardware run.
        :type hw_shots: int
        """
        if hw_shots != self._last_hw_shots:
            self._trigger.set_shots(hw_shots)
            self._last_hw_shots = hw_shots

    # ========================================================================
    # PUBLIC PROPERTIES
    # ========================================================================

    @property
    def last_timing_stats(self) -> dict[str, float]:
        """Retrieve the last timing statistics from an acquisition.

        :return: Dictionary with timing breakdown (total_ms, fpga_wait_ms,
            dma_overhead_ms, sw_overhead_ms).
        :rtype: dict[str, float]
        """
        return self._last_timing_stats

    @property
    def acq_trigger_channels(self) -> dict[int, int]:
        """Mapping of acquisition IP index to its currently assigned trigger channel.

        A channel value of 0 means the acquisition unit is deaf (not listening).

        :return: Copy of the trigger channel assignment map.
        :rtype: dict[int, int]
        """
        return dict(self._acq_trigger_channel)

    # ========================================================================
    # PUBLIC METHODS — DMA Acquisition
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
        :type mode: str
        :param samp_per_shot: Number of samples per individual shot.
        :type samp_per_shot: int
        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :return: The maximum allowable shots for a single atomic execution.
        :rtype: int
        """
        buffer_max = self._dma_engine.get_max_shots(mode, samp_per_shot, acq_index)
        return min(self._trigger.max_hw_shots, buffer_max)

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
        :type acq_indices: list[int]
        :param mode: Acquisition mode.
        :type mode: str
        :param shots: Total number of shots to acquire.
        :type shots: int
        :param samp_per_shot: Number of samples per shot.
        :type samp_per_shot: int
        :param timeout: Per-chunk timeout budget in seconds. The effective SIGALRM is set
            to ``timeout * num_chunks`` (total), so a single slow chunk is not interrupted
            until the cumulative budget expires. Pass ``None`` to disable timeout.
        :type timeout: float | None
        :param validate_chunk: If True, perform input validation and compute chunk sizes.
        :type validate_chunk: bool
        :return: Iterator yielding data_dict for each chunk.
        :rtype: Iterator[dict[int, np.ndarray]]
        """
        # Start total routine timer for performance analysis
        t_start_routine = time.perf_counter()
        fpga_wait_accum = 0.0
        dma_overhead_accum = 0.0

        if validate_chunk:
            # --- Input Validation ---
            if not acq_indices:
                raise ConfigurationError("No acquisition unit indices provided.")
            if len(acq_indices) > len(self._fireq_soc.hw_specs["acquisitions"]):
                raise ConfigurationError(
                    f"Requested {len(acq_indices)} acquisition units, "
                    f"but only {len(self._fireq_soc.hw_specs['acquisitions'])} available."
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

        # Wrap all acquisitions with single timeout context.
        # The timing stats update is in a finally block to ensure it runs even if the
        # caller breaks out of the generator early (e.g. via break or exception).
        try:
            with self._dma_timeout_context(total_timeout):
                # --- Case 1: Single Hardware Acquisition ---
                if shots <= max_hw_shots:
                    data, fpga_s, dma_s = self._run_single_hw_acquisition(
                        acq_indices=acq_indices,
                        mode=mode,
                        shots=shots,
                        samp_per_shot=samp_per_shot,
                    )
                    fpga_wait_accum += fpga_s
                    dma_overhead_accum += dma_s
                    yield data

                # --- Case 2: Multiple Hardware Acquisitions (Pipelined Chunking) ---
                else:
                    self._logger.debug("Splitting %d shots into chunks of %d", shots, max_hw_shots)
                    remaining = shots
                    first_acq_idx = acq_indices[0]

                    # Pipelined state: buffer from pre-armed transfer (None if not in-flight)
                    inflight_buffer: object | None = None
                    inflight_shots: int = 0

                    while remaining > 0:
                        hw_shots = min(max_hw_shots, remaining)
                        next_remaining = remaining - hw_shots
                        has_next = next_remaining > 0

                        # --- Start current chunk (if not pre-started) ---
                        if inflight_buffer is None:
                            # First iteration: configure trigger, ARM first Acquisition IP, TRIGGER
                            self._memoize_trigger_shots(hw_shots)

                            self._configure_acq_output_mode(acq_indices, mode)

                            inflight_buffer = self._dma_engine.arm_acquisition(
                                samp_per_shot=samp_per_shot,
                                shots_per_exp=hw_shots,
                                mode=mode,
                                acq_index=first_acq_idx,
                                fast_path=self._sweep_prepared,
                            )
                            self._trigger.trigger_experiment()
                            inflight_shots = hw_shots

                        # --- Complete current chunk: WAIT + COPY all AcquisitionIPs ---
                        results, fpga_s, dma_s = self._retrieve_all_acq_ips(
                            acq_indices,
                            inflight_buffer,
                            inflight_shots,
                            samp_per_shot,
                            mode,
                        )
                        fpga_wait_accum += fpga_s
                        dma_overhead_accum += dma_s

                        # --- Pre-start next chunk (before yield) for pipelined execution ---
                        if has_next:
                            next_hw_shots = min(max_hw_shots, next_remaining)
                            self._memoize_trigger_shots(next_hw_shots)

                            inflight_buffer = self._dma_engine.arm_acquisition(
                                samp_per_shot=samp_per_shot,
                                shots_per_exp=next_hw_shots,
                                mode=mode,
                                acq_index=first_acq_idx,
                                fast_path=self._sweep_prepared,
                            )
                            self._trigger.trigger_experiment()
                            inflight_shots = next_hw_shots
                        else:
                            inflight_buffer = None  # No more chunks

                        # --- Yield current chunk (DMA for next chunk already running) ---
                        yield results

                        remaining = next_remaining

        finally:
            # --- Performance Calculation ---
            # Placed in finally so stats are always updated, even if caller breaks early.
            t_end_routine = time.perf_counter()
            total_duration = t_end_routine - t_start_routine
            sw_overhead = total_duration - fpga_wait_accum - dma_overhead_accum

            # Update statistics for telemetry (detailed breakdown)
            self._last_timing_stats = {
                "total_ms": total_duration * 1000.0,
                "fpga_wait_ms": fpga_wait_accum * 1000.0,
                "dma_overhead_ms": dma_overhead_accum * 1000.0,
                "sw_overhead_ms": sw_overhead * 1000.0,
            }

    # ========================================================================
    # PUBLIC METHODS — Sweep Mode
    # ========================================================================

    def prepare_sweep(self, mode: str, acq_indices: list[int]) -> None:
        """Prepare acquisition IPs and DMA engine for sweep-optimized execution.

        This configuration locks the acquisition hardware into the specified mode to
        guarantee invariant behavior across the sweep duration.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :type mode: str
        :param acq_indices: List of active acquisition unit indices involved in the sweep.
        :type acq_indices: list[int]
        """
        # Lock output type for the sweep duration
        self._configure_acq_output_mode(acq_indices, mode, force=True)

        # Update active acquisition units - frees buffers for units not in use
        self._dma_engine.set_active_acq_ip(acq_indices)

        self._sweep_prepared = True
        # Reset memoized trigger shots so first acquisition in sweep configures trigger.
        self._last_hw_shots = None

    def end_sweep(self) -> None:
        """Finalize the sweep execution and release DMA engine resources.

        This method must be called at the end of a sweep sequence to ensure the DMA
        engine correctly exits the optimized state and acquisition IPs are clean.
        """
        self._dma_engine.end_sweep()
        self._sweep_prepared = False
        # Reset memorized trigger shots for next acquisition sequence.
        self._last_hw_shots = None

    # ========================================================================
    # PUBLIC METHODS — Modulation
    # ========================================================================

    def set_modulation(self, acq_index: int, mod: Modulation) -> dict:
        """Configure the DDS modulation parameters for an acquisition unit.

        Handles both the digital frequency synthesis configuration and the
        analog-domain Mix-Mode settings (Nyquist zone) based on the target frequency.

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
        acq = self._get_acq(acq_index)

        self._configure_adc_mix_mode(acq_index, freq_mhz)

        self._check(
            acq.set_acquisition_dds_parameters(
                frequency=freq_mhz,
                phase=phase,
                adc_samplerate=float(self._fireq_soc.hw_specs["summary"]["adc_sr_hz"]) / 1e6,
            ),
            operation="set_acquisition_dds_parameters",
        )
        self._logger.debug("set_modulation: done acq=%d", acq_index)

        return {
            "acq_index": acq_index,
            "frequency_mhz": freq_mhz,
            "phase": phase,
        }

    # ========================================================================
    # PUBLIC METHODS — Trigger Listener
    # ========================================================================

    def set_trigger_listener(self, acq_index: int, trig: TriggerCommand) -> dict:
        """Configure which trigger channel the acquisition should listen to.

        :param acq_index: Index of the target acquisition unit.
        :type acq_index: int
        :param trig: Dictionary defining the trigger source channel.
        :type trig: TriggerCommand
        :return: The applied trigger configuration.
        :rtype: dict
        """
        channel = trig["channel"]

        self._logger.debug("set_trigger_listener: acq=%d channel=%s", acq_index, channel)
        acq = self._get_acq(acq_index)

        self._check(
            acq.set_trigger_channel(channel=channel),
            operation="set_trigger_channel",
        )

        if channel == 0:
            self._logger.debug("Acquisition %d is deaf to any trigger!", acq_index)
        else:
            self._logger.debug(
                "Acquisition %d listens to trigger_word channel %d",
                acq_index,
                channel,
            )
        self._acq_trigger_channel[int(acq_index)] = int(channel)

        return {
            "acq_index": acq_index,
            "channel": channel,
        }

    # ========================================================================
    # PUBLIC METHODS — Timing
    # ========================================================================

    def set_timing(self, acq_index: int, tof: int, duration: int) -> dict:
        """Configure the timing parameters (Time of Flight and acquisition duration).

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
        acq = self._get_acq(acq_index)

        self._check(
            acq.set_acquisition_duration(duration),
            operation="set_acquisition_duration",
        )

        self._check(
            acq.set_time_of_flight(tof),
            operation="set_time_of_flight",
        )
        return {
            "acq_index": acq_index,
            "tof": tof,
            "duration": duration,
        }

    # ========================================================================
    # INTERNAL HELPERS — DMA
    # ========================================================================

    def _retrieve_all_acq_ips(
        self,
        acq_indices: list[int],
        inflight_buffer: object,
        shots: int,
        samp_per_shot: int,
        mode: str,
    ) -> tuple[dict[int, np.ndarray], float, float]:
        """Retrieve DMA buffers for all acquisition IPs after a trigger.

        The first acquisition IP waits on its pre-armed buffer.
        Remaining IPs are armed and retrieved sequentially (data already in FIFO).

        :param acq_indices: Ordered list of acquisition IP indices.
        :param inflight_buffer: Pre-armed DMA buffer for the first IP.
        :param shots: Number of shots in this hardware run.
        :param samp_per_shot: Samples per shot.
        :param mode: Acquisition mode.
        :return: (results_dict, fpga_wait_s, dma_overhead_s).
        """
        results: dict[int, np.ndarray] = {}
        fpga_wait_s = 0.0
        dma_overhead_s = 0.0

        # First acquisition IP: wait on pre-armed buffer
        dma_result = self._dma_engine.retrieve_acquisition(buffer=inflight_buffer)
        results[acq_indices[0]] = dma_result.buffer.copy()
        fpga_wait_s += dma_result.dma_wait_s
        dma_overhead_s += dma_result.invalidate_s

        # Remaining acquisition IPs: arm + retrieve (data already in FIFO)
        for acq_idx in acq_indices[1:]:
            buffer = self._dma_engine.arm_acquisition(
                samp_per_shot=samp_per_shot,
                shots_per_exp=shots,
                mode=mode,
                acq_index=acq_idx,
                fast_path=self._sweep_prepared,
            )
            dma_result = self._dma_engine.retrieve_acquisition(buffer=buffer)
            results[acq_idx] = dma_result.buffer.copy()
            fpga_wait_s += dma_result.dma_wait_s
            dma_overhead_s += dma_result.invalidate_s

        return results, fpga_wait_s, dma_overhead_s

    def _configure_acq_output_mode(
        self,
        acq_indices: list[int],
        mode: str,
        *,
        force: bool = False,
    ) -> None:
        """Configure acquisition IP output type (decimated/accumulated).

        Skipped when sweep mode is active (output already locked by
        ``prepare_sweep``), unless *force* is True.

        :param acq_indices: List of acquisition unit indices to configure.
        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :param force: If True, configure even when sweep mode is active.
        """
        if not force and self._sweep_prepared:
            return
        for acq_idx in acq_indices:
            acq = self._get_acq(acq_idx)
            if mode in ("decimated", "accumulated"):
                self._check(
                    acq.set_decimated_output_type(mode),
                    operation="set_decimated_output_type",
                )

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

        old_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _dma_timeout_handler)
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
    ) -> tuple[dict[int, np.ndarray], float, float]:
        """Execute a single hardware acquisition cycle (ARM -> TRIGGER -> RETRIEVE).

        Returns raw DMA buffers. Client computes valid_words from request params.

        :param acq_indices: List of acquisition unit indices.
        :param mode: Acquisition mode.
        :param shots: Number of shots for this specific hardware run.
        :param samp_per_shot: Samples per shot.
        :return: (data_dict, fpga_wait_s, dma_overhead_s).
        """
        self._memoize_trigger_shots(shots)
        self._configure_acq_output_mode(acq_indices, mode)

        # ARM first acquisition IP → TRIGGER → RETRIEVE all IPs
        first_buffer = self._dma_engine.arm_acquisition(
            samp_per_shot=samp_per_shot,
            shots_per_exp=shots,
            mode=mode,
            acq_index=acq_indices[0],
            fast_path=self._sweep_prepared,
        )
        self._trigger.trigger_experiment()

        return self._retrieve_all_acq_ips(
            acq_indices,
            first_buffer,
            shots,
            samp_per_shot,
            mode,
        )


__all__ = ["AcquisitionOps"]
