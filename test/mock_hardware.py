# file: fireq-utils/test/mock_hardware.py
import numpy as np
from unittest.mock import MagicMock
from typing import Dict, Any

# =============================================================================
# MOCK PYNQ BUFFER
# =============================================================================
class MockPynqBuffer(np.ndarray):
    """
    Simulates a PYNQ Contiguous Memory Buffer.
    
    Inherits from numpy.ndarray to mimic buffer behavior while adding
    virtual physical address attributes required by DMA drivers.
    """
    def __new__(cls, shape, dtype=np.uint32):
        obj = super().__new__(cls, shape, dtype=dtype)
        obj.physical_address = 0x10000000
        obj.device_address = 0x10000000
        return obj

    def __array_finalize__(self, obj):
        if obj is None: return
        self.physical_address = getattr(obj, 'physical_address', 0x10000000)

    def freebuffer(self): pass
    def invalidate(self): pass
    def flush(self): pass

# =============================================================================
# MOCK LOW-LEVEL DRIVERS
# =============================================================================
class MockGeneratorDriver:
    """
    Simulates the low-level Generator IP driver.
    
    Maintains internal state for Envelope and Wave memory to support 
    verification of high-level adapter logic (caching, replacement, etc.).
    """
    def __init__(self, idx: int):
        self.idx = idx
        self.envelope_memory_dict: Dict[str, Any] = {}
        self.wave_memory_dict: Dict[str, Any] = {}
        self.memory_mapped_fifo_segment_depth = 4096 * 4
        self.sample_size = 16
        self.number_of_channels = 16
        self.max_waveform_duration = 65536

    def add_envelope_to_envelope_memory(self, samples, for_interp, is_sym, i_even, q_even, name):
        self.envelope_memory_dict[name] = {"size": len(samples), "type": "interp" if for_interp else "std"}
        return 0

    def create_wave_definition_word(self, env_name, *args):
        """
        Simulates WDW compilation.
        
        Returns:
            int: A dummy WDW (123456) on success.
            int: -3 if the envelope does not exist (Driver Error).
        """
        # Strict simulation: fail if envelope is missing
        if env_name not in self.envelope_memory_dict:
            return -3 # Standard driver error code
        
        # Gain validation (example argument check)
        gain = args[2] if len(args) > 2 else 0
        if abs(gain) > 1.0:
            return -3
        return 123456

    def create_vz_gate_definition_word(self, phase_rad): return 99999 
    def write_readout_wave(self, wdw): return 0
    def add_wave_in_wave_memory(self, wdw, wave_id):
        self.wave_memory_dict[str(wave_id)] = wdw
        return 0
    def replace_wave_in_wave_memory(self, wdw, wave_id, new_id):
        self.wave_memory_dict[str(new_id)] = wdw
        return 0
    def reset_wave_memory_dict(self):
        self.wave_memory_dict.clear()
        return 0
    def reset_envelope_dict(self):
        self.envelope_memory_dict.clear()
        return 0
    def set_drive_order_source(self, src): return 0
    def add_wave_to_drive_wave_sequence(self, idx, wave_id): return 0
    def set_drive_dds_parameters(self, frequency, dac_samplerate): return 0
    def set_readout_dds_parameters(self, frequency, phase, dac_samplerate): return 0
    def set_lfsr_seed(self, seed): return 0
    def set_trigger_channel(self, channel, ttype): return 0

class MockAcquisitionDriver:
    """Simulates the low-level Acquisition IP driver."""
    def __init__(self, idx: int):
        self.idx = idx
        self.ctrl = 0
        self.axi_lite_interface_mmio = MagicMock()

    def set_acquisition_dds_parameters(self, frequency, phase, adc_samplerate): return 0
    def set_acquisition_duration(self, dur): return 0
    def set_time_of_flight(self, tof): return 0
    def set_decimated_output_type(self, mode): return 0
    def set_trigger_channel(self, channel): return 0

class MockTriggerDriver:
    """Simulates the Trigger Generator driver."""
    def __init__(self):
        self.max_hw_repetitions = 65535
        self.channel_fifo_depth = 1024
        self.drive_delay_max = 65535

    def set_number_of_shots(self, shots): return 0
    def set_experiment_duration(self, dur): return 0
    def set_readout_delay(self, delay, ch): return 0
    def insert_drive_delay(self, ch, idx, delay, gen): return 0
    def start_experiment(self): pass

class MockDMA:
    """Simulates the AXI DMA Engine behavior."""
    def __init__(self):
        self.recvchannel = MagicMock()
        self.recvchannel.running = True
        self.recvchannel.idle = False
        def side_effect_transfer(buffer):
            if isinstance(buffer, np.ndarray): buffer[:] = 0x00010001 
        self.recvchannel.transfer.side_effect = side_effect_transfer
        self.mmio = MagicMock()

class MockOverlay:
    """
    Simulates the entire PYNQ Overlay structure.
    
    Aggregates Generators, Acquisitions, Trigger, and DMA drivers.
    """
    def __init__(self):
        self.is_healthy = True
        self.hw_specs = {
            "summary": {
                "dac_sr_hz": 4e9, "adc_sr_hz": 4e9, "adc_parallelism": 8,
                "generation_ips": 2, 
                "acquisition_ips": 2,
                "num_generators": 2,    
                "num_acquisitions": 2   
                # -------------------------------------------------------------
            },
            "generators": [{"id": 0}, {"id": 1}],
            "acquisitions": [
                {
                    "id": 0, "parallelism": 8, "max_duration_cycles": 65536,
                    "decimated_fifo_depth_words": 16384, "dec_output_width_bits": 32,
                    "raw_fifo_depth_words": 16384, "raw_output_width_bits": 256
                },
                {
                    "id": 1, "parallelism": 8, "max_duration_cycles": 65536,
                    "decimated_fifo_depth_words": 16384, "dec_output_width_bits": 32,
                    "raw_fifo_depth_words": 16384, "raw_output_width_bits": 256
                }
            ],
            "trigger": {} 
        }
        self.dma = MockDMA()
        self.axis_switch = MagicMock()
        self.trigger = MockTriggerDriver()
        self.generators = [MockGeneratorDriver(0), MockGeneratorDriver(1)]
        self.acquisitions = [MockAcquisitionDriver(0), MockAcquisitionDriver(1)]

    def summary(self): return self.hw_specs["summary"]
    def configure_dac_mix_mode(self, *args, **kwargs): return {"changed": False}
    def configure_adc_mix_mode(self, *args, **kwargs): return {"changed": False}

    # Properties used for direct access via adapter
    @property
    def num_generators(self):
        return len(self.generators)

    @property
    def num_acquisitions(self):
        return len(self.acquisitions)