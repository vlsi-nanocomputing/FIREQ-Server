import sys
import os
import unittest
import numpy as np
from unittest.mock import MagicMock, call

# --- FIX: INIEZIONE MOCK PYNQ ---
# Questo serve SEMPRE, perché importando 'backend' si tira dietro tutto
try:
    import pynq
except ImportError:
    sys.modules['pynq'] = MagicMock()
# --------------------------------

# Importiamo i moduli da testare
from backend.driver_wrappers import GeneratorAdapter
from backend.exceptions import ConfigurationError, DriverError

class TestGeneratorAdapter(unittest.TestCase):
    """
    Testiamo che l'Adapter protegga l'hardware e traduca gli errori.
    """

    def setUp(self):
        # 1. Mock del driver C (il "pupazzo")
        self.mock_drv = MagicMock()
        
        # 2. Attributi hardware finti (obbligatori per l'init)
        self.mock_drv.MaximumDuration = 65536
        self.mock_drv.NumberOfChannels = 1
        self.mock_drv.TriggerChannels = 4
        self.mock_drv.SampleSize = 2
        self.mock_drv.MemoryMappedFifoSegmentDepth = 4096

        # 3. Specifiche Hardware (Simuliamo RFSoC Gen 3)
        self.specs = {
            'dac_sr': 6000.0e6,      # 6 GSPS (6000 MHz)
            'dac_nyquist': 3000.0e6, # Nyquist a 3000 MHz
            'dac_max_nyquist_zone': 2 # Max Zona 2
        }
        
        # 4. Mock del blocco RF-DC (per testare il cambio zona)
        self.mock_rf_block = MagicMock()
        self.mock_rf_block.NyquistZone = 1 # Parte in Zona 1

        # 5. Creiamo l'oggetto da testare
        self.gen = GeneratorAdapter(self.mock_drv, self.specs, rf_block=self.mock_rf_block)

    def test_frequency_too_high(self):
        """
        Test: Se chiedo 7 GHz (Zona 3) su un DAC da 6 GSPS, deve esplodere.
        """
        # Nyquist = 3 GHz. Zona 1 = 0-3, Zona 2 = 3-6.
        # 7 GHz è oltre il limite (Zona 3).
        with self.assertRaises(ConfigurationError) as cm:
            self.gen.set_drive_frequency(7000.0) # 7000 MHz
        
        # Verifichiamo che il messaggio d'errore parli di "HARDWARE LIMIT"
        self.assertIn("HARDWARE LIMIT", str(cm.exception))

    def test_nyquist_zone_switching(self):
        """
        Test: Se cambio frequenza passando da Zona 1 a Zona 2, l'hardware deve aggiornarsi.
        """
        # 1. Impostiamo frequenza in Zona 2 (es. 4 GHz)
        #    4000 MHz > 3000 MHz (Nyquist), quindi è Zona 2.
        self.gen.set_drive_frequency(4000.0)
        
        # Verifica: L'attributo NyquistZone del blocco RF deve essere diventato 2?
        # Nella logica: Zone 2 (Pari) -> Mixing Mode (2 in notazione AMD)
        self.assertEqual(self.mock_rf_block.NyquistZone, 2)
        
        # 2. Torniamo in Zona 1 (es. 1 GHz)
        self.gen.set_drive_frequency(1000.0)
        # Verifica: Deve tornare a 1 (Normal Mode)
        self.assertEqual(self.mock_rf_block.NyquistZone, 1)

    def test_driver_error_handling(self):
        """
        Test: Se il driver C ritorna -3 (errore generico), Python deve lanciare DriverError.
        """
        # Istruiamo il pupazzo a fallire
        self.mock_drv.create_wave_definition_word.return_value = -3
        
        with self.assertRaises(DriverError) as cm:
            self.gen.create_waveform("onda_pazza", 100, 0.5)
        
        # Verifichiamo che il codice d'errore sia stato catturato
        self.assertEqual(cm.exception.return_code, -3)

    def test_upload_envelope_complex_cast(self):
        """
        Test: Se passo numeri Reali (float), lui deve convertirli in Complessi per l'FPGA.
        """
        # Array di float (non complessi)
        samples = np.array([1.0, 0.5, -0.5]) 
        
        self.mock_drv.add_envelope_to_envelope_memory.return_value = 0 # Successo
        
        self.gen.upload_envelope("test_wave", samples)
        
        # Recuperiamo con cosa è stata chiamata la funzione mockata
        args, _ = self.mock_drv.add_envelope_to_envelope_memory.call_args
        dati_passati = args[0]
        
        # Verifica: I dati passati al driver sono di tipo complesso?
        self.assertTrue(np.iscomplexobj(dati_passati))

if __name__ == '__main__':
    unittest.main()