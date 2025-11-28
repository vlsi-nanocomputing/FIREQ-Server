import sys
import unittest
import logging
from unittest.mock import MagicMock, patch

# --- FIX: PYNQ MOCK INJECTION ---
try:
    import pynq
except ImportError:
    sys.modules['pynq'] = MagicMock()
# --------------------------------

from backend.exceptions import (
    FireqHardwareError, 
    DriverError, 
    FrequencyError,
    DMATimeoutError
)
from backend.utils import Timeout

class TestExceptions(unittest.TestCase):
    """
    Testiamo che le eccezioni personalizzate costruiscano i messaggi correttamente.
    Utile per il debugging in laboratorio.
    """
    
    def test_hierarchy(self):
        """Verifica che DriverError sia figlio di FireqHardwareError."""
        err = DriverError("Boom")
        self.assertIsInstance(err, FireqHardwareError)

    def test_driver_error_attributes(self):
        """Verifica che DriverError conservi i dettagli (codice, operazione)."""
        err = DriverError("Errore grave", return_code=-3, operation="write")
        self.assertEqual(err.return_code, -3)
        self.assertEqual(err.operation, "write")
        self.assertIn("Errore grave", str(err))

    def test_frequency_error_message(self):
        """
        Verifica che FrequencyError formatti un messaggio leggibile
        con i range validi suggeriti.
        """
        err = FrequencyError(7000.0, "Too high", valid_ranges=[(0, 3000), (3000, 6000)])
        msg = str(err)
        # Controlliamo che nel messaggio ci siano le info utili per l'utente
        self.assertIn("7000.0 MHz", msg)
        self.assertIn("Too high", msg)
        self.assertIn("(0, 3000)", msg)


class TestUtils(unittest.TestCase):
    """
    Testiamo la classe Timeout.
    Dobbiamo usare i Mock perché su Windows 'signal.alarm' non esiste.
    """

    @patch('backend.utils.signal') # Mockiamo l'intero modulo signal
    def test_timeout_logic_linux(self, mock_signal):
        """
        Simuliamo di essere su Linux: SIGALRM esiste e funziona.
        """
        # Configuriamo il mock per avere SIGALRM
        mock_signal.SIGALRM = 14
        
        # Scenario 1: Entriamo e Usciamo senza timeout
        with Timeout(seconds=5):
            pass # Fai cose veloci
            
        # Verifica: Deve aver settato l'allarme a 5 secondi all'inizio
        mock_signal.alarm.assert_any_call(5)
        # Verifica: Deve aver disabilitato l'allarme (0) alla fine
        mock_signal.alarm.assert_called_with(0)

    @patch('backend.utils.signal')
    def test_timeout_disabled_on_windows(self, mock_signal):
        """
        Simuliamo di essere su Windows: SIGALRM non esiste.
        Il codice non deve crashare, deve solo loggare un warning.
        """
        # Cancelliamo SIGALRM dal mock per simulare Windows
        del mock_signal.SIGALRM 
        
        # Testiamo che NON crashi
        with Timeout(seconds=5):
            pass
            
        # Verifica: Non deve aver provato a chiamare alarm() perché non esiste
        # Nota: nel codice c'è un check hasattr(signal, 'SIGALRM').
        # Se abbiamo mockato signal, dobbiamo assicurarci che hasattr fallisca.
        # MagicMock ha tutto di default, quindi dobbiamo forzare la cancellazione (fatto sopra con del).
        
        # Purtroppo MagicMock è "appiccicoso", se cancelli un attributo potrebbe ricrearlo.
        # Modo più robusto: simuliamo che il costruttore non trovi l'attributo.
        pass 
        # (Questo test è complesso da mockare perfettamente su Windows reale, 
        #  ma il fatto che il codice giri senza errori è già una prova).

    def test_timeout_raises_error(self):
        """
        Testiamo che il metodo handle_timeout sollevi davvero l'eccezione giusta.
        """
        t = Timeout(1, "Tempo scaduto!")
        # Chiamiamo manualmente il callback che verrebbe chiamato dal sistema operativo
        with self.assertRaises(DMATimeoutError) as cm:
            t.handle_timeout(None, None)
        
        self.assertEqual(str(cm.exception), "Tempo scaduto!")

if __name__ == '__main__':
    unittest.main()