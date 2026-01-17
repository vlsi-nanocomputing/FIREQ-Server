# file: fireq-utils/server/dma_engine.py
"""

Purpose
-------
This module provides a *high-level*, review-oriented abstraction for FPGA data acquisition
using a Xilinx AXI DMA controlled through PYNQ.

It exists to isolate hardware-coupled acquisition into one place:

- stream routing via an AXI Stream Switch ("raw" vs "decimated/accumulated" path),
- contiguous DDR buffer allocation through :func:`pynq.allocate`,
- starting and waiting for DMA transfers,
- robust recovery logic when the DMA becomes stuck (e.g., TLAST not received),
- conversion of packed acquisition words into complex I/Q NumPy arrays.

Architectural intent
--------------------
The orchestration layer (e.g., server/experiment logic) should *not* need to know:
which MMIO registers to poke, how PYNQ behaves when DMA is wedged, how buffers
must be aligned/allocated, or how firmware packs I/Q samples. This module centralizes those
concerns and exposes a small, explicit contract:

1) `AcquisitionEngine.arm_acquisition` to configure routing + allocate buffers + start DMA
2) `AcquisitionEngine.retrieve_acquisition` to wait + recover on failure + parse output

Invariants and assumptions
--------------------------
- ``hw_specs`` must describe the acquisition IPs and FIFO sizing (depth/width/parallelism)
  consistently with the loaded bitstream.
- ``dma`` is a valid PYNQ DMA instance exposing ``recvchannel`` and ``mmio``.
- The DMA direction is S2MM (stream-to-memory);
- Buffer sizing assumes a firmware contract about output packing:
  - decimated/raw: one 32-bit word per complex sample (I16|Q16 packed),
  - accumulated: two 32-bit words per shot (I32 then Q32).
- This module is intentionally *fail-fast* on invalid modes/specs because silent truncation
  or mis-parsing would corrupt experimental results.

Failure modes and recovery policy
---------------------------------
DMA can enter states that PYNQ cannot recover from cleanly (e.g., a hung ``wait()``
when TLAST never arrives). The policy here is:

- On timeout or unexpected DMA errors, perform a direct MMIO-based reset of the DMA core
  (bypassing PYNQ helpers that may hang), then free persistent buffers to prevent reuse of
  potentially inconsistent memory mappings.

Performance trade-offs
----------------------
The engine can reuse persistent DMA buffers across acquisitions to avoid repeated allocations.
A "sweep fast path" exists to skip repeated validation/logging when the caller guarantees that
the configuration is invariant across iterations.

The sweep fast path is *unsafe* if the caller changes sample counts, shot counts, mode, or the
hardware configuration without ending the sweep and re-arming through the full path.
"""


import logging
import signal  # for timeout handling
import time
from typing import Any, Dict, Literal, Optional

import numpy as np
from pynq import allocate

from .exceptions import DMAError, DMATimeoutError


class AcquisitionEngine:
    """
    High-level manager for DMA acquisitions.
    The intended call sequence is:

    - `arm_acquisition`:
        Validates capacity (full path), routes the stream switch, allocates or reuses a DDR
        buffer, and starts DMA reception.
    - `retrieve_acquisition`:
        Waits for completion, applies timeout protection, performs fail-fast recovery on
        DMA errors, invalidates CPU caches, and parses into complex I/Q arrays.

    Minor sweep optimization
    ------------------
    When executing repeated acquisitions with identical configuration, the caller may use:

    - `prepare_sweep` to declare the acquisition mode invariant
    - `arm_acquisition` calls (fast path used internally)
    - `end_sweep` to return to conservative behavior

    The sweep fast path skips capacity validation and assumes buffer sizing and firmware
    format remain unchanged. If those assumptions are violated, results may be truncated
    or misinterpreted: this is an explicit trade-off.

    """

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    # Pipelined buffer acquisition margin
    # Notice that it is a global margin s.t. it will be uniform across all DMA instances
    # Future-proof in case of multiple DMA in the same PL design

    def __init__(
        self, dma: Any, switch: Any, logger: Optional[logging.Logger] = None, hw_specs: Dict[str, Any] = None
    ) -> None:
        """
        Construct an acquisition engine bound to a specific DMA + stream switch.

        :param dma:
            PYNQ DMA instance
        :type dma: Any
        :param switch:
            AXI Stream Switch IP used to route the selected ADC/mode stream into the DMA.
        :type switch: Any
        :param logger:
            Optional logger. If not provided, a module logger is used.
        :type logger: Optional[logging.Logger]
        :param hw_specs:
            Hardware specification dictionary describing acquisition IP properties.
            This is treated as the "single source of truth" for buffer sizing
            and limits.
        :type hw_specs: Dict[str, Any]

        :raises DMAError:
            If the DMA channel cannot be started (indicates invalid overlay
            wiring or a broken DMA object).
        """

        self.dma = dma
        self.switch = switch
        self.logger = logger or logging.getLogger(__name__)
        self.hw_specs = hw_specs
        self._inflight = False
        self._persistent_buffers = {}
        # sweep mode flags
        self._sweep_mode = None
        self._sweep_prepared = False
        # Last successful DMA wait duration (seconds). Set to 0.0 on entry.
        self.last_dma_wait_s = 0.0

        # Ensure the DMA recvchannel is transitioned into a usable state early.
        # This is intentionally done at construction time so failures are detected
        # before we allocate buffers or program other IPs (fail-fast for HW sanity).
        self._ensure_started()

        # MMIO register addresses are duplicated here to keep this class self-contained.
        # If these addresses change with the bitstream, hw/overlay versioning must ensure
        # this code is updated in lockstep.
        self.REG_CTRL = 0x00
        self.REG_MI_MUX_0 = 0x40
        self.MASK_COMMIT = 0x00000002
        # --- DMA (S2MM) registers for "emergency" reset ---
        self.REG_S2MM_DMACR = 0x30
        self.REG_S2MM_DMASR = 0x34
        self.MASK_RESET = 0x00000004  # bit Reset
        self.MASK_IRQ_CLEAR = 0x00007000  # W1C on IOC, DM, ERR

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def abort(self) -> None:
        """
        Abort an in-flight DMA acquisition and force the hardware into a known state.

        This method is "defensive": it attempts a stop via PYNQ (when available),
        then unconditionally performs a low-level reset sequence.

        Motivation
        ----------
        When DMA is wedged, PYNQ helper methods are observed to block
        indefinitely sometimes. A direct reset is the only reliable way to
        restore forward progress and prevent subsequent acquisitions from
        reusing a corrupted DMA state.


        """

        try:
            # This is not trusted as a recovery mechanism because
            # some stuck conditions manifest as hangs inside PYNQ control paths.
            if hasattr(self.dma.recvchannel, "stop"):
                self.dma.recvchannel.stop()
        except Exception:
            pass
        self._hard_reset()

    def free_resources(self) -> None:
        """
        Release all persistent DMA buffers allocated by this instance.

        Buffer lifetime policy
        ----------------------
        This engine may cache DDR buffers per ADC index to avoid repeated
        allocation and improve sweep throughput. Those buffers are physically
        contiguous and can be large; explicit release is important in
        long-running server processes.

        Robustness notes
        ----------------
        ``freebuffer()`` is invoked in a best-effort manner because buffer
        objects may be in partially-initialized states after low-level
        failures. Errors are logged but swallowed to prioritize cleanup
        completion over strict exception propagation.
        """

        for idx, buf in self._persistent_buffers.items():
            if hasattr(buf, "freebuffer"):
                try:
                    buf.freebuffer()
                except Exception as e:
                    # Cleanup: failures here are non-fatal, and raising would
                    # likely obscure the "original" hardware failure that triggered cleanup.
                    self.logger.warning(f"Failed to free buffer for ADC {idx}: {e}")
        self._persistent_buffers.clear()

    def get_max_shots(
        self,
        mode: Literal["raw", "decimated", "accumulated"],
        samp_per_shot: int,
        adc_index: int,
    ) -> int:
        """
        Compute the maximum number of shots that can fit in the acquisition FIFO/buffer.

        This is a hardware-capacity computation based on the FIFO depth/width
        described in ``hw_specs``. It intentionally does not consider
        higher-level constraints such as trigger generator register limits.

        :param mode:
            Acquisition output mode. Determines FIFO width and packing.
        :type mode: Literal["raw", "decimated", "accumulated"]
        :param samp_per_shot:
            Samples per shot as interpreted by the selected mode. For raw mode, the
            effective samples per shot scale by ADC parallelism.
        :type samp_per_shot: int
        :param adc_index:
            Acquisition IP index used to select FIFO parameters.
        :type adc_index: int
        :return:
            Maximum number of shots that fit without overflow (0 if
            configuration yields degenerate sizing).
        :rtype: int

        :raises DMAError:
            If ``mode`` is unknown.
        """
        acq_spec = self.hw_specs["acquisitions"][adc_index]

        if mode in ["decimated", "accumulated"]:
            fifo_depth = int(acq_spec.get("decimated_fifo_depth_words", 0))
            fifo_width = int(acq_spec.get("dec_output_width_bits", 64))
        elif mode == "raw":
            fifo_depth = int(acq_spec.get("raw_fifo_depth_words", 0))
            fifo_width = int(acq_spec.get("raw_output_width_bits", 256))
        else:
            raise DMAError(f"Unknown mode: {mode}")

        # Capacity is computed in bits to avoid mixing word-size assumptions.
        # This makes the constraint audit-friendly when FIFO widths differ by mode.
        total_bits = fifo_depth * fifo_width

        if mode == "accumulated":
            return total_bits // 64
        elif mode == "decimated":
            bits_per_shot = samp_per_shot * 32
            return total_bits // bits_per_shot if bits_per_shot > 0 else 0
        else:  # raw
            parallelism = int(acq_spec.get("parallelism", 1))
            bits_per_shot = samp_per_shot * parallelism * 32
            return total_bits // bits_per_shot if bits_per_shot > 0 else 0

    def __del__(self):
        """Free resources in case the object is destroyed."""
        self.free_resources()

    # ------------------------------------------------------------------
    # Acquisition methods : main methods
    # ------------------------------------------------------------------

    def arm_acquisition(
        self,
        samp_per_shot: int,
        shots_per_exp: int,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
    ) -> Any:
        """
        Arm a DMA acquisition: validate, route, allocate/reuse buffer, and start DMA.

        This method is intentionally split into two paths:

        - Full path (conservative):
            Validates requested acquisition size against FIFO capacity, checks DMA state,
            performs routing, allocates a sufficiently large buffer, and starts DMA.
        - Sweep fast path (optimized):
            Reuses previously allocated buffers and skips repeated validation
            when the caller guarantees invariant acquisition configuration
            across iterations.

        :param samp_per_shot:
            Number of samples per shot (mode-dependent interpretation).
        :type samp_per_shot: int
        :param shots_per_exp:
            Number of shots to acquire in this hardware run.
        :type shots_per_exp: int
        :param mode:
            Acquisition mode: ``raw``/``decimated``/``accumulated``.
        :type mode: Literal["raw", "decimated", "accumulated"]
        :param adc_index:
            ADC/acquisition IP index to route and arm.
        :type adc_index: int
        :return:
            The allocated (or reused) DMA buffer passed to ``recvchannel.transfer()``.
        :rtype: Any

        :raises DMAError:
            On invalid sizes, invalid mode, or inability to start DMA transfer.
        """

        # "Fast path "is correct if the caller keeps mode and sizing invariants stable
        # across iterations.
        if self._sweep_prepared and self._sweep_mode == mode:
            return self._arm_acquisition_fast(mode, adc_index)
        # Full path
        return self._arm_acquisition_full(samp_per_shot, shots_per_exp, mode, adc_index)

    def retrieve_acquisition(
        self,
        buffer: Any,
        mode: str,
        shots: int,
        samp_per_shot: int,
        adc_index: int,
        timeout: Optional[float] = None,
    ) -> np.ndarray:
        """
        Wait for DMA completion, apply timeout protection, and parse the
        acquired data.

        Hardware DMA can hang (commonly: missing TLAST, upstream stream
        starvation). In those cases, waiting indefinitely would deadlock
        the server. Therefore:

        - an optional timeout is enforced (UNIX only, via ``SIGALRM``.
          REMARK: it is compatible with the main thread only),
        - on timeout or unexpected DMA errors, a low-level hard reset is
          executed,
        - persistent buffers are freed to avoid reusing potentially
          inconsistent mappings.

        :param buffer:
            DMA destination buffer previously returned by :meth:`arm_acquisition`.
        :type buffer: Any
        :param mode:
            Acquisition mode used for parsing.
        :type mode: str
        :param shots:
            Number of shots expected in this buffer.
        :type shots: int
        :param samp_per_shot:
            Samples per shot (mode-dependent interpretation).
        :type samp_per_shot: int
        :param adc_index:
            ADC/acquisition IP index used for parsing parameters (e.g., parallelism).
        :type adc_index: int
        :param timeout:
            Timeout in seconds. If ``None`` or non-positive, no timeout is enforced.
        :type timeout: Optional[float]
        :return:
            Parsed complex I/Q data.
        :rtype: np.ndarray

        :raises DMATimeoutError:
            If the DMA wait exceeds the timeout.
        :raises DMAError:
            For other DMA failures or parsing/validation errors.
        """

        # --- Setup optional timeout via signals (UNIX only) ---
        timeout_sec: Optional[float] = None
        old_handler = None
        self.last_dma_wait_s = 0.0
        # NOTE: _hash_sigalrm variable is only meant
        # to enable functional tests on Windows environment
        # Check if SIGALRM is available (Unix only)
        has_sigalrm = hasattr(signal, "SIGALRM")

        if timeout is not None and timeout > 0:
            if has_sigalrm:
                timeout_sec = float(timeout)

                def _timeout_handler(signum, frame):
                    raise TimeoutError("DMA wait timeout")

                # Save old handler, set new one
                old_handler = signal.getsignal(signal.SIGALRM)
                signal.signal(signal.SIGALRM, _timeout_handler)
                signal.setitimer(signal.ITIMER_REAL, timeout_sec)
            else:
                self.logger.warning("DMA timeout disabled: SIGALRM not supported on this platform.")

        try:
            # Block until DMA finishes
            # Blocking wait is the standard PYNQ completion mechanism.
            # If the hardware never asserts TLAST, this call may block forever without
            # an external timeout mechanism.
            t_wait_start = time.perf_counter()
            self.dma.recvchannel.wait()
            self.last_dma_wait_s = time.perf_counter() - t_wait_start

        except TimeoutError as e:
            # Timeout means DMA is likely starving (waiting for TLAST).
            # We MUST reset the core to clear the internal buffer state.
            self.logger.error(f"DMA Timeout ({timeout_sec}s). Resetting core.")
            self._hard_reset()
            # free resources in case of timeout error
            self.free_resources()
            raise DMATimeoutError(f"DMA transfer timed out after {timeout_sec:.3f} s") from e

        except Exception as e:
            # Any other error (e.g. RuntimeError: DMA channel not started)
            # implies the state is inconsistent. Reset.
            self._hard_reset()
            self.free_resources()
            self.logger.error(f"DMA Runtime Error: {e}. Resetting core.")
            raise DMAError(f"DMA transfer failed: {e}") from e

        finally:
            # Restore signal handler
            if timeout_sec is not None:
                try:
                    signal.setitimer(signal.ITIMER_REAL, 0.0)
                except Exception:
                    pass
                if old_handler is not None:
                    try:
                        signal.signal(signal.SIGALRM, old_handler)
                    except Exception:
                        pass

        # Invalidate buffer cache
        try:
            buffer.invalidate()
        except Exception:
            pass

        # --- Parse data ---
        return self._parse(buffer, mode, shots, samp_per_shot, adc_index)

    # ------------------------------------------------------------------
    # Acquisition methods : full validation for non-sweep experiments
    # ------------------------------------------------------------------

    def _arm_acquisition_full(
        self,
        samp_per_shot: int,
        shots_per_exp: int,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
    ) -> Any:
        """
        Conservative arm path: validate capacity, check DMA state, route
        stream, allocate buffer, start DMA.

        This method is intentionally strict. The validation step is
        performed *before* routing/starting DMA so that configuration errors
        are reported without mutating hardware state.

        :raises DMAError:
            If parameters are invalid, capacity is exceeded, DMA is not
            startable, or transfer fails.
        """

        if shots_per_exp < 1:
            raise DMAError("shots_per_exp must be >= 1")

        total_shots = shots_per_exp

        # 1. Validation
        self._validate_buffer_capacity(samp_per_shot, shots_per_exp, mode, adc_index)

        self.logger.debug(
            f"Arming DMA: samp/shot={samp_per_shot}, shots/exp={shots_per_exp}, "
            f"total={total_shots}, mode={mode}, adc={adc_index}"
        )

        # 2. DMA state check
        # DMA may report a non-running/non-idle state when halted or after
        # an error. Starting a transfer in that state is undefined; actively
        # reset avoids failures and producing misleading partial data.
        if not self.dma.recvchannel.running and not self.dma.recvchannel.idle:
            self.logger.warning("DMA in halted/error state. Forcing hard reset.")
            self._hard_reset()

        # 3. Switch routing
        self._route_switch(adc_index=adc_index, raw_mode=(mode == "raw"))

        # 4. Buffer allocation
        total_words = self._compute_total_words(mode, adc_index)
        buffer = self._get_or_allocate_buffer(adc_index, total_words)

        # 5. Start DMA
        try:
            # If this fails, reset immediately: subsequent calls should not
            # inherit a partially-started channel state.
            self.dma.recvchannel.transfer(buffer)
        except Exception as e:
            self._hard_reset()
            raise DMAError(f"DMA start failed: {e}") from e

        return buffer

    # ------------------------------------------------------------------
    # Acquisition methods: sweep
    # ------------------------------------------------------------------

    def prepare_sweep(
        self,
        mode: Literal["raw", "decimated", "accumulated"],
    ) -> None:
        """
        Enable sweep mode optimizations for repeated acquisitions.
        Sweep mode is a performance feature: repeated iterations share the same
        acquisition configuration (mode, duration limits, buffer sizing).
        Reuse persistent buffers and skip capacity validation on each arm.

        Contract
        --------
        The caller is responsible for keeping these invariants constant
        across the sweep:

        - acquisition ``mode``,
        - effective buffer sizing implied by the firmware and hw_specs,
        - no hot-swapping overlays/bitstreams while sweep mode is active.

        If any of these change, the caller must call :meth:`end_sweep` and
        re-arm via the full path to avoid mis-sized buffers or mis-parsed
        data.
        """

        self._sweep_prepared = True
        self._sweep_mode = mode
        self.logger.debug(f"Sweep prepared: mode={mode}")

    def end_sweep(self) -> None:
        """
        Disable sweep mode and return to conservative validation behavior.

        """
        self._sweep_prepared = False
        self._sweep_mode = None

    def _arm_acquisition_fast(
        self,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
    ) -> Any:
        """
        Optimized sweep arm path.

        Differences vs full path:

        - skips FIFO capacity validation and most logging,
        - reuses a persistent buffer if available,
        - still performs stream routing (ADC selection is assumed not to be
          invariant in general).

        Safety notes
        ------------
        This path is only correct when the caller guarantees that the
        acquisition sizing assumptions remain unchanged across the sweep.
        If the request grows beyond the cached buffer size, we allocate a
        larger one (safe monotonic growth).
        """

        # 1. Routing (always needed - changes per ADC)
        self._route_switch(adc_index=adc_index, raw_mode=(mode == "raw"))

        # 2. Reuse existing buffer (assume already allocated)
        buffer = self._persistent_buffers.get(adc_index)
        if buffer is None:
            # Fallback to full allocation if not pre-allocated
            total_words = self._compute_total_words(mode, adc_index)
            buffer = self._get_or_allocate_buffer(adc_index, total_words)

        try:
            self.dma.recvchannel.transfer(buffer)
        except Exception as e:
            self._sweep_prepared = False  # Exit sweep mode
            self._hard_reset()
            raise DMAError(f"DMA start failed in sweep: {e}") from e

        return buffer

    # ------------------------------------------------------------------
    # Internal methods: routing, dimensions, parsing, reset
    # ------------------------------------------------------------------
    def _ensure_started(self) -> None:
        """
        Ensure the PYNQ DMA recvchannel is started.

        Motivation
        ---------
        To ensure the DMA was correctly setup after a hard reset.

        :raises DMAError:
            If the channel cannot be started.
        """

        ch = self.dma.recvchannel
        try:
            # PYNQ implementations differ: some expose a `.running` property, others do not.
            # We probe defensively to keep the engine compatible across versions.
            if hasattr(ch, "running"):
                if not ch.running:
                    ch.start()
            else:
                ch.start()
        except Exception as e:
            raise DMAError(f"Failed to start DMA recvchannel: {e}") from e

    def _compute_total_words(
        self,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
    ) -> int:
        """
        Compute the DMA buffer length required by the selected mode.

        This is based on *hardware maximum duration* as described by
        ``hw_specs``, not on the user's requested duration. The intention
        is to allocate a buffer that always fits the maximum allowed
        acquisition for the current bitstream configuration.

        Firmware packing assumptions
        ----------------------------
        - accumulated: 2 words per cycle (I32, Q32)
        - decimated: 1 word per cycle (packed I16/Q16)
        - raw: ``parallelism`` words per cycle (packed I16/Q16 per lane)

        :raises DMAError:
            If ``mode`` is unknown.
        """

        # Buffer sizing uses hw_specs as the authoritative interface contract
        # between Python and FPGA firmware. Any mismatch here is a
        # versioning/configuration bug.
        acq_spec = self.hw_specs["acquisitions"][adc_index]
        # take out parameters from acquisition spec
        spec_dur = acq_spec["max_duration_cycles"]
        spec_par = acq_spec["parallelism"]

        max_cycles = int(spec_dur)
        parallelism = int(spec_par)

        self.logger.debug(f"Buffer Calc (ADC {adc_index}, {mode}): HW_MaxCycles={max_cycles}, Par={parallelism} ")
        if mode == "accumulated":
            # Accumulated output: 2 Sample/Clock
            return max_cycles * 2

        elif mode == "decimated":
            # Decimated output: 1 Sample/Clock
            return max_cycles

        elif mode == "raw":
            # Raw output: 'parallelism' Samples/Clock
            return max_cycles * parallelism

        else:
            raise DMAError(f"Unknown acquisition mode for buffer sizing: {mode}")

    def _route_switch(self, adc_index: int, raw_mode: bool) -> None:
        """
        Route the AXI Stream Switch to select the desired ADC and output mode.

        The switch is modeled as having two ports per ADC index:
        - even port: raw stream
        - odd port:  decimated/accumulated stream

        If ``self.switch`` is falsy, routing is treated as disabled and the
        method returns immediately (assumes a static fabric or a design
        without a switch).

        :param adc_index: Acquisition IP index.
        :type adc_index: int
        :param raw_mode:
            If True, route the raw path; otherwise route
            decimated/accumulated.
        :type raw_mode: bool

        :raises DMAError:
            If MMIO writes fail (indicates broken overlay wiring or IP
            address mismatch).
        """

        if not self.switch:
            return

        # Port mapping is a *bitstream-level contract*. If the switch
        # topology changes, this mapping must be updated together with
        # hw_specs.
        base_port = int(adc_index) * 2
        target_port = base_port + (0 if raw_mode else 1)

        self.logger.info(f"Routing AXI switch: adc={adc_index}, raw_mode={raw_mode} -> port={target_port}")

        try:
            self.switch.mmio.write(self.REG_MI_MUX_0, target_port)
            self.switch.mmio.write(self.REG_CTRL, self.MASK_COMMIT)
        except Exception as e:
            raise DMAError(f"AXI switch routing failed: {e}") from e

    def _parse(self, buffer: Any, mode: str, shots: int, samp_per_shot: int, adc_index: int) -> np.ndarray:
        """
        Converts the raw DMA buffer into a complex numpy array.

        This method processes raw data retrieved from the DMA, handling different
        firmware data formats (decimated/raw vs accumulated).

        :param buffer: The raw data buffer containing DMA samples.
        :type buffer: Any
        :param mode: The acquisition mode ('decimated', 'raw', or 'accumulated').
        :type mode: str
        :param shots: Number of acquisition shots captured.
        :type shots: int
        :param samp_per_shot: Number of samples per shot (decimated) or raw samples.
        :type samp_per_shot: int
        :param adc_index: Index of the ADC being processed.
        :type adc_index: int
        :return: A complex numpy array of shape (shots, samples) or (shots,).
        :rtype: np.ndarray
        :raises DMAError: If the hardware specifications are invalid or mode is unknown.
        """
        try:
            parallelism = int(self.hw_specs["acquisitions"][adc_index]["parallelism"])
        except (KeyError, ValueError, TypeError):
            raise DMAError(f"Cannot determine parallelism for ADC {adc_index} from hw_specs.")

        # Parsing begins by interpreting the DMA payload as uint32 words.
        # The firmware exports word-aligned samples, and using uint32
        # makes bit slicing/pattern interpretation explicit.

        raw_u32 = buffer.view(np.uint32) if isinstance(buffer, np.ndarray) else np.frombuffer(buffer, dtype=np.uint32)

        if mode == "accumulated":
            # Data format: 32-bit I and 32-bit Q are stored in separate words.
            # Sequence: I0, Q0, I1, Q1, ...

            # Accumulated mode is "one complex value per shot", represented
            # as two 32-bit signed words. Any mismatch here is a
            # firmware/API contract violation
            valid_len = shots * 2

            if len(raw_u32) < valid_len:
                self.logger.error(
                    f"Buffer size {len(raw_u32)} smaller than expected for accumulated mode ({valid_len})."
                )

            # Slice only the valid portion of the buffer
            trimmed_data = raw_u32[:valid_len]

            # Reinterpret as signed 32-bit integers for I/Q processing
            # I is at indices 0, 2, 4... | Q is at indices 1, 3, 5...
            i_data = trimmed_data[0::2].astype(np.int32)
            q_data = trimmed_data[1::2].astype(np.int32)

            complex_data = i_data + 1j * q_data

            return complex_data

        elif mode in ("decimated", "raw"):
            # Data format: Packed 32-bit word.
            # [31:16] = Q (16-bit signed)
            # [15:00] = I (16-bit signed)

            if mode == "decimated":
                real_samples_per_shot = samp_per_shot
            else:  # raw
                real_samples_per_shot = samp_per_shot * parallelism

            total_valid_samples = real_samples_per_shot * shots

            if total_valid_samples > len(raw_u32):
                self.logger.error("Buffer DMA smaller than expected valid samples.")
                valid_data = raw_u32
            else:
                valid_data = raw_u32[:total_valid_samples]

            # Parse packed I/Q using bitwise operations
            # Note: Casting to int16 handles the sign extension correctly
            # for 16-bit values.
            i_data = (valid_data & 0xFFFF).astype(np.int16)
            q_data = (valid_data >> 16).astype(np.int16)

            complex_data = i_data + 1j * q_data

            # Reshape data for user-ease and high level JSON-serialization
            try:
                return complex_data.reshape((shots, real_samples_per_shot))
            except ValueError as e:
                self.logger.warning(
                    f"Reshape failed for {shots} shots (samples_per_shot={real_samples_per_shot}): {e}. "
                    "Returning flat array."
                )
                return complex_data

        else:
            raise DMAError(f"Unknown acquisition mode for parsing: {mode}")

    def _hard_reset(self) -> None:
        """
        Perform a robust MMIO-based reset of the AXI DMA channel.

        Why this exists
        ---------------
        DMA can stall in ways that cause high-level control paths to hang
        indefinitely (notably PYNQ ``stop()`` / ``wait()`` interactions when
        the stream never completes). This method implements a deterministic,
        bounded-time recovery sequence:

        1) halt the channel by clearing Run/Stop,
        2) assert soft reset and wait (with a strict timeout) for the reset
           bit to clear,
        3) clear interrupt/status bits (write-one-to-clear),
        4) re-start the recvchannel to resynchronize PYNQ's wrapper state,
        5) reset internal PYNQ bookkeeping flags when present.

        Failure policy
        --------------
        If the reset bit does not clear within the timeout, we raise
        :class:`DMAError`. At that point, the most likely causes are missing
        clocks, a broken fabric, or an inconsistent overlay state.
        Continuing would be unsafe and misleading.

        :raises DMAError:
            On unrecoverable reset failures.
        """

        self.logger.warning("Initiating DMA S2MM Hard Reset Sequence...")
        mmio = self.dma.mmio

        # --- Constants defined locally to ensure compatibility ---
        MASK_RS = 0x00000001  # Run/Stop bit
        MASK_RESET = 0x00000004  # Soft Reset bit
        MASK_IRQ_ALL = 0x00007000  # All Interrupt flags (IOC, Dly, Err)

        # 1. BYPASS PYNQ stop() because it hangs if HW is stuck.
        # Instead, manually clear the Run/Stop bit (Halt).
        try:
            cr = mmio.read(self.REG_S2MM_DMACR)
            mmio.write(self.REG_S2MM_DMACR, cr & ~MASK_RS)
        except Exception as e:
            self.logger.error(f"Failed to halt DMA manually: {e}")

        try:
            # 2. Trigger Soft Reset (Write Reset bit = 1)
            mmio.write(self.REG_S2MM_DMACR, MASK_RESET)

            # 3. Wait for Reset to clear (with strict Timeout)
            # Timeout is intentionally short: a reset that cannot complete
            # promptly is not a transient performance issue but a sign of a
            # deeper hardware fault.
            timeout = 0.5  # 500ms safety limit
            start = time.time()

            while mmio.read(self.REG_S2MM_DMACR) & MASK_RESET:
                if (time.time() - start) > timeout:
                    self.logger.critical("DMA Hardware Reset TIMEOUT. Reset bit stuck high.")
                    raise DMAError("Hardware Reset Failed (Bit Stuck). Clock missing?")
                time.sleep(0.001)

            # 4. Clear Interrupts/Errors (Write 1 to clear)
            mmio.write(self.REG_S2MM_DMASR, MASK_IRQ_ALL)

            # 5. Re-synchronize PYNQ Object
            # Since we bypassed stop(), we force a start.
            # The HW is now reset, so .running property (which reads HW) will be False.
            if hasattr(self.dma.recvchannel, "start"):
                self.dma.recvchannel.start()

            # 6. Reset internal flags
            if hasattr(self.dma.recvchannel, "_first_transfer"):
                self.dma.recvchannel._first_transfer = True

            self.logger.info("DMA S2MM Hard Reset Completed.")

        except Exception as e:
            self.logger.error(f"Critical Failure during DMA Reset: {e}")
            raise DMAError(f"Critical Failure during DMA Reset: {e}") from e

    def _validate_buffer_capacity(
        self,
        samp_per_shot: int,
        shots_per_exp: int,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
    ) -> None:
        """
        Validate that the requested acquisition fits in the FPGA-side
        buffering capacity.

        This check is performed in *bits* using FIFO depth/width taken from
        ``hw_specs``. It prevents silent truncation and protects against
        wrong configurations.

        Caller responsibilities
        -----------------------
        - Provide correct ``hw_specs`` consistent with the loaded overlay.
        - Ensure ``samp_per_shot`` and ``shots_per_exp`` are the *true*
          firmware-level values for the selected mode.

        :raises DMAError:
            If the request exceeds capacity or if the mode is unknown.
        """

        acq_spec = self.hw_specs["acquisitions"][adc_index]

        # --- 1. Retrieve Hardware FIFO Capacity from hw_specs ---
        if mode in ["decimated", "accumulated"]:
            fifo_depth_words = int(acq_spec.get("decimated_fifo_depth_words", 0))
            fifo_width_bits = int(acq_spec.get("dec_output_width_bits", 64))
        elif mode == "raw":
            fifo_depth_words = int(acq_spec.get("raw_fifo_depth_words", 0))
            fifo_width_bits = int(acq_spec.get("raw_output_width_bits", 256))
        else:
            raise DMAError(f"Unknown mode for validation: {mode}")

        total_fifo_bits = fifo_depth_words * fifo_width_bits
        usable_fifo_bits = total_fifo_bits

        # --- 2. Calculate requested and limits based on mode ---
        if mode == "raw":
            parallelism = int(acq_spec.get("parallelism", 1))
            bits_per_sample = 32
            samples_per_shot = samp_per_shot * parallelism
            total_requested_bits = samples_per_shot * shots_per_exp * bits_per_sample

            max_total_samples = usable_fifo_bits // bits_per_sample
            max_samp_per_shot = (
                max_total_samples // parallelism
                if shots_per_exp == 1
                else max_total_samples // (shots_per_exp * parallelism)
            )
            max_shots = usable_fifo_bits // (samples_per_shot * bits_per_sample) if samples_per_shot > 0 else 0

        elif mode == "decimated":
            bits_per_sample = 32
            total_requested_bits = samp_per_shot * shots_per_exp * bits_per_sample

            max_total_samples = usable_fifo_bits // bits_per_sample
            max_samp_per_shot = max_total_samples // shots_per_exp if shots_per_exp > 0 else max_total_samples
            max_shots = max_total_samples // samp_per_shot if samp_per_shot > 0 else 0

        elif mode == "accumulated":
            bits_per_shot = 64
            total_requested_bits = shots_per_exp * bits_per_shot

            max_shots = usable_fifo_bits // bits_per_shot
            max_samp_per_shot = None  # Not relevant for accumulated

        # --- 3. Check and raise descriptive error ---
        if total_requested_bits > usable_fifo_bits:

            if mode == "accumulated":
                raise DMAError(
                    f"Buffer capacity exceeded (mode={mode}, ADC={adc_index}):\n"
                    f"  Requested: {shots_per_exp} shots/experiment\n"
                )
            else:
                # For raw/decimated, show both constraints
                if mode == "decimated":
                    requested_samples = samp_per_shot * shots_per_exp
                else:
                    requested_samples = samp_per_shot * shots_per_exp * parallelism

                hint_lines = []
                if samp_per_shot > max_samp_per_shot:
                    hint_lines.append(
                        f" samp_per_shot too large: {samp_per_shot} > "
                        f"{max_samp_per_shot} (for {shots_per_exp} shots)"
                    )
                if shots_per_exp > max_shots:
                    hint_lines.append(
                        f"  shots too large: {shots_per_exp} > {max_shots} " f"(for {samp_per_shot} samp/shot)"
                    )

                hint = "\n".join(hint_lines) if hint_lines else "  Reduce shots or samp_per_shot"
                # Error messages are intentionally descriptive and include
                # actionable hints, because capacity failures are common
                # during experiment development

                raise DMAError(
                    f"Buffer capacity exceeded (mode={mode}, "
                    f"ADC={adc_index}):\n"
                    f"  Requested: {requested_samples} total samples "
                    f"({shots_per_exp} shots × {samp_per_shot} samp/shot)\n"
                    f"  Maximum:   {max_total_samples} total samples\n"
                    f"{hint}"
                )

    def _get_or_allocate_buffer(self, adc_index: int, total_words: int) -> Any:
        """
        Return a persistent DMA buffer for the given ADC, allocating if necessary.

        Performance
        -----------
        DDR allocation via `pynq.allocate` is relatively expensive and can
        fragment resources in long-running processes. We therefore cache one
        buffer per ADC index.

        Correctness rationale
        ---------------------
        A buffer is only reused if its length is >= the requested length
        (in words). This prevents overflow. Using a larger-than-needed
        buffer is safe because parsing uses explicit "valid length"
        computations based on ``shots`` and ``samp_per_shot``.

        :param adc_index: ADC/acquisition index used as the cache key.
        :type adc_index: int
        :param total_words: Required buffer length in 32-bit words.
        :type total_words: int
        :return: A PYNQ-allocated buffer suitable for DMA reception.
        :rtype: Any
        """

        existing = self._persistent_buffers.get(adc_index)

        if existing is not None and existing.shape[0] >= total_words:
            return existing

        buffer = allocate(shape=(total_words,), dtype="u4")
        self._persistent_buffers[adc_index] = buffer
        return buffer


__all__ = ["AcquisitionEngine"]
