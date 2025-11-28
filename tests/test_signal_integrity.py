import sys
import unittest
import numpy as np
from unittest.mock import MagicMock, patch

# --- FIX: MOCK PYNQ ---
# Questo blocco è necessario per ingannare Python quando non siamo sulla board FPGA
try:
    import pynq
except ImportError:
    sys.modules['pynq'] = MagicMock()
# ----------------------

from backend.driver_wrappers import GeneratorAdapter
from backend.dma_engine import AcquisitionEngine

class TestDataIntegrity(unittest.TestCase):
    """
    SUITE UNIFICATA DI INTEGRITÀ DATI.
    
    Verifica che la catena di segnale digitale (Python <-> FPGA) sia matematicamente perfetta.
    Copre sia la GENERAZIONE (DAC) che l'ACQUISIZIONE (ADC).
    """

    def setUp(self):
        """Setup comune per tutti i test."""
        # --- MOCK GENERATORE ---
        self.mock_gen_drv = MagicMock()
        self.mock_gen_drv.MaximumDuration = 65536
        self.mock_gen_drv.NumberOfChannels = 1
        self.mock_gen_drv.TriggerChannels = 4
        self.mock_gen_drv.SampleSize = 2
        self.mock_gen_drv.MemoryMappedFifoSegmentDepth = 4096
        
        self.gen_specs = {
            'dac_sr': 6e9,
            'dac_nyquist': 3e9,
            'dac_max_nyquist_zone': 2
        }
        self.generator = GeneratorAdapter(self.mock_gen_drv, self.gen_specs)

        # --- MOCK ACQUISIZIONE ---
        self.mock_dma = MagicMock()
        self.mock_dma.recvchannel.idle = True
        self.mock_dma.mmio.read.return_value = 0x0000 # Status OK
        self.mock_switch = MagicMock()
        self.mock_logger = MagicMock()
        
        self.engine = AcquisitionEngine(self.mock_dma, self.mock_switch, self.mock_logger)

    # =========================================================================
    # SEZIONE 1: INTEGRITÀ GENERAZIONE (Output verso FPGA)
    # =========================================================================

    def test_generation_integrity(self):
        """
        Testbench Generazione: Verifica che i segnali complessi inviati al DAC
        arrivino al driver bit-per-bit identici.
        """
        print("\n[Generation Integrity] Starting tests...")
        
        num_points = 100
        t = np.linspace(-3, 3, num_points)
        
        # Zoo dei Segnali di Input
        test_cases = {
            "Ramp_Complex": np.linspace(0, 1, num_points) + 1j*np.linspace(0, -1, num_points),
            "Gaussian_Pulse": np.exp(-t**2).astype(complex),
            "Rectangular_Pulse": (np.ones(num_points) * 0.5 + 0j).astype(complex),
            "Zero_Signal": np.zeros(num_points, dtype=complex),
            "White_Noise": (np.random.rand(num_points) + 1j*np.random.rand(num_points))
        }

        for name, signal in test_cases.items():
            with self.subTest(signal_type=name):
                # Reset mock
                self.mock_gen_drv.reset_mock()
                self.mock_gen_drv.add_envelope_to_envelope_memory.return_value = 0
                
                # AZIONE
                self.generator.upload_envelope(f"test_{name}", signal)
                
                # VERIFICA
                args, _ = self.mock_gen_drv.add_envelope_to_envelope_memory.call_args
                fpga_data = args[0]
                
                # 1. Tipo
                self.assertTrue(np.iscomplexobj(fpga_data), f"[{name}] Not complex!")
                # 2. Valori Esatti
                np.testing.assert_array_equal(fpga_data, signal, err_msg=f"[{name}] CORRUPTED!")
                
                print(f"  ✓ DAC: {name} Passed")

    # =========================================================================
    # SEZIONE 2: INTEGRITÀ ACQUISIZIONE (Input da FPGA)
    # =========================================================================

    def _simulate_fpga_packing(self, complex_data, mode):
        """Helper per simulare i dati grezzi provenienti dall'FPGA."""
        if mode == 'decimated':
            # 32-bit packed: High=Real, Low=Imag (int16)
            real = np.real(complex_data).astype(np.int16).astype(np.uint32)
            imag = np.imag(complex_data).astype(np.int16).astype(np.uint32)
            return ((real << 16) | (imag & 0xFFFF))
            
        elif mode == 'accumulated':
            # 64-bit split: Buffer pari=Imag, Buffer dispari=Real (int32)
            buffer_raw = np.zeros(len(complex_data) * 2, dtype=np.int32)
            buffer_raw[0::2] = np.imag(complex_data).astype(np.int32)
            buffer_raw[1::2] = np.real(complex_data).astype(np.int32)
            return buffer_raw
            
        elif mode == 'raw':
            # 16-bit low only
            return np.real(complex_data).astype(np.int16).astype(np.uint32)

    def test_acquisition_integrity(self):
        """
        Testbench Acquisizione: Verifica che i dati grezzi simulati dall'FPGA
        vengano decodificati (parsed) correttamente in numeri complessi Python.
        """
        print("\n[Acquisition Integrity] Starting tests...")
        
        # Casi limite e numeri difficili
        test_data = {
            'decimated': np.array([0, 1+1j, -1-1j, 32767-32768j, 100-500j], dtype=np.complex128),
            'accumulated': np.array([0, 100000+200000j, -500000-500000j], dtype=np.complex128),
            'raw': np.array([0, 100, -100, 32000, -32000], dtype=np.int16)
        }

        for mode, expected in test_data.items():
            with self.subTest(mode=mode):
                # A. Simuliamo l'FPGA che impacchetta i bit
                raw_data = self._simulate_fpga_packing(expected, mode)
                
                # B. Creiamo il finto buffer Pynq
                class FakeBuffer(np.ndarray):
                    def invalidate(self): pass
                    def freebuffer(self): pass
                
                buffer_view = raw_data.view(FakeBuffer)
                
                # C. Decodifica (Azione)
                decoded = self.engine.retrieve_acquisition(buffer_view, mode=mode, timeout=1)
                
                # D. Confronto
                np.testing.assert_array_equal(decoded, expected, err_msg=f"[{mode}] DECODING FAILED")
                
                print(f"  ✓ ADC: Mode '{mode.upper()}' Passed")

if __name__ == '__main__':
    unittest.main()