# file: fireq_orchestrator/hardware/dma_engine.py
"""
Data Acquisition Engine Module - no Flush Version
===============================================================
- PYNQ high-level DMA acquisition engine.
- Maintains:
    * multi-shot handling
    * mode-based buffer dimensioning
    * AXI Stream Switch routing
    * parsing I/Q in complex arrays
=================================================================
"""

import logging
import numpy as np
import signal  # for timeout handling
from pynq import allocate
from typing import Optional, Any, Literal, Dict

from .exceptions import DMATimeoutError, DMAError 
class AcquisitionEngine:
    """
    High-level manager for DMA acquisitions.

    Responsibilities:
    - Configure stream routing (raw vs decimated/accumulated) via AXI Stream Switch.
    - Allocate contiguous DDR buffers with `pynq.allocate`.
    - Start the DMA S2MM channel with `recvchannel.transfer(...)`.
    - Wait for completion with `recvchannel.wait()`.
    - Parse the buffer into complex I/Q arrays, with multi-shot reshaping.

    """

    def __init__(self, dma: Any, switch: Any, logger: Optional[logging.Logger] = None,
                 hw_specs: Optional[Dict[str, Any]] = None) -> None:
        """
        :param dma: PYNQ DMA (e.g., overlay.axi_dma_0)
        :param switch: AXI Stream Switch IP used to select the path (raw vs decimated)
        :param logger: Optional logger
        :param hw_specs: Optional dictionary with hardware specifications (adc_parallelism, etc.)
        """
        self.dma = dma
        self.switch = switch
        self.logger = logger or logging.getLogger(__name__)
        self.hw_specs = hw_specs or {}

        # Switch register definitions
        #NOTE: evaluate to move elsewhere (inventory module?)
        self.REG_CTRL = 0x00
        self.REG_MI_MUX_0 = 0x40
        self.MASK_COMMIT = 0x00000002
        # --- DMA (S2MM) registers for "emergency" reset ---
        self.REG_S2MM_DMACR = 0x30
        self.REG_S2MM_DMASR = 0x34
        self.MASK_RESET = 0x00000004   # bit Reset
        self.MASK_IRQ_CLEAR = 0x00007000  # W1C su IOC, DM, ERR (valore classico)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

        
    def abort(self) -> None:
        """
        Aborts any ongoing DMA transfer and performs a minimal hardware reset.
        Useful in case of errors or timeouts to bring the DMA back to a known state.
        """
        try:
            # prova a fermare gentilmente il canale PYNQ
            if hasattr(self.dma.recvchannel, "stop"):
                self.dma.recvchannel.stop()
        except Exception:
            pass

        # e comunque fai il reset hardware minimale
        self._hard_reset()


    def arm_acquisition(
        self,
        samp_per_shot: int,
        shots: int,
        mode: Literal["raw", "decimated", "accumulated"],
        adc_index: int,
    ) -> Any:
        """
        Configures routing and DMA and starts the transfer.

        Does not wait for completion (to do so, use `retrieve_acquisition`).

        :param samp_per_shot: Number of samples desired per shot (interpretation
                              depends on the mode, as per the firmware).
        :param shots: Number of shots to aggregate in the buffer.
        :param mode: 'raw', 'decimated', 'accumulated'
        :param adc_index: Index of the ADC from which we are reading (used for the mux).
        :return: DMA buffer (allocate object) ready to be passed to `retrieve_acquisition`.
        """
        if shots < 1:
            raise DMAError("Shots must be >= 1")

        self.logger.debug(
            f"Arming DMA (simple): samples/shot={samp_per_shot}, shots={shots}, "
            f"mode={mode}, adc_index={adc_index}"
        )

        # 1. Switch routing
        self._route_switch(adc_index=adc_index, raw_mode=(mode == "raw"))

        # 2. Calculate buffer size in words (u4)
        total_words = self._compute_total_words(samp_per_shot, shots, mode)

        self.logger.debug(
            f"Allocating DMA buffer: {total_words} words for {shots} shots (Mode: {mode})"
        )

        try:
            buffer = allocate(shape=(total_words,), dtype="u4")
        except Exception as e:
            raise DMAError(f"DMA allocation failed: {e}") from e

        # 3. Start DMA (no manual MMIO)
        try:
            self.dma.recvchannel.transfer(buffer)
        except Exception as e:
            # If start fails, free the buffer to avoid leaks
            self._hard_reset()
            if hasattr(buffer, "freebuffer"):
                try:
                    buffer.freebuffer()
                except Exception:
                    pass
            raise DMAError(f"DMA start failed: {e}") from e

        return buffer

    def retrieve_acquisition(
        self,
        buffer: Any,
        mode: str,
        shots: int,
        timeout: Optional[float] = None,
    ) -> np.ndarray:
        """
        Waits for DMA completion and retrieves data as complex numpy array.

        Timeout handling:
            - If `timeout` is None or <= 0 block until the DMA finishes or raises an exception.
            - If `timeout` > 0 (seconds): we arm a UNIX signal-based timer
              (ITIMER_REAL). If `recvchannel.wait()` does not return within the
              given time, a TimeoutError is raised and converted into a
              DMATimeoutError, and a minimal DMA hard-reset is performed.
        :param buffer: DMA buffer previously allocated and passed to `arm_acquisition`.
        :param mode: 'raw', 'decimated', 'accumulated'
        :param shots: Number of shots to aggregate in the buffer.
        :param timeout: Optional timeout in seconds for the wait operation.
        :return: numpy array with complex data (reshaped if shots > 1).
        """
        # --- Setup optional timeout via signals (UNIX only) ---
        timeout_sec: Optional[float] = None
        old_handler = None

        if timeout is not None and timeout > 0:
            timeout_sec = float(timeout)
            # Define the timeout handler
            def _timeout_handler(signum, frame):
                raise TimeoutError("DMA wait timeout")

            # Save the old handler to restore it later
            old_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, _timeout_handler)
            # Use ITIMER_REAL to support fractional timeouts
            signal.setitimer(signal.ITIMER_REAL, timeout_sec)

        try:
            # Block until DMA finishes / until timeout handler fires
            self.dma.recvchannel.wait()
        except TimeoutError as e:
            # Timeout: reset DMA and signal DMATimeoutError
            self._hard_reset()

            if hasattr(buffer, "freebuffer"):
                try:
                    buffer.freebuffer()
                except Exception:
                    pass

            raise DMATimeoutError(
                f"DMA transfer timed out after {timeout_sec:.3f} s"
            ) from e

        except Exception as e:
            # Altro errore PYNQ/DMA: reset e DMAError
            self._hard_reset()

            if hasattr(buffer, "freebuffer"):
                try:
                    buffer.freebuffer()
                except Exception:
                    pass

            raise DMAError(f"DMA transfer failed: {e}") from e

        finally:
            # Restore old signal handler and disable timer
            # The handler is restored such that it can be used again later without re-definition
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

        # Invalidate buffer cache (best-effort)
        try:
            buffer.invalidate()
        except Exception:
            pass

        try:
            data = self._parse(buffer, mode, shots)
        finally:
            # Sempre liberare il buffer dopo l'uso
            if hasattr(buffer, "freebuffer"):
                try:
                    buffer.freebuffer()
                except Exception:
                    pass

        return data

    # ------------------------------------------------------------------
    # Internal methods: routing, dimensions, parsing, reset
    # ------------------------------------------------------------------

    def _compute_total_words(
        self,
        samp_per_shot: int,
        shots: int,
        mode: Literal["raw", "decimated", "accumulated"],
    ) -> int:
        """
        Calcola il numero totale di word (u32) da allocare in memoria
        in base a:
        - numero di sample per shot,
        - numero di shot,
        - formato dati del firmware (mode).
        """
        if mode == "accumulated":
            # Accumulated: un risultato complesso per shot (I e Q a 32 bit ciascuno)
            words_per_shot = 2  # I, Q
            total_words = words_per_shot * shots
            self.logger.debug(
                f"Accumulated mode: {words_per_shot} words/shot, total={total_words}"
            )
            return total_words

        if mode == "raw":
            # Raw: stream demodulato full-bandwidth.
            # Ogni sample complesso è codificato in un'unica word (I16 | Q16).
            # Tuttavia l'hardware produce i sample a blocchi di `adc_parallelism`.
            par = int(self.hw_specs.get("adc_parallelism", 8))
            samples_per_shot = int(samp_per_shot)

            # Ceiling per garantire di avere almeno samp_per_shot sample
            acq_cycles_per_shot = (samples_per_shot + par - 1) // par
            actual_samples_per_shot = acq_cycles_per_shot * par
            total_words = actual_samples_per_shot * shots

            self.logger.debug(
                f"Raw mode: requested={samples_per_shot} samples/shot -> "
                f"actual={actual_samples_per_shot} samples/shot, total_words={total_words}"
            )
            return total_words

        # Decimated: 1 sample complesso = 1 word a 32 bit (I16 | Q16)
        samples_per_shot = int(samp_per_shot)
        total_words = samples_per_shot * shots
        self.logger.debug(
            f"Decimated mode: {samples_per_shot} words/shot, total={total_words}"
        )
        return total_words

    def _route_switch(self, adc_index: int, raw_mode: bool) -> None:
        """
        Configura l'AXI Stream Switch per selezionare il path corretto.

        Convenzione:
            - Porta pari  (2*adc_index)     -> RAW (full bandwidth)
            - Porta dispari (2*adc_index+1) -> Decimated / Accumulated

        Questa funzione è volutamente semplice:
            - nessun flush di pipeline
            - nessun reset DMA
            - solo scrittura del registro di mux + commit.
        """
        if not self.switch:
            return

        base_port = int(adc_index) * 2
        target_port = base_port + (0 if raw_mode else 1)

        self.logger.info(
            f"Routing AXI switch: adc={adc_index}, raw_mode={raw_mode} -> port={target_port}"
        )

        try:
            # Scrivi il nuovo valore di mux e committa
            self.switch.mmio.write(self.REG_MI_MUX_0, target_port)
            self.switch.mmio.write(self.REG_CTRL, self.MASK_COMMIT)
        except Exception as e:
            raise DMAError(f"AXI switch routing failed: {e}") from e

    def _parse(self, buffer: Any, mode: str, shots: int) -> np.ndarray:
        """
        Converts the raw DMA buffer into a complex numpy array.
        Ease the handling of data once retrieved from the DMA.
        Data formats (from firmware):
        - decimated/raw:
            32 bit per complex sample:
                [31:16] = Q (signed int16)
                [15:00] = I (signed int16)
        - accumulated:
            I and Q separate, 32 bit each:
                word[0] = I0, word[1] = Q0, word[2] = I1, word[3] = Q1, ...
        """
        if mode == "accumulated":
            # I, Q:  32 bit, alternated
            i_data = buffer[0::2].astype(np.int32)
            q_data = buffer[1::2].astype(np.int32)
            complex_data = i_data + 1j * q_data

        elif mode in ("decimated", "raw"):
            # I16 | Q16 in a single word
            raw_u4 = buffer.view(np.uint32)
            i_data = (raw_u4 & 0xFFFF).astype(np.int16)
            q_data = (raw_u4 >> 16).astype(np.int16)
            complex_data = i_data + 1j * q_data

        else:
            raise DMAError(f"Unknown acquisition mode for parsing: {mode}")

        # --- Gestione multi-shot ---
        if shots > 1:
            total_len = len(complex_data)
            samples_per_shot = total_len // shots
            if samples_per_shot == 0:
                # Fall-back: niente reshape sensato possibile
                self.logger.warning(
                    f"Cannot reshape DMA data for {shots} shots: total_len={total_len}"
                )
                return complex_data

            trimmed = complex_data[: samples_per_shot * shots]
            try:
                return trimmed.reshape((shots, samples_per_shot))
            except Exception as e:
                self.logger.warning(
                    f"Reshape failed for {shots} shots (samples_per_shot={samples_per_shot}): {e}. "
                    "Returning flat array."
                )
                return complex_data

        return complex_data

    def _hard_reset(self) -> None:
        """
        Reset 'minimale' del canale S2MM del DMA.

        Non tocca lo switch, non fa flush: serve solo a riportare il core
        in stato sano dopo un errore, evitando di dover riflashare l'overlay.
        """
        try:
            mmio = self.dma.mmio

            # 1) Set the reset bit
            mmio.write(self.REG_S2MM_DMACR, self.MASK_RESET)
            # 2) Clear the reset bit (becomes operational again)
            mmio.write(self.REG_S2MM_DMACR, 0x00000000)
            # 3) Clear interrupts / error flags (write-1-to-clear)
            mmio.write(self.REG_S2MM_DMASR, self.MASK_IRQ_CLEAR)

            # 4) Reset internal PYNQ state (important for the first transfer)
            if hasattr(self.dma.recvchannel, "_first_transfer"):
                self.dma.recvchannel._first_transfer = True

            self.logger.info("DMA S2MM hard-reset completed.")
        except Exception as e:
            self.logger.error(f"DMA hard-reset failed: {e}")
            raise DMAError(f"DMA hard-reset failed: {e}") from e

__all__ = ["AcquisitionEngine"]
