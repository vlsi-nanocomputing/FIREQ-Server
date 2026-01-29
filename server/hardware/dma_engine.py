# file: fireq-utils/server/dma_engine.py
"""High-level DMA acquisition utilities for the FIREQ server.

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
import sys
import time
from typing import Literal, NoReturn

import numpy as np
from pynq import allocate

from ..models.exceptions import DMAError, DMATimeoutError

# Zero-copy parsing requires little-endian byte order (ARM/Zynq is little-endian)
assert sys.byteorder == "little", "Zero-copy DMA parsing requires little-endian system"


class AcquisitionEngine:
    """High-level manager for DMA acquisitions.

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
        self,
        dma: object,
        switch: object,
        logger: logging.Logger | None = None,
        hw_specs: dict[str, object] | None = None,
    ) -> None:
        """Construct an acquisition engine bound to a specific DMA + stream switch.

        :param dma: PYNQ DMA instance
        :type dma: object
        :param switch: AXI Stream Switch IP used to route the selected ADC/mode stream
            into the DMA.
        :type switch: object
        :param logger: Optional logger. If not provided, a module logger is used.
        :type logger: Optional[logging.Logger]
        :param hw_specs: Hardware specification dictionary describing acquisition IP
            properties. This is treated as the "single source of truth" for buffer
            sizing and limits.
        :type hw_specs: Dict[str, object]
        :raises DMAError: If the DMA channel cannot be started (indicates invalid
            overlay wiring or a broken DMA object).
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
        self._reset_on_next_arm = False
        # Last successful DMA wait duration (seconds). Set to 0.0 on entry.
        self.last_dma_wait_s = 0.0
        # Detailed timing: buffer invalidation (seconds).
        self.last_invalidate_s: float = 0.0
        # Memoization for stream routing to avoid redundant MMIO writes.
        self._last_routed_port: int | None = None
        # Cache SIGALRM availability check (Unix only).
        self._has_sigalrm: bool = hasattr(signal, "SIGALRM")

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
        """Abort an in-flight DMA acquisition and force the hardware into a known state.

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
        """Release all persistent DMA buffers allocated by this instance.

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

    def set_active_adcs(self, adc_indices: list[int]) -> None:
        """Update which ADCs are active and free buffers for inactive ADCs.

        This method should be called before arm_acquisition() when the set of
        active ADCs changes (e.g., switching between single-ADC and dual-ADC
        experiments). Buffers for ADCs that are no longer in the active set
        are freed to reduce memory usage.

        :param adc_indices: List of ADC indices that will be used.
        :type adc_indices: list[int]
        """
        new_active = set(adc_indices)

        # Free buffers for ADCs that are no longer active
        for adc_idx in list(self._persistent_buffers.keys()):
            if adc_idx not in new_active:
                buf = self._persistent_buffers.pop(adc_idx, None)
                if buf is not None and hasattr(buf, "freebuffer"):
                    try:
                        buf.freebuffer()
                        self.logger.debug(f"Freed buffer for inactive ADC {adc_idx}")
                    except Exception as e:
                        self.logger.warning(f"Failed to free buffer for inactive ADC {adc_idx}: {e}")

    def _get_fifo_params(
        self,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
    ) -> tuple[int, int]:
        """Return FIFO depth and width for the given mode and ADC.

        :param mode: Acquisition mode.
        :type mode: Literal["raw", "decimated", "accumulated"]
        :param adc_index: ADC/acquisition IP index.
        :type adc_index: int
        :return: Tuple of (fifo_depth_words, fifo_width_bits).
        :rtype: tuple[int, int]
        :raises DMAError: If mode is unknown.
        """
        acq_spec = self.hw_specs["acquisitions"][adc_index]

        if mode == "raw":
            fifo_depth = int(acq_spec.get("raw_fifo_depth_words", 0))
            fifo_width = int(acq_spec.get("raw_output_width_bits", 256))
        elif mode in ("decimated", "accumulated"):
            fifo_depth = int(acq_spec.get("decimated_fifo_depth_words", 0))
            fifo_width = int(acq_spec.get("dec_output_width_bits", 64))
        else:
            raise DMAError(f"Unknown mode: {mode}")

        return fifo_depth, fifo_width

    def get_max_shots(
        self,
        mode: Literal["raw", "decimated", "accumulated"],
        samp_per_shot: int,
        adc_index: int,
    ) -> int:
        """Compute the maximum number of shots that can fit in the acquisition FIFO/buffer.

        :param mode: Acquisition output mode.
        :type mode: Literal["raw", "decimated", "accumulated"]
        :param samp_per_shot: Samples per shot (scales by parallelism for raw mode).
        :type samp_per_shot: int
        :param adc_index: Acquisition IP index.
        :type adc_index: int
        :return: Maximum shots that fit without overflow (0 if degenerate).
        :rtype: int
        """
        fifo_depth, fifo_width = self._get_fifo_params(mode, adc_index)
        total_bits = fifo_depth * fifo_width

        if mode == "accumulated":
            return total_bits // 64
        elif mode == "decimated":
            bits_per_shot = samp_per_shot * 32
            return total_bits // bits_per_shot if bits_per_shot > 0 else 0
        else:  # raw
            acq_spec = self.hw_specs["acquisitions"][adc_index]
            parallelism = int(acq_spec.get("parallelism", 1))
            bits_per_shot = samp_per_shot * parallelism * 32
            return total_bits // bits_per_shot if bits_per_shot > 0 else 0

    def __del__(self) -> None:
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
    ) -> object:
        """Arm a DMA acquisition: validate, route, allocate/reuse buffer, and start DMA.

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
        :rtype: object

        :raises DMAError:
            On invalid sizes, invalid mode, or inability to start DMA transfer.
        """
        skip_idle_check = False

        if self._reset_on_next_arm:
            self.logger.debug("Clearing _reset_on_next_arm flag (no hard reset, skip idle check).")
            self._reset_on_next_arm = False
            # Skip idle check to avoid triggering hard reset if DMA reports non-idle
            # briefly after sweep end
            skip_idle_check = True

        # "Fast path "is correct if the caller keeps mode and sizing invariants stable
        # across iterations.
        if self._sweep_prepared and self._sweep_mode == mode:
            return self._arm_acquisition_fast(mode, adc_index)
        # Full path
        return self._arm_acquisition_full(samp_per_shot, shots_per_exp, mode, adc_index, skip_idle_check)

    def retrieve_acquisition(
        self,
        buffer: object,
        timeout: float | None = None,
        skip_timeout: bool = False,
    ) -> np.ndarray:
        """Wait for DMA completion, apply timeout protection, and return buffer.

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
        :type buffer: object
        :param timeout:
            Timeout in seconds. If ``None`` or non-positive, no timeout is enforced.
        :type timeout: Optional[float]
        :param skip_timeout:
            If True, skip internal signal handler setup (caller manages timeout externally).
        :type skip_timeout: bool
        :return:
            Full DMA buffer. Client computes valid_words from request params.
        :rtype: np.ndarray

        :raises DMATimeoutError:
            If the DMA wait exceeds the timeout.
        :raises DMAError:
            For other DMA failures.
        """
        # Reset timing accumulators
        self.last_dma_wait_s = 0.0
        self.last_invalidate_s = 0.0

        # --- Setup optional timeout via signals (UNIX only) ---
        timeout_sec: float | None = None
        old_handler = None

        if not skip_timeout and timeout is not None and timeout > 0:
            if self._has_sigalrm:
                timeout_sec = float(timeout)

                def _timeout_handler(signum: int, frame: object) -> NoReturn:
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
            raise DMATimeoutError(f"DMA transfer timed out after {timeout_sec:.3f} s", timeout_sec) from e

        except Exception as e:
            # Any other error (e.g. RuntimeError: DMA channel not started)
            # implies the state is inconsistent. Reset.
            self._hard_reset()
            self.free_resources()
            self.logger.error(f"DMA Runtime Error: {e}. Resetting core.")
            raise DMAError(f"DMA transfer failed: {e}") from e

        finally:
            # Restore signal handler (only if we set one up)
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

        # Invalidate buffer cache (timed)
        t_invalidate_start = time.perf_counter()
        try:
            buffer.invalidate()
        except Exception:
            pass
        self.last_invalidate_s = time.perf_counter() - t_invalidate_start

        # Return full buffer (client computes valid_words from request params)
        return buffer

    # ------------------------------------------------------------------
    # Acquisition methods : full validation for non-sweep experiments
    # ------------------------------------------------------------------

    def _arm_acquisition_full(
        self,
        samp_per_shot: int,
        shots_per_exp: int,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
        skip_idle_check: bool = False,
    ) -> object:
        """Conservative arm path: validate capacity, check DMA state, route stream, allocate buffer, start DMA.

        This method is intentionally strict. The validation step is
        performed *before* routing/starting DMA so that configuration errors
        are reported without mutating hardware state.

        :param skip_idle_check: If True, skip the DMA idle check. Used when
            the caller has just performed a hard reset and the DMA may briefly
            report non-idle state during the start sequence.
        :type skip_idle_check: bool

        :raises DMAError:
            If parameters are invalid, capacity is exceeded, DMA is not
            startable, or transfer fails.
        """
        if shots_per_exp < 1:
            raise DMAError("shots_per_exp must be >= 1")

        # 1. Validation - check capacity using get_max_shots
        max_shots = self.get_max_shots(mode, samp_per_shot, adc_index)
        if shots_per_exp > max_shots:
            raise DMAError(
                f"Buffer capacity exceeded (mode={mode}, ADC={adc_index}): "
                f"requested {shots_per_exp} shots, max is {max_shots} "
                f"(for {samp_per_shot} samp/shot)"
            )

        self.logger.debug(
            f"Arming DMA: samp/shot={samp_per_shot}, shots/exp={shots_per_exp}, " f"mode={mode}, adc={adc_index}"
        )

        # 2. DMA state check
        # DMA may report a non-idle state after a previous run (or an error).
        # Starting a new transfer while not idle is undefined; actively reset
        # to avoid timeouts and partial data.
        # Skip this check if we just performed a reset (the DMA may briefly
        # report non-idle during the start sequence after a hard reset).
        if not skip_idle_check and not self.dma.recvchannel.idle:
            self.logger.warning("DMA not idle before arm. Forcing hard reset.")
            self._hard_reset()

        # 3. Switch routing
        self._route_switch(adc_index=adc_index, raw_mode=(mode == "raw"))

        # 4. Buffer allocation (size = FIFO capacity in 32-bit words)
        fifo_depth, fifo_width = self._get_fifo_params(mode, adc_index)
        total_words = fifo_depth * (fifo_width // 32)
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
        """Enable sweep mode optimizations for repeated acquisitions.

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
        """Disable sweep mode and return to conservative validation behavior."""
        self._sweep_prepared = False
        self._sweep_mode = None
        self._reset_on_next_arm = True
        # Reset routing memoization so next acquisition re-routes.
        self._last_routed_port = None

    def _arm_acquisition_fast(
        self,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
    ) -> object:
        """Optimized sweep arm path.

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

        # 2. Reuse existing buffer or fallback to full allocation
        buffer = self._persistent_buffers.get(adc_index)
        if buffer is None:
            # Fallback to full allocation if not pre-allocated
            fifo_depth, fifo_width = self._get_fifo_params(mode, adc_index)
            total_words = fifo_depth * (fifo_width // 32)
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
        """Ensure the PYNQ DMA recvchannel is started.

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

    def _route_switch(self, adc_index: int, raw_mode: bool) -> None:
        """Route the AXI Stream Switch to select the desired ADC and output mode.

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

        # Port mapping is a *bitstream-level contract*. Hardcoded swap for this bitstream:
        # acq0 -> base port 2 (raw=2, dec/acc=3), acq1 -> base port 0 (raw=0, dec/acc=1).
        adc_idx = int(adc_index)
        hard_map = {0: 2, 1: 0}
        base_port = hard_map.get(adc_idx, adc_idx * 2)
        target_port = base_port + (0 if raw_mode else 1)

        # Skip MMIO writes if routing hasn't changed (memoization).
        if target_port == self._last_routed_port:
            return

        try:
            self.switch.mmio.write(self.REG_MI_MUX_0, target_port)
            self.switch.mmio.write(self.REG_CTRL, self.MASK_COMMIT)
            self._last_routed_port = target_port
        except Exception as e:
            raise DMAError(f"AXI switch routing failed: {e}") from e

    def _hard_reset(self) -> None:
        """Perform a robust MMIO-based reset of the AXI DMA channel.

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
        :class:`DMAError`. At that point, the most likely cause is
        inconsistent overlay state.
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

            # 7. Reset routing memoization so next acquisition re-routes.
            self._last_routed_port = None

            self.logger.debug("DMA S2MM Hard Reset Completed.")

        except Exception as e:
            self.logger.error(f"Critical Failure during DMA Reset: {e}")
            raise DMAError(f"Critical Failure during DMA Reset: {e}") from e

    def _get_or_allocate_buffer(self, adc_index: int, total_words: int) -> object:
        """Return a persistent DMA buffer for the given ADC, allocating if necessary.

        Buffer size is always the full FIFO capacity. We cache one buffer per
        ADC index to avoid repeated allocations.

        :param adc_index: ADC/acquisition index used as the cache key.
        :type adc_index: int
        :param total_words: Buffer length in 32-bit words (full FIFO capacity).
        :type total_words: int
        :return: A PYNQ-allocated buffer suitable for DMA reception.
        :rtype: object
        """
        existing = self._persistent_buffers.get(adc_index)

        if existing is not None and existing.shape[0] >= total_words:
            return existing

        # Free old buffer if it exists but is too small
        if existing is not None and hasattr(existing, "freebuffer"):
            existing.freebuffer()

        buffer = allocate(shape=(total_words,), dtype="u4")
        self._persistent_buffers[adc_index] = buffer
        return buffer


__all__ = ["AcquisitionEngine"]
