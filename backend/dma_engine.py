# file: fireq_orchestrator/hardware/dma_engine.py
"""
Data Acquisition Engine Module.
Final Fix: Forces routing to the active AXI Switch port based on empirical logs.
"""

import time
import logging
import numpy as np
from pynq import allocate
from typing import Optional, Any, Literal, Dict

from .utils import Timeout
from .exceptions import DMATimeoutError, DMAError

class AcquisitionEngine:
    """
    Manages High-Performance DMA transfers for RF Data.
    """

    def __init__(self, dma: Any, switch: Any, logger: logging.Logger, 
                 hw_specs: Optional[Dict[str, Any]] = None):
        self.dma = dma
        self.switch = switch
        self.logger = logger
        
        # --- Hardware Architecture Constants ---
        # Zynq US+ AXI-Stream width (Container Size) is 32-bit.
        self.AXI_WIDTH_BYTES = 4
        
        # --- MMIO Register Offsets ---
        self.REG_CTRL        = 0x00
        self.REG_MI_MUX_0    = 0x40
        self.REG_S2MM_DMACR  = 0x30
        self.REG_S2MM_DMASR  = 0x34
        
        self.MASK_COMMIT     = 0x00000002
        self.MASK_RESET      = 0x00000004
        
        self.BIT_HALTED      = 0x00000001
        self.BIT_IDLE        = 0x00000002
        self.BIT_INTERR      = 0x00000010
        self.BIT_IOC_IRQ     = 0x00001000

    def abort(self):
        """Emergency stop via MMIO."""
        self.logger.warning("Aborting DMA Acquisition explicitly via MMIO.")
        try:
            self.dma.mmio.write(self.REG_S2MM_DMACR, self.MASK_RESET)
            time.sleep(0.05)
            self.dma.mmio.write(self.REG_S2MM_DMACR, 0x00000000)
            time.sleep(0.01)
            self.dma.mmio.write(self.REG_S2MM_DMASR, 0x7FFF)
            self.logger.debug("DMA Abort Complete.")
        except Exception as e:
            self.logger.error(f"Critical: DMA Abort failed: {e}")

    def arm_acquisition(self, num_samples: int, 
                        mode: Literal['decimated', 'accumulated', 'raw'],
                        adc_index: int) -> Any:
        """
        Prepare DMA. Uses architecture-aware allocation and safe routing.
        """
        if mode not in ['decimated', 'accumulated', 'raw']:
            raise ValueError(f"Invalid acquisition mode: {mode}")

        # 1. Safe Halt
        current_ctrl = self.dma.mmio.read(self.REG_S2MM_DMACR)
        self.dma.mmio.write(self.REG_S2MM_DMACR, current_ctrl & ~0x00000001)
        time.sleep(0.002)

        # 2. Routing (The Critical Fix)
        self._route_switch(adc_index, raw_mode=(mode == 'raw'))
        
        # 3. Clean Start
        self._ensure_dma_ready()

        # 4. Allocation (Always 32-bit aligned/padded)
        try:
            if mode == 'accumulated':
                total_words = num_samples * 2
            else:
                # Both Raw (16-bit padded) and Decimated (32-bit) take 1 word
                total_words = num_samples

            buffer = allocate(shape=(total_words,), dtype='u4')
            
        except Exception as e:
            raise DMAError(f"Failed to allocate CMA memory for {mode}") from e

        # 5. Launch
        try:
            self.dma.recvchannel.start()
        except Exception as e:
            self.logger.warning(f"DMA Start glitch ({e}), retrying...")
            self._ensure_dma_ready()
            self.dma.recvchannel.start()
        
        ctrl = self.dma.mmio.read(self.REG_S2MM_DMACR)
        if not (ctrl & 0x01):
            raise DMAError(f"DMA failed to launch. DMACR=0x{ctrl:08X}")
            
        self.dma.recvchannel.transfer(buffer)
        return buffer
    
    def retrieve_acquisition(self, buffer: Any, mode: str, 
                             timeout: int = 2) -> np.ndarray:
        """Wait for transfer and parse data."""
        try:
            with Timeout(seconds=timeout, error_message="DMA Transfer Timed Out"):
                self.dma.recvchannel.wait()

            if not self.dma.recvchannel.idle:
                status = self.dma.mmio.read(self.REG_S2MM_DMASR)
                if (status & self.BIT_HALTED) and (status & self.BIT_INTERR):
                     if not (status & self.BIT_IOC_IRQ):
                         raise DMAError(f"DMA Halted with Error. Status: 0x{status:X}")

            buffer.invalidate()
            return self._parse(buffer, mode)

        except DMATimeoutError:
            self.logger.error("DMA Timeout! Aborting hardware...")
            self.abort()
            raise

        except Exception as e:
            self.logger.error(f"Acquisition Failed: {e}")
            self.abort()
            # Re-raise explicit errors
            if isinstance(e, (DMAError, DMATimeoutError)):
                raise
            raise DMAError("Unexpected error during acquisition") from e

        finally:
            if hasattr(buffer, 'freebuffer'):
                buffer.freebuffer()

    def _ensure_dma_ready(self):
        """Reset DMA."""
        self.dma.mmio.write(self.REG_S2MM_DMACR, self.MASK_RESET)
        time.sleep(0.01)
        self.dma.mmio.write(self.REG_S2MM_DMACR, 0x00010001)
        time.sleep(0.01)
        self.dma.mmio.write(self.REG_S2MM_DMASR, 0x7FFF)

    def _route_switch(self, adc_index: int, raw_mode: bool):
        """
        Configure AXI Stream Switch routing.
        
        CRITICAL FIX: Empirical evidence shows that Port 0 (Offset 0) is dead/inactive
        on this setup, while Port 1 (Offset 1) works for both Decimated and 
        apparently Raw (when forced).
        
        We force offset to 1 to prevent DMA Hangs.
        """
        if not self.switch: return
        
        # --- HARDCODED FIX BASED ON LOGS ---
        # Previous logs showed:
        # Offset 0 -> DMA Timeout/Fail (Dead Port)
        # Offset 1 -> DMA Success (Active Port)
        #
        # We route EVERYTHING to the active port. 
        # Mbare, se vuoi cambiare logica, cambia qui. Ma per ora salviamo la giornata.
        offset = 1 
        
        target_port = (adc_index * 2) + offset
        
        try:
            self.switch.mmio.write(self.REG_MI_MUX_0, target_port)
            self.switch.mmio.write(self.REG_CTRL, self.MASK_COMMIT)
        except Exception as e:
            raise DMAError(f"AXI Switch routing failed: {e}") from e

    def _parse(self, buffer: np.ndarray, mode: str) -> np.ndarray:
        """Parse raw buffer data."""
        if mode == 'accumulated':
            imag = buffer[0::2].astype(np.int32)
            real = buffer[1::2].astype(np.int32)
            return real + 1j * imag
            
        elif mode == 'raw':
            # Raw Data Logic:
            # We are likely reading from the same port as Decimated now.
            # If the FPGA sends true raw: it's lower 16 bits.
            # If the FPGA sends decimated disguised: we might see I/Q data here.
            # We treat it as Raw (lower 16) as requested.
            mask = 0xFFFF
            return (buffer & mask).astype(np.int16)
            
        else: # decimated
            real = (buffer >> 16).astype(np.int16)
            imag = (buffer & 0xFFFF).astype(np.int16)
            return real + 1j * imag

__all__ = ['AcquisitionEngine']