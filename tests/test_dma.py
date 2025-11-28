import sys
import unittest
import numpy as np
from unittest.mock import MagicMock, patch

# --- FIX: PYNQ MOCK INJECTION ---
try:
    import pynq
except ImportError:
    mock_pynq = MagicMock()
    sys.modules['pynq'] = mock_pynq
# --------------------------------

from backend.dma_engine import AcquisitionEngine
from backend.exceptions import DMAError, DMATimeoutError

class TestAcquisitionEngine(unittest.TestCase):
    """
    Unit tests for the Data Acquisition Engine (DMA).
    """

    def setUp(self):
        """Reset delle condizioni prima di OGNI singolo test."""
        self.mock_dma = MagicMock()
        self.mock_dma.recvchannel.idle = True
        self.mock_switch = MagicMock()
        self.mock_logger = MagicMock()
        self.engine = AcquisitionEngine(self.mock_dma, self.mock_switch, self.mock_logger)

    @patch('backend.dma_engine.allocate')
    def test_arm_acquisition_routing(self, mock_allocate):
        """
        Test 1: Verifica Routing dello Switch.
        """
        mock_buffer = MagicMock()
        mock_allocate.return_value = mock_buffer
        self.mock_dma.mmio.read.return_value = 1 
        
        self.engine.arm_acquisition(1024, 'decimated', adc_index=0)
        
        # Verifica offset hardware fisso a 1
        self.mock_switch.mmio.write.assert_any_call(0x40, 1)

    def test_retrieve_timeout(self):
        """
        Test 2: ZOMBIE KILLER.
        Deve gestire il caso in cui il DMA si blocca.
        """
        mock_buffer = MagicMock()
        
        # 1. Configuriamo il MOCK per ESPLODERE (Timeout)
        self.mock_dma.recvchannel.idle = False 
        self.mock_dma.mmio.read.return_value = 0x11 # Error status
        self.mock_dma.recvchannel.wait.side_effect = DMATimeoutError("Timeout!")
        
        # 2. Verifichiamo che l'eccezione venga lanciata
        with self.assertRaises(DMATimeoutError):
            self.engine.retrieve_acquisition(mock_buffer, 'decimated', timeout=1)
        
        # 3. Verifichiamo che abbia provato a resettare l'hardware (Reset Mask 4)
        self.mock_dma.mmio.write.assert_any_call(0x30, 4)

    def test_happy_path_parsing(self):
        """
        Test 3: HAPPY PATH (Parsing Dati).
        Deve convertire correttamente i bit grezzi in numeri complessi.
        """
        # 1. Configuriamo il MOCK per FUNZIONARE
        # Dati: (1 + 2j) e (-1 - 1j)
        input_data = np.array([0x00010002, 0xFFFFFFFF], dtype=np.uint32)
        
        # Creiamo un finto buffer che si comporti come un array numpy
        class FakeBuffer(np.ndarray):
            def invalidate(self): pass
            def freebuffer(self): pass

        buffer_reale = input_data.view(FakeBuffer)
        
        # Il DMA è felice (Idle = True, Status = OK)
        self.mock_dma.recvchannel.idle = True
        self.mock_dma.mmio.read.return_value = 0x0000
        # IMPORTANTE: Rimuoviamo l'effetto esplosivo del test precedente (anche se setUp lo fa già)
        self.mock_dma.recvchannel.wait.side_effect = None
        
        # 2. Eseguiamo (non ci aspettiamo errori qui!)
        result = self.engine.retrieve_acquisition(buffer_reale, mode='decimated', timeout=1)
        
        # 3. Verifichiamo la matematica
        # 0x00010002 -> 1 + 2j
        self.assertEqual(result[0], 1 + 2j)
        # 0xFFFFFFFF -> -1 - 1j
        self.assertEqual(result[1], -1 - 1j)

if __name__ == '__main__':
    unittest.main()