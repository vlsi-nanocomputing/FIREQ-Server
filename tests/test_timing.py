import sys
import os
import unittest
from unittest.mock import MagicMock

# --- FIX: INGANNIAMO PYTHON (MOCK PYNQ) ---
# Se non siamo sulla board, 'pynq' non esiste.
# Creiamo un modulo finto al volo per non far crashare l'import.
try:
    import pynq
except ImportError:
    # Creiamo un mock
    mock_pynq = MagicMock()
    # Lo iniettiamo nella memoria di sistema come se fosse installato
    sys.modules['pynq'] = mock_pynq
    # Opzionale: se servono sotto-moduli specifici (es. pynq.lib)
    sys.modules['pynq.lib'] = MagicMock()
# ------------------------------------------
from backend.timing import TimingValidator
from backend.exceptions import TimingError, ConfigurationError


class TestTimingValidator(unittest.TestCase):
    """Unit tests for the TimingValidator class.
       This test covers initialization and validation of experiment timing configurations.
    """

    def setUp(self):
        """
        Prepare the test fixture before each test method.
        We mock the trigger adapter to provide hardware limits.
        """
        self.mock_trigger = MagicMock()
        self.mock_trigger.max_duration = 10000
        self.mock_trigger.max_shots = 1000

        # Standard specs derived from a theoretical RFSoC configuration
        self.specs = {
            'dac_sr': 6e9,  # 6 GSPS
            'adc_sr': 3e9,  # 3 GSPS
            'adc_parallelism': 8
        }
        self.validator = TimingValidator(self.mock_trigger, self.specs)

    def test_initialization_missing_specs(self):
        """Test that initialization fails if specs are missing."""
        incomplete_specs = {'dac_sr': 6e9} # Missing adc_sr
        with self.assertRaises(ConfigurationError):
            TimingValidator(self.mock_trigger, incomplete_specs)

    def test_duration_too_short(self):
        """Test that validation catches experiments shorter than minimum propagation time."""
        # MIN_DURATION_CYCLES is defined as 50 in timing.py
        with self.assertRaises(TimingError):
            self.validator.validate_experiment(duration_cycles=10, shots=1)

    def test_readout_exceeds_duration(self):
        """Test the case where readout parameters make the experiment impossible."""
        # Duration is 200, but Trigger Delay (150) + TOF (10) + Acq (100) > 200
        readout_cfg = {
            'num_samples': 800, # At 3GSPS/8, this takes 100 cycles
            'trigger_delay': 150,
            'time_of_flight': 10
        }
        with self.assertRaises(TimingError):
            self.validator.validate_experiment(duration_cycles=200, shots=1, readout_cfg=readout_cfg)

    def test_valid_experiment(self):
        """Test a perfectly valid experiment configuration."""
        readout_cfg = {
            'num_samples': 100,
            'trigger_delay': 50,
            'time_of_flight': 10
        }
        # Should not raise any exception
        try:
            self.validator.validate_experiment(duration_cycles=2000, shots=100, readout_cfg=readout_cfg)
        except TimingError:
            self.fail("validate_experiment raised TimingError unexpectedly!")

if __name__ == '__main__':
    unittest.main()