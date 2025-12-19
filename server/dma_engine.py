# file: fireq-utils/server/hardware/dma_engine.py
"""
DMA-Engine.

This module provides a high-level interface for managing Direct Memory Access (DMA) 
transfers between FPGA-based Analog-to-Digital Converters (ADCs) and the 
Processing System (PS). 
It handles AXI-Stream routing via AXI-Switch, hardware buffer allocation
and real-time data parsing for different acquisition modalities (raw, decimated, accumulated).

"""

import logging
import numpy as np
import time
import signal
from pynq import allocate
from typing import Optional, Any, Literal, Dict, List

from .exceptions import DMATimeoutError, DMAError 


class AcquisitionEngine:
    """
    High-level manager for AXI acquisitions.

    This class encapsulates the logic for hardware-timed data capture, managing 
    the synchronization between the AXI-Stream switch and the DMA engine. 
    It supports multi-shot acquisition and automatic data reshaping into 
    complex-valued values.

    :ivar dma: The PYNQ DMA overlay object.
    :ivar switch: The AXI-Stream Switch object for signal routing.
    :ivar logger: Logger instance for experiment status and error reporting.
    :ivar hw_specs: Dictionary containing hardware constraints.
    :ivar _inflight: Boolean flag indicating an ongoing DMA transfer.
    """

    def __init__(self, dma: Any, switch: Any, logger: Optional[logging.Logger] = None,
                 hw_specs: Optional[Dict[str, Any]] = None,
                 acq_drivers: Optional[List[Any]] = None) -> None:
        """
        Initialize the Acquisition Engine and hardware register offsets.

        Configures the memory-mapped I/O offsets for the AXI-Stream Switch 
        and the S2MM DMA channel. It also triggers an initial check to 
        ensure the DMA channel is in a running state.

        :param dma: PYNQ DMA object for data transfer.
        :param switch: PYNQ MMIO or IP object for AXI switching.
        :param logger: Optional logging.Logger instance.
        :param hw_specs: Dictionary of hardware parameters (e.g., ADC parallelism).
        :param acq_drivers: List of driver objects for the acquisition IP cores.
        """
        self.dma = dma
        self.switch = switch
        self.logger = logger or logging.getLogger(__name__)
        self.hw_specs = hw_specs or {}
        self.acq_drivers = acq_drivers or []
        self._inflight = False
        
        # Switch register definitions
        #NOTE : hardcoded, consider to exctract them from the board
        self.REG_CTRL = 0x00
        self.REG_MI_MUX_0 = 0x40
        self.MASK_COMMIT = 0x00000002
        
        # --- DMA (S2MM) registers ---
        self.REG_S2MM_DMACR = 0x30
        self.REG_S2MM_DMASR = 0x34
        self.MASK_RESET = 0x00000004
        self.MASK_RUNSTOP = 0x00000001
        self.MASK_IRQ_CLEAR = 0x00007000

        self._ensure_started()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def abort(self) -> None:
        """
        Immediately terminate any ongoing hardware acquisition.

        Triggers a hard reset of the DMA engine to clear the S2MM pipeline 
        and reset the internal state machines.
        """
        self._hard_reset()

    def acquire(
        self,
        samp_per_shot: int,
        shots: int,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
        timeout: Optional[float] = None
    ) -> np.ndarray:
        """
        Execute a complete acquisition cycle.

        Arms the DMA engine, waits for the transfer to complete within the 
        specified timeout, and returns the parsed data. If a failure occurs, 
        it ensures the allocated buffers are freed to prevent memory leaks.

        :param samp_per_shot: Number of samples to acquire for each trigger/shot.
        :type samp_per_shot: int
        :param shots: Total number of acquisition cycles (shots).
        :type shots: int
        :param mode: Acquisition modality: 'raw' (demodulated-not-filtered), 'decimated' (demodulated and filtered), 
                     or 'accumulated' (demodulated, filtered and integrated).
        :type mode: Literal["raw", "decimated", "accumulated"]
        :param adc_index: Hardware index of the ADC source.
        :type adc_index: int
        :param timeout: Maximum wait time in seconds. Defaults to 5.0s.
        :type timeout: Optional[float]
        :return: Reshaped array of complex samples (numpy.complex64/128).
        :rtype: np.ndarray
        :raises DMATimeoutError: If the hardware fails to assert the TLAST signal.
        :raises DMAError: For low-level AXI protocol or allocation failures.
        """

        buffer = self.arm_acquisition(samp_per_shot, shots, mode, adc_index)
        try:
            return self.retrieve_acquisition(buffer, mode, shots, timeout)
        except Exception:
            if hasattr(buffer, "freebuffer"):
                try:
                    buffer.freebuffer()
                except Exception:
                    pass
            raise

    def arm_acquisition(
        self,
        samp_per_shot: int,
        shots: int,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
    ) -> Any:
        """
        Configure hardware and initiate the DMA transfer.

        Performs AXI-Switch routing, configures the acquisition IP cores 
        (decimators/integrators), allocates contiguous memory (CMA), 
        and triggers the S2MM channel.

        :param samp_per_shot: Samples per shot.
        :type samp_per_shot: int
        :param shots: Number of shots.
        :type shots: int
        :param mode: Acquisition mode.
        :type mode: str
        :param adc_index: Source ADC channel.
        :type adc_index: int
        :return: PYNQ Buffer object (Contiguous Memory Allocation).
        :rtype: Any
        """
        try:
            if not self.dma.recvchannel.idle:
                self.logger.warning("DMA was busy/stuck before arming. Forcing Hard Reset.")
                self._hard_reset()
        except Exception:
            self._hard_reset()

        if shots < 1:
            raise DMAError("Shots must be >= 1")

        self._configure_acquisition_ip(adc_index, mode)
        self._route_switch(adc_index=adc_index, raw_mode=(mode == "raw"))

        total_words = self._compute_total_words(samp_per_shot, shots, mode)

        try:
            buffer = allocate(shape=(total_words,), dtype="u4")
        except Exception as e:
            raise DMAError(f"DMA allocation failed: {e}") from e

        try:
            self.dma.recvchannel.transfer(buffer)
        except Exception as e:
            self.logger.error(f"DMA transfer start failed: {e}")
            self._hard_reset()
            if hasattr(buffer, "freebuffer"):
                try:
                    buffer.freebuffer()
                except Exception:
                    pass
            raise DMAError(f"DMA start failed: {e}") from e

        return buffer

    def retrieve_acquisition(self, buffer, mode, shots, timeout=None):
        """
        Retrieve data from the DMA .
        It uses a SIGALRM watchdog to ensure the process does not hang if the 
        FPGA hardware fails to assert TLAST.

        :param buffer: Contiguous memory buffer where data is stored.
        :type buffer: PynqBuffer
        :param mode: Acquisition modality (raw, decimated, or accumulated).
        :type mode: str
        :param shots: Number of triggers/shots acquired.
        :type shots: int
        :param timeout: Time limit in seconds before raising a timeout error.
        :type timeout: Optional[float]
        :return: Parsed and reshaped complex data array.
        :rtype: np.ndarray
        :raises DMATimeoutError: If the transfer does not complete within the timeout.
        :raises DMAError: If the DMA engine reports an internal error during wait.
        """
        timeout_sec = int(timeout) if timeout and timeout > 0 else 5

        # Define a local timeout handler for the SIGALRM signal
        def _internal_timeout_handler(signum, frame):
            raise DMATimeoutError(f"DMA transfer timed out after {timeout_sec}s")

        # Register the signal handler and set the alarm
        old_handler = signal.signal(signal.SIGALRM, _internal_timeout_handler)
        signal.alarm(timeout_sec)

        try:
            # Wait for the hardware interrupt (TLAST) to trigger completion
            self.dma.recvchannel.wait()
        except Exception as e:
            # Perform a hard reset if the transfer fails or times out
            self._hard_reset()
            if hasattr(buffer, "freebuffer"):
                try:
                    buffer.freebuffer()
                except Exception:
                    pass
            # Re-raise the timeout or DMA error to the caller
            if "timed out" in str(e):
                raise DMATimeoutError(f"DMA transfer timed out after {timeout_sec}s") from e
            raise DMAError(f"DMA wait failed: {e}") from e
        finally:
            # Always disable the alarm and restore the previous signal handler
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        # Refresh the cache visibility for the CPU
        try:
            buffer.invalidate()
        except Exception:
            pass

        # Parse and reshape the data according to the acquisition mode
        try:
            data = self._parse(buffer, mode, shots)
        finally:
            if hasattr(buffer, "freebuffer"):
                try:
                    buffer.freebuffer()
                except Exception:
                    pass

        return data

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """
        Verify and enforce the active state of the DMA receive channel.

        Checks if the S2MM channel is running. If the standard PYNQ start 
        sequence fails, it attempts a low-level hard reset to recover 
        the hardware interface.
        """
        ch = self.dma.recvchannel
        try:
            if hasattr(ch, "running"):
                if not ch.running:
                    ch.start()
            else:
                ch.start()
        except Exception as e:
            self.logger.warning(f"Standard DMA start failed ({e}), attempting hard reset init...")
            self._hard_reset()

    def _compute_total_words(
        self,
        samp_per_shot: int,
        shots: int,
        mode: Literal["raw", "decimated", "accumulated"],
    ) -> int:
        if mode == "accumulated":
            return 2 * shots

        if mode == "raw":
            par = int(self.hw_specs.get("adc_parallelism", 8))
            samples_per_shot = int(samp_per_shot)
            acq_cycles_per_shot = (samples_per_shot + par - 1) // par
            actual_samples_per_shot = acq_cycles_per_shot * par
            return actual_samples_per_shot * shots

        return int(samp_per_shot) * shots

    def _configure_acquisition_ip(self, adc_index: int, mode: str) -> None:
        """
        Set the operational mode of the upstream acquisition IP cores.

        Communicates with the specific ADC driver to configure its output 
        type (e.g., decimated or accumulated). This ensures that the 
        AXI-Stream data format matches the expected DMA transfer size.

        :param adc_index: Index of the target ADC/Driver.
        :param mode: Desired acquisition modality.
        """
        if not self.acq_drivers:
            return
        if not (0 <= adc_index < len(self.acq_drivers)):
            return
        if mode == "raw":
            return
        if mode in ("decimated", "accumulated"):
            driver = self.acq_drivers[adc_index]
            try:
                if hasattr(driver, "set_decimated_output_type"):
                    driver.set_decimated_output_type(mode)
            except Exception:
                pass

    def _route_switch(self, adc_index: int, raw_mode: bool) -> None:
        """
        Configure the AXI-Stream Switch to route the correct signal to the DMA.

        Calculates the target port based on the ADC index and the acquisition 
        mode (raw vs. processed) and commits the configuration to the 
        Switch registers.

        :param adc_index: Source ADC index.
        :param raw_mode: If True, routes the high-speed raw data stream.
        :raises DMAError: If the AXI-Lite write to the switch fails.
        """
        if not self.switch:
            return
        base_port = int(adc_index) * 2
        target_port = base_port + (0 if raw_mode else 1)
        try:
            self.switch.mmio.write(self.REG_MI_MUX_0, target_port)
            self.switch.mmio.write(self.REG_CTRL, self.MASK_COMMIT)
        except Exception as e:
            raise DMAError(f"AXI switch routing failed: {e}") from e

    def _parse(self, buffer: Any, mode: str, shots: int) -> np.ndarray:
        """
        Convert raw binary buffer data into complex-valued NumPy tensors.

        It performs bit-masking to extract 16-bit In-phase (I) and Quadrature (Q) 
        components from 32-bit words and handles multi-shot reshaping.

        :param buffer: The source PYNQ buffer.
        :type buffer: Any
        :param mode: The acquisition mode used for specific bit-mapping logic.
        :type mode: str
        :param shots: Number of shots for tensor reshaping.
        :type shots: int
        :return: Array of complex numbers (real + 1j*imag).
        :rtype: np.ndarray
        """
        if mode == "accumulated":
            i_data = buffer[0::2].astype(np.int32)
            q_data = buffer[1::2].astype(np.int32)
            complex_data = i_data + 1j * q_data
        elif mode in ("decimated", "raw"):
            raw_u4 = buffer.view(np.uint32)
            i_data = (raw_u4 & 0xFFFF).astype(np.int16)
            q_data = (raw_u4 >> 16).astype(np.int16)
            complex_data = i_data + 1j * q_data
        else:
            raise DMAError(f"Unknown acquisition mode for parsing: {mode}")

        if shots > 1:
            total_len = len(complex_data)
            samples_per_shot = total_len // shots
            if samples_per_shot == 0:
                return complex_data
            trimmed = complex_data[: samples_per_shot * shots]
            try:
                return trimmed.reshape((shots, samples_per_shot))
            except Exception:
                return complex_data
        return complex_data

    def _hard_reset(self):
        """
        Perform a low-level reset of the AXI DMA S2MM channel.

        Writes to the DMACR register to trigger a soft reset, waits for 
        acknowledgment, and then re-enables the S2MM channel and clears 
        pending interrupts.
        """
        try:
            mmio = self.dma.mmio

            mmio.write(self.REG_S2MM_DMACR, self.MASK_RESET)
            time.sleep(0.01)

            for _ in range(100):
                if not (mmio.read(self.REG_S2MM_DMACR) & self.MASK_RESET):
                    break

            mmio.write(self.REG_S2MM_DMASR, 0x00007000)
            mmio.write(self.REG_S2MM_DMACR, self.MASK_RUNSTOP)

            ch = self.dma.recvchannel
            if hasattr(ch, '_first_transfer'):
                ch._first_transfer = True

        except Exception as e:
            raise DMAError(f"DMA hard-reset failed: {e}") from e


__all__ = ["AcquisitionEngine"]