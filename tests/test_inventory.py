import sys
import unittest
from unittest.mock import MagicMock

# --- FIX: MOCK PYNQ PREVENTIVO ---
# Inventory potrebbe importare overlay_driver che importa pynq
try:
    import pynq
except ImportError:
    sys.modules['pynq'] = MagicMock()
# ---------------------------------

from backend.inventory import HardwareInventory

class TestHardwareInventory(unittest.TestCase):
    """
    Testiamo la scoperta dell'hardware e la validazione dei clock.
    """

    def setUp(self):
        # 1. Creiamo un Overlay Finto (il bitstream simulato)
        self.mock_overlay = MagicMock()
        
        # Trucco: Facciamo credere a Inventory che questo sia un vero FIREQ_SoC
        # Inventory controlla type(overlay).__name__, quindi lo inganniamo così:
        self.mock_overlay.__class__.__name__ = 'FIREQ_SoC'
        
        # 2. Popoliamo le liste di IP (Generatori, Acquisizioni, Trigger)
        #    Inventory si aspetta delle liste piene.
        self.mock_overlay._generation_ips = [MagicMock()]
        self.mock_overlay._readout_ips = [MagicMock()]
        self.mock_overlay._trigger_ips = [MagicMock()]
        
        # 3. Popoliamo l'infrastruttura (DMA, RF-DC)
        self.mock_overlay.axi_dma_0 = MagicMock()
        self.mock_overlay.usp_rf_data_converter_0 = MagicMock()
        
        # 4. CONFIGURAZIONE DEI CLOCK (La parte difficile)
        #    Dobbiamo simulare i "Tile" del convertitore RF.
        
        # Creiamo un Tile DAC finto che sembra funzionare bene
        mock_dac_tile = MagicMock()
        mock_dac_tile.PLLLockStatus = 2 # 2 significa "LOCKED" (Agganciato)
        mock_dac_tile.PLLConfig = {'SampleRate': 6.0} # 6.0 GSPS
        mock_dac_tile.blocks = [MagicMock(), MagicMock()] # Simuliamo 2 blocchi per tile
        
        # Creiamo un Tile ADC finto
        mock_adc_tile = MagicMock()
        mock_adc_tile.PLLLockStatus = 2
        mock_adc_tile.PLLConfig = {'SampleRate': 3.0} # 3.0 GSPS
        mock_adc_tile.blocks = [MagicMock(), MagicMock()]

        # Attacchiamo questi tile al controllore RF-DC
        self.mock_overlay.usp_rf_data_converter_0.dac_tiles = [mock_dac_tile]
        self.mock_overlay.usp_rf_data_converter_0.adc_tiles = [mock_adc_tile]

    def test_successful_discovery(self):
        """
        Test: Se l'hardware è perfetto, Inventory deve inizializzarsi e calcolare le spec.
        """
        hw = HardwareInventory(self.mock_overlay)
        
        # Verifiche
        self.assertIsNotNone(hw.gens) # Ha trovato i generatori?
        self.assertIsNotNone(hw.dma)  # Ha trovato il DMA?
        
        # Verifica che abbia letto i Sample Rate giusti
        # Nota: inventory moltiplica per 1e9, quindi 6.0 diventa 6e9
        self.assertEqual(hw.specs['dac_sr'], 6.0e9)
        self.assertEqual(hw.specs['adc_sr'], 3.0e9)

    def test_missing_critical_ip(self):
        """
        Test: Se mancano i Generatori nel bitstream, deve esplodere.
        """
        # Svuotiamo la lista dei generatori
        self.mock_overlay._generation_ips = [] 
        
        # Ci aspettiamo RuntimeError
        with self.assertRaisesRegex(RuntimeError, "No Signal Generators found"):
            HardwareInventory(self.mock_overlay)

    def test_clock_mismatch(self):
        """
        Test: Se due DAC hanno frequenze diverse, deve esplodere.
        Un sistema scientifico non può avere clock asincroni!
        """
        # Tile 1: 6 GSPS
        tile1 = MagicMock()
        tile1.PLLConfig = {'SampleRate': 6.0}
        
        # Tile 2: 5 GSPS (Errore!)
        tile2 = MagicMock()
        tile2.PLLConfig = {'SampleRate': 5.0} 
        
        # Li mettiamo entrambi nella lista
        self.mock_overlay.usp_rf_data_converter_0.dac_tiles = [tile1, tile2]
        
        # Ci aspettiamo RuntimeError che parli di "Mismatch"
        with self.assertRaisesRegex(RuntimeError, "DAC Clock Mismatch"):
            HardwareInventory(self.mock_overlay)

if __name__ == '__main__':
    unittest.main()