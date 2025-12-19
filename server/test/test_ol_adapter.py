import unittest
import logging
import numpy as np
import sys
import os
from unittest.mock import MagicMock, patch, ANY

# -------------------------------------------------------------------------
# 1. PATH SETUP
# -------------------------------------------------------------------------
# Add project root to sys.path to allow imports from the 'server' package
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# -------------------------------------------------------------------------
# 2. GLOBAL MOCKING
# -------------------------------------------------------------------------
# Mock hardware libraries (pynq, xrfdc) that are unavailable in the test environment
mock_pynq = MagicMock()
sys.modules["pynq"] = mock_pynq
mock_pynq.allocate = MagicMock()
sys.modules["xrfdc"] = MagicMock()
sys.modules["xrfclk"] = MagicMock()

# -------------------------------------------------------------------------
# 3. IMPORTS
# -------------------------------------------------------------------------
try:
    from server.ol_adapter import OL_adapter, WaveEntry, modulation, trigger_command
    from server.exceptions import ConfigurationError, DriverError, HardwareStateError
except ImportError as e:
    print(f"Critical Import Error: {e}")
    raise

# =========================================================================
# BASE CLASS (Common Setup)
# =========================================================================
class BaseTestOL(unittest.TestCase):
    """
    Base test class providing common setup and mocking for all OL_adapter tests.
    """

    def setUp(self):
        # 1. Mock Overlay and Specifications
        self.mock_ol = MagicMock()
        self.mock_ol.is_healthy = True
        self.mock_ol.hw_specs = {
            "summary": {
                "adc_parallelism": 8,
                "dac_sr_hz": 4000000000, 
                "adc_sr_hz": 1000000000,
                "adc_parallelism_set": [8] 
            }
        }
        # Mock mix-mode configuration methods
        self.mock_ol.configure_dac_mix_mode.return_value = {"changed": False, "nyquist_zone": 1}
        self.mock_ol.configure_adc_mix_mode.return_value = {"changed": False, "nyquist_zone": 1}
        # Mock summary method
        self.mock_ol.summary.return_value = {"bitfile": "test.bit"}

        # 2. Mock Generator Driver
        self.mock_gen = MagicMock()
        self.mock_gen.SampleSize = 16
        self.mock_gen.NumberOfChannels = 16
        self.mock_gen.MemoryMappedFifoSegmentDepth = 4096 * 4 
        self.mock_gen.WaveMemoryDict = {} 
        self.mock_gen.EnvelopeMemoryDict = {}
        # Default success return values (0 = Success)
        self.mock_gen.add_envelope_to_envelope_memory.return_value = 0
        self.mock_gen.create_wave_definition_word.return_value = 123456789
        self.mock_gen.add_wave_in_wave_memory.return_value = 0
        self.mock_gen.add_wave_to_drive_wave_sequence.return_value = 0
        self.mock_gen.set_drive_order_source.return_value = 0
        self.mock_gen.replace_wave_in_wave_memory.return_value = 0
        self.mock_gen.reset_wave_memory_dict.return_value = 0
        self.mock_gen.reset_envelope_dict.return_value = 0
        self.mock_gen.set_drive_dds_parameters.return_value = 0
        self.mock_gen.set_readout_dds_parameters.return_value = 0
        self.mock_gen.set_trigger_channel.return_value = 0
        
        self.mock_ol.generators = [self.mock_gen]

        # 3. Mock Acquisition Driver
        self.mock_acq = MagicMock()
        self.mock_acq.set_acquisition_dds_parameters.return_value = 0
        self.mock_acq.set_acquisition_duration.return_value = 0
        self.mock_acq.set_time_of_flight.return_value = 0
        self.mock_acq.set_trigger_channel.return_value = 0
        
        self.mock_ol.acquisitions = [self.mock_acq]

        # 4. Mock Trigger Driver
        self.mock_trig = MagicMock()
        self.mock_trig.MaxHWRepetitions = 65535
        self.mock_trig.ChannelFifoDepth = 1024
        self.mock_trig.DriveDelayMax = 1000
        self.mock_trig.insert_drive_delay.return_value = 0
        self.mock_trig.set_readout_delay.return_value = 0
        self.mock_trig.set_number_of_shots.return_value = 0
        self.mock_trig.set_experiment_duration.return_value = 0
        
        self.mock_ol.trigger = self.mock_trig

        # 5. Infrastructure & Patching
        self.mock_ol.dma = MagicMock()
        self.mock_ol.axis_switch = MagicMock()
        # Patch AcquisitionEngine to prevent real instantiation during tests
        self.patcher_acq = patch('server.ol_adapter.AcquisitionEngine')
        self.MockAcqEngine = self.patcher_acq.start()
        
        # Initialize the Adapter with the mock overlay
        self.adapter = OL_adapter(self.mock_ol, logger=logging.getLogger("TestLogger"))

    def tearDown(self):
        self.patcher_acq.stop()


# =========================================================================
# TEST GROUP 1: BASIC FUNCTIONALITY
# =========================================================================
class TestOLBasic(BaseTestOL):
    
    def test_init_check(self):
        """Verifies initialization raises HardwareStateError if overlay is unhealthy."""
        bad_ol = MagicMock()
        bad_ol.is_healthy = False
        with self.assertRaises(HardwareStateError):
            OL_adapter(bad_ol)

    def test_summary_delegation(self):
        """Tests that summary() correctly delegates to the overlay."""
        expected = {"bitfile": "test.bit"}
        self.mock_ol.summary.return_value = expected
        res = self.adapter.summary()
        self.assertEqual(res, expected)


# =========================================================================
# TEST GROUP 2: GENERATOR IP
# =========================================================================
class TestOLGenerator(BaseTestOL):

    # --- Upload Envelopes ---
    def test_upload_envelopes_valid(self):
        """Tests upload of a standard complex envelope."""
        envelopes = [{
            "name": "TEST_ENV", "for_interpolation": False, "is_symmetric": False,
            "i_even": False, "q_even": False, "samples_iq": [[0.1, 0.1], [0.2, 0.2]]
        }]
        self.mock_gen.NumberOfChannels = 2 
        res = self.adapter.upload_envelopes(gen_index=0, envelopes=envelopes)
        self.assertIn("TEST_ENV", res["loaded"])
        self.mock_gen.add_envelope_to_envelope_memory.assert_called_once()

    def test_upload_envelopes_auto_padding(self):
        """Verifies automatic zero-padding for non-interpolated envelopes."""
        envelopes = [{
            "name": "PAD_ME", "for_interpolation": False, "is_symmetric": False,
            "i_even": False, "q_even": False, "samples_iq": [[0.1, 0.1]] 
        }]
        self.mock_gen.NumberOfChannels = 16 
        self.adapter.upload_envelopes(gen_index=0, envelopes=envelopes)
        args, _ = self.mock_gen.add_envelope_to_envelope_memory.call_args
        self.assertEqual(args[0].shape[0], 16)

    def test_upload_envelopes_skip_existing(self):
        """Tests skipping upload for existing envelopes."""
        envelopes = [{"name": "EXISTING", "for_interpolation": False, "is_symmetric":False, "i_even":False, "q_even":False, "samples_iq":[[0,0]]}]
        self.mock_gen.EnvelopeMemoryDict = {"EXISTING": {}}
        res = self.adapter.upload_envelopes(gen_index=0, envelopes=envelopes)
        self.assertIn("EXISTING", res["skipped"])
        self.mock_gen.add_envelope_to_envelope_memory.assert_not_called()

    # --- Compile Waves ---
    def test_compile_waves_new(self):
        """Tests compilation of a new wave definition."""
        waves = [{"wave_id": "w1", "envelope": "ENV", "duration": 100, "gain": 0.5}]
        self.mock_gen.EnvelopeMemoryDict = {"ENV": {}}
        res = self.adapter.compile_waves(gen_index=0, waves=waves, replace=False)
        self.assertEqual(res["waves"][0]["wave_id"], "w1")
        self.mock_gen.create_wave_definition_word.assert_called()
        self.mock_gen.add_wave_in_wave_memory.assert_called()

    def test_compile_waves_skip_optimization(self):
        """Verifies optimization skips existing waves."""
        cache = self.adapter.get_wave_cache(0)
        cache["w1"] = WaveEntry("ENV", 100, 0.5, wdw=999)
        self.mock_gen.WaveMemoryDict = {"w1": 0x1000}
        
        waves = [{"wave_id": "w1", "envelope": "ENV", "duration": 100, "gain": 0.5}]
        res = self.adapter.compile_waves(gen_index=0, waves=waves, replace=False)
        self.assertIn("w1", res["skipped"])
        self.mock_gen.create_wave_definition_word.assert_not_called()

    def test_compile_waves_replace(self):
        """Tests wave replacement when parameters change."""
        cache = self.adapter.get_wave_cache(0)
        cache["w1"] = WaveEntry("ENV", 100, 0.5, wdw=999)
        self.mock_gen.WaveMemoryDict = {"w1": 0x1000}
        
        waves = [{"wave_id": "w1", "envelope": "ENV", "duration": 200, "gain": 0.5}] # Duration changed
        res = self.adapter.compile_waves(gen_index=0, waves=waves, replace=True)
        self.assertIn("w1", res["replaced"])
        self.mock_gen.replace_wave_in_wave_memory.assert_called()

    # --- FIFO Program ---
    def test_program_drive_sequence(self):
        """Tests programming the generator FIFO sequence."""
        wave_list = ["w1", "w2"]
        cache = self.adapter.get_wave_cache(0)
        cache["w1"] = WaveEntry("E", 10, 1.0, wdw=10)
        cache["w2"] = WaveEntry("E", 10, 1.0, wdw=20)
        self.mock_gen.WaveMemoryDict = {"w1": 0, "w2": 4}

        self.adapter.program_drive_sequence(gen_index=0, wave_id_list=wave_list)
        self.mock_gen.set_drive_order_source.assert_called_with(0) # FIFO
        self.assertEqual(self.mock_gen.add_wave_to_drive_wave_sequence.call_count, 2)

    # --- Resets ---
    def test_reset_wave_memory(self):
        """Tests resetting wave memory."""
        self.adapter.reset_wave_memory(gen_index=0, preserve_specs=False)
        self.mock_gen.reset_wave_memory_dict.assert_called_once()
        
    def test_reset_envelopes(self):
        """Tests resetting envelopes."""
        self.adapter.reset_envelopes(gen_index=0)
        self.mock_gen.reset_envelope_dict.assert_called_once()

    # --- Modulation (G6) ---
    def test_generator_modulation_drive(self):
        """Tests modulation config for 'drive'."""
        mod = modulation(frequency_mhz=150.0, phase=0.0)
        self.adapter.generator_modulation(gen_index=0, label="drive", gen_mod=mod)
        self.mock_ol.configure_dac_mix_mode.assert_called_with(0, "drive", 150.0)
        self.mock_gen.set_drive_dds_parameters.assert_called_once_with(frequency=150.0, dac_samplerate=4000.0)

    def test_generator_modulation_readout(self):
        """Tests modulation config for 'readout'."""
        mod = modulation(frequency_mhz=200.0, phase=1.57)
        self.adapter.generator_modulation(gen_index=0, label="readout", gen_mod=mod)
        self.mock_gen.set_readout_dds_parameters.assert_called_once_with(frequency=200.0, phase=1.57, dac_samplerate=4000.0)

    # --- Trigger Config (G7) ---
    def test_gen_trigger2listen(self):
        """Tests configuration of generator trigger listening."""
        trig_cmd = trigger_command(ttype="drive", channel=1)
        self.adapter.gen_trigger2listen(gen_index=0, trig=trig_cmd)
        self.mock_gen.set_trigger_channel.assert_called_with(
            channel=1, ttype="drive"
        )


# =========================================================================
# TEST GROUP 3: ACQUISITION IP
# =========================================================================
class TestOLAcquisition(BaseTestOL):

    def test_acquisition_parameters(self):
        """Tests acquisition modulation parameters."""
        mod = modulation(frequency_mhz=50.0, phase=3.14)
        self.adapter.acquisition_parameters(acq_index=0, acq_mod=mod)
        
        
    def test_acquisition_timing(self):
        """Tests acquisition timing (duration, tof)."""
        tof_val = 123
        duration_val = 2000
        
        self.adapter.acquisition_timing(acq_index=0, tof=tof_val, duration=duration_val)
        
        self.mock_acq.set_acquisition_duration.assert_called_with(duration_val)
        self.mock_acq.set_time_of_flight.assert_called_with(tof_val)

    def test_acq_trigger2listen(self):
        """Tests configuration of acquisition trigger listening."""
        trig_cmd = trigger_command(ttype="readout", channel=2)
        self.adapter.acq_trigger2listen(acq_index=0, trig=trig_cmd)
        self.mock_acq.set_trigger_channel.assert_called_with(
            channel=2
        )


# =========================================================================
# TEST GROUP 4: TRIGGER IP (TG1)
# =========================================================================
class TestOLTrigger(BaseTestOL):

    def test_tg_set_shots(self):
        """Tests setting shots."""
        self.adapter.tg_set_shots(1000)
        self.mock_trig.set_number_of_shots.assert_called_with(1000)

    def test_tg_set_duration(self):
        """Tests setting experiment duration."""
        self.adapter.tg_set_duration(5000)
        self.mock_trig.set_experiment_duration.assert_called_with(5000)

    def test_tg_program_delays(self):
        """Tests programming trigger delays."""
        drive = {"1": {"delay": [(10, 1)]}}
        readout = {"1": {"delay": 50}}
        self.adapter.tg_program_delays(drive=drive, readout=readout)
        self.mock_trig.set_readout_delay.assert_called_with(50, 1)
        self.mock_trig.insert_drive_delay.assert_called_with(1, 1, 10, 1)

    def test_upload_envelope_reserved_name(self):
        """
        Tests that uploading an envelope with a reserved name (starting with '_')
        is blocked by the adapter and reported as a failure in the response.
        """
        envelopes = [{
            "name": "_RESERVED_NAME", # Invalid name
            "for_interpolation": False, "is_symmetric": False,
            "i_even": False, "q_even": False, "samples_iq": [[0.1, 0.1]]
        }]
        
        # We expect the operation to 'succeed' (no crash) but report the failure in the result dict
        res = self.adapter.upload_envelopes(gen_index=0, envelopes=envelopes)
        
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(res["failed"][0]["name"], "_RESERVED_NAME")
        self.assertIn("forbidden", res["failed"][0]["error"])
        # The low-level driver should NEVER be called for this envelope
        self.mock_gen.add_envelope_to_envelope_memory.assert_not_called()

    def test_upload_mixed_envelopes(self):
        """
        Tests the simultaneous upload of mixed envelope types (interpolated and 
        non-interpolated) to verify that boolean flags are passed correctly 
        for each individual item.
        """
        envelopes = [
            # Envelope 1: Interpolated, Symmetric
            {
                "name": "GAUSS_INTERP", 
                "for_interpolation": True, "is_symmetric": True,
                "i_even": True, "q_even": True, "samples_iq": [[0.1, 0.1], [0.2, 0.2]]
            },
            # Envelope 2: Non-Interpolated (Raw)
            {
                "name": "RECT_RAW", 
                "for_interpolation": False, "is_symmetric": False,
                "i_even": False, "q_even": False, "samples_iq": [[1.0, 1.0], [1.0, 1.0]]
            }
        ]
        
        self.mock_gen.NumberOfChannels = 2 # Match size to avoid padding logic interference
        res = self.adapter.upload_envelopes(gen_index=0, envelopes=envelopes)
        
        self.assertEqual(len(res["loaded"]), 2)
        self.assertEqual(self.mock_gen.add_envelope_to_envelope_memory.call_count, 2)
        
        # Verify call arguments for Env 1 (Interpolated)
        # args structure: (env_data, for_interp, is_sym, i_even, q_even, name)
        call_args_1 = self.mock_gen.add_envelope_to_envelope_memory.call_args_list[0]
        self.assertTrue(call_args_1[0][1], "First envelope should have for_interp=True")
        self.assertTrue(call_args_1[0][2], "First envelope should have is_sym=True")
        self.assertEqual(call_args_1[0][5], "GAUSS_INTERP")

        # Verify call arguments for Env 2 (Raw)
        call_args_2 = self.mock_gen.add_envelope_to_envelope_memory.call_args_list[1]
        self.assertFalse(call_args_2[0][1], "Second envelope should have for_interp=False")
        self.assertFalse(call_args_2[0][2], "Second envelope should have is_sym=False")
        self.assertEqual(call_args_2[0][5], "RECT_RAW")

    def test_upload_envelope_no_padding_error(self):
        """
        Tests that if `auto_pad_noninterp` is False and the envelope size is not 
        aligned with parallelism, the driver's error is caught and returned.
        """
        envelopes = [{
            "name": "WRONG_SIZE",
            "for_interpolation": False, 
            "is_symmetric": False, "i_even": False, "q_even": False, 
            "samples_iq": [[0.1, 0.1]] # Size 1
        }]
        
        self.mock_gen.NumberOfChannels = 16 # Hardware requires multiples of 16
        
        # Simulate the driver returning an error code (e.g. -3 for Invalid Parameters)
        # Note: OL_adapter._call raises DriverError/ConfigurationError for negative codes
        # upload_envelopes catches Exception, so we can simulate it directly.
        self.mock_gen.add_envelope_to_envelope_memory.side_effect = ConfigurationError("Driver rejected size")
        
        # Disable auto-padding
        res = self.adapter.upload_envelopes(gen_index=0, envelopes=envelopes, auto_pad_noninterp=False)
        
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(res["failed"][0]["name"], "WRONG_SIZE")
        self.assertIn("Driver rejected size", str(res["failed"][0]["error"]))
        
        # Ensure driver WAS called (because we disabled the safety check)
        self.mock_gen.add_envelope_to_envelope_memory.assert_called()

    def test_reset_wave_memory_fifo_clearing(self):
        """
        Tests the `reset_wave_memory` method specifically verifying the `clear_last_fifo` logic.
        Ensures that the internal FIFO cache is cleared or preserved as requested.
        """
        gen_idx = 0
        # 1. Pre-populate the FIFO cache
        fake_fifo = ["w1", "w2", "w3"]
        self.adapter._last_fifo[gen_idx] = fake_fifo
        
        # Case A: Reset WITH FIFO clearing
        self.adapter.reset_wave_memory(gen_index=gen_idx, clear_last_fifo=True)
        self.assertNotIn(gen_idx, self.adapter._last_fifo, "FIFO cache should be cleared")
        
        # 2. Restore state
        self.adapter._last_fifo[gen_idx] = fake_fifo
        
        # Case B: Reset WITHOUT FIFO clearing (e.g. only re-uploading WDWs)
        self.adapter.reset_wave_memory(gen_index=gen_idx, clear_last_fifo=False)
        self.assertIn(gen_idx, self.adapter._last_fifo, "FIFO cache should be preserved")
        self.assertEqual(self.adapter._last_fifo[gen_idx], fake_fifo)

    # =========================================================================
    # Envelopes & Reset Logic
    # =========================================================================

    def test_upload_envelope_reserved_name(self):
        """
        Tests that uploading an envelope with a reserved name (starting with '_')
        is blocked by the adapter and reported as a failure.
        """
        envelopes = [{
            "name": "_RESERVED_NAME", # Invalid name
            "for_interpolation": False, "is_symmetric": False,
            "i_even": False, "q_even": False, "samples_iq": [[0.1, 0.1]]
        }]
        
        # We expect the operation to 'succeed' (no crash) but report the failure in the result dict
        res = self.adapter.upload_envelopes(gen_index=0, envelopes=envelopes)
        
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(res["failed"][0]["name"], "_RESERVED_NAME")
        self.assertIn("forbidden", res["failed"][0]["error"])
        # The low-level driver should NEVER be called for this envelope
        self.mock_gen.add_envelope_to_envelope_memory.assert_not_called()

    def test_upload_mixed_envelopes(self):
        """
        Tests the simultaneous upload of mixed envelope types (interpolated and 
        non-interpolated) to verify that boolean flags are passed correctly 
        for each individual item.
        """
        envelopes = [
            # Envelope 1: Interpolated, Symmetric
            {
                "name": "GAUSS_INTERP", 
                "for_interpolation": True, "is_symmetric": True,
                "i_even": True, "q_even": True, "samples_iq": [[0.1, 0.1], [0.2, 0.2]]
            },
            # Envelope 2: Non-Interpolated (Raw)
            {
                "name": "RECT_RAW", 
                "for_interpolation": False, "is_symmetric": False,
                "i_even": False, "q_even": False, "samples_iq": [[1.0, 1.0], [1.0, 1.0]]
            }
        ]
        
        self.mock_gen.NumberOfChannels = 2 # Match size to avoid padding logic interference
        res = self.adapter.upload_envelopes(gen_index=0, envelopes=envelopes)
        
        self.assertEqual(len(res["loaded"]), 2)
        self.assertEqual(self.mock_gen.add_envelope_to_envelope_memory.call_count, 2)
        
        # Verify call arguments for Env 1 (Interpolated)
        # args structure: (env_data, for_interp, is_sym, i_even, q_even, name)
        call_args_1 = self.mock_gen.add_envelope_to_envelope_memory.call_args_list[0]
        self.assertTrue(call_args_1[0][1], "First envelope should have for_interp=True")
        self.assertTrue(call_args_1[0][2], "First envelope should have is_sym=True")
        self.assertEqual(call_args_1[0][5], "GAUSS_INTERP")

        # Verify call arguments for Env 2 (Raw)
        call_args_2 = self.mock_gen.add_envelope_to_envelope_memory.call_args_list[1]
        self.assertFalse(call_args_2[0][1], "Second envelope should have for_interp=False")
        self.assertFalse(call_args_2[0][2], "Second envelope should have is_sym=False")
        self.assertEqual(call_args_2[0][5], "RECT_RAW")

    def test_upload_envelope_no_padding_error(self):
        """
        Tests that if `auto_pad_noninterp` is False and the envelope size is not 
        aligned with parallelism, the driver's error is caught and returned gracefully.
        """
        envelopes = [{
            "name": "WRONG_SIZE",
            "for_interpolation": False, 
            "is_symmetric": False, "i_even": False, "q_even": False, 
            "samples_iq": [[0.1, 0.1]] # Size 1
        }]
        
        self.mock_gen.NumberOfChannels = 16 # Hardware requires multiples of 16
        
        # Simulate the driver returning an error code (e.g. -3 for Invalid Parameters)
        # Note: OL_adapter._call raises DriverError/ConfigurationError for negative codes.
        # upload_envelopes catches Exception, so we can simulate it directly.
        self.mock_gen.add_envelope_to_envelope_memory.side_effect = ConfigurationError("Driver rejected size")
        
        # Disable auto-padding
        res = self.adapter.upload_envelopes(gen_index=0, envelopes=envelopes, auto_pad_noninterp=False)
        
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(res["failed"][0]["name"], "WRONG_SIZE")
        self.assertIn("Driver rejected size", str(res["failed"][0]["error"]))
        
        # Ensure driver WAS called (because we disabled the safety check)
        self.mock_gen.add_envelope_to_envelope_memory.assert_called()

    def test_reset_wave_memory_fifo_clearing(self):
        """
        Tests the `reset_wave_memory` method specifically verifying the `clear_last_fifo` logic.
        Ensures that the internal FIFO cache is cleared or preserved as requested.
        """
        gen_idx = 0
        # 1. Pre-populate the FIFO cache
        fake_fifo = ["w1", "w2", "w3"]
        self.adapter._last_fifo[gen_idx] = fake_fifo
        
        # Case A: Reset WITH FIFO clearing
        self.adapter.reset_wave_memory(gen_index=gen_idx, clear_last_fifo=True)
        self.assertNotIn(gen_idx, self.adapter._last_fifo, "FIFO cache should be cleared")
        
        # 2. Restore state
        self.adapter._last_fifo[gen_idx] = fake_fifo
        
        # Case B: Reset WITHOUT FIFO clearing (e.g. only re-uploading WDWs)
        self.adapter.reset_wave_memory(gen_index=gen_idx, clear_last_fifo=False)
        self.assertIn(gen_idx, self.adapter._last_fifo, "FIFO cache should be preserved")
        self.assertEqual(self.adapter._last_fifo[gen_idx], fake_fifo)

if __name__ == '__main__':
    unittest.main()