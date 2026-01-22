"""Mock hardware drivers and helpers for tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import numpy as np


# =============================================================================
# MOCK PYNQ BUFFER
# =============================================================================
class MockPynqBuffer(np.ndarray):
    """Simulates a PYNQ Contiguous Memory Buffer.

    Inherits from numpy.ndarray to mimic buffer behavior while adding virtual physical
    address attributes required by DMA drivers.
    """

    def __new__(cls, shape: object, dtype: object = np.uint32) -> MockPynqBuffer:
        """Create a mock contiguous buffer."""
        obj = super().__new__(cls, shape, dtype=dtype)
        obj.physical_address = 0x10000000
        obj.device_address = 0x10000000
        return obj

    def __array_finalize__(self, obj: object | None) -> None:
        """Finalize numpy subclass creation."""
        if obj is None:
            return
        self.physical_address = getattr(obj, "physical_address", 0x10000000)

    def freebuffer(self) -> None:
        """No-op freebuffer for mock."""
        pass

    def invalidate(self) -> None:
        """No-op invalidate for mock."""
        pass

    def flush(self) -> None:
        """No-op flush for mock."""
        pass


# =============================================================================
# MOCK LOW-LEVEL DRIVERS
# =============================================================================
class MockGeneratorDriver:
    """Simulates the low-level Generator IP driver.

    Maintains internal state for Envelope and Wave memory to support verification of
    high-level adapter logic (caching, replacement, etc.).
    """

    def __init__(self, idx: int) -> None:
        """Initialize a mock generator driver."""
        self.idx = idx
        self.envelope_memory_dict: dict[str, Any] = {}
        self.wave_memory_dict: dict[str, Any] = {}
        self.memory_mapped_fifo_segment_depth = 4096 * 4
        self.sample_size = 16
        self.number_of_channels = 16
        self.max_waveform_duration = 65536
        # Track trigger channel settings for verification
        self.trigger_channel_calls: list[dict[str, Any]] = []
        self.current_drive_channel: int = 0
        self.current_readout_channel: int = 0

    def add_envelope_to_envelope_memory(
        self,
        samples: list[list[float]],
        for_interp: bool,
        is_sym: bool,
        i_even: bool,
        q_even: bool,
        name: str,
    ) -> int:
        """Add an envelope definition to the mock memory."""
        self.envelope_memory_dict[name] = {
            "size": len(samples),
            "type": "interp" if for_interp else "std",
        }
        return 0

    def create_wave_definition_word(self, env_name: str, *args: object) -> int:
        """Simulates WDW compilation.

        Returns:
            int: A dummy WDW (123456) on success.
            int: -3 if the envelope does not exist (Driver Error).
        """
        # Strict simulation: fail if envelope is missing
        if env_name not in self.envelope_memory_dict:
            return -3  # Standard driver error code

        # Gain validation (example argument check)
        gain = args[2] if len(args) > 2 else 0
        if abs(gain) > 1.0:
            return -3
        return 123456

    def create_vz_gate_definition_word(self, phase_rad: float) -> int:
        """Return a dummy VZ gate word."""
        return 99999

    def write_readout_wave(self, wdw: int) -> int:
        """Mock write of a readout wave."""
        return 0

    def add_wave_in_wave_memory(self, wdw: int, wave_id: str) -> int:
        """Mock adding a wave to memory."""
        self.wave_memory_dict[str(wave_id)] = wdw
        return 0

    def replace_wave_in_wave_memory(self, wdw: int, wave_id: str, new_id: str) -> int:
        """Mock replacing a wave in memory."""
        self.wave_memory_dict[str(new_id)] = wdw
        return 0

    def reset_wave_memory_dict(self) -> int:
        """Mock clearing wave memory."""
        self.wave_memory_dict.clear()
        return 0

    def reset_envelope_dict(self) -> int:
        """Mock clearing envelope memory."""
        self.envelope_memory_dict.clear()
        return 0

    def set_drive_order_source(self, src: int) -> int:
        """Mock setting drive order source."""
        return 0

    def add_wave_to_drive_wave_sequence(self, idx: int, wave_id: str) -> int:
        """Mock appending to the drive wave sequence."""
        return 0

    def set_drive_dds_parameters(self, frequency: float, dac_samplerate: float) -> int:
        """Mock setting drive DDS parameters."""
        return 0

    def set_readout_dds_parameters(self, frequency: float, phase: float, dac_samplerate: float) -> int:
        """Mock setting readout DDS parameters."""
        return 0

    def set_lfsr_seed(self, seed: int) -> int:
        """Mock setting LFSR seed."""
        return 0

    def set_trigger_channel(self, channel: int, ttype: str) -> int:
        """Mock setting trigger channel."""
        self.trigger_channel_calls.append({"channel": channel, "ttype": ttype})
        if ttype == "drive":
            self.current_drive_channel = channel
        elif ttype == "readout":
            self.current_readout_channel = channel
        return 0


class MockAcquisitionDriver:
    """Simulates the low-level Acquisition IP driver."""

    def __init__(self, idx: int) -> None:
        """Initialize a mock acquisition driver."""
        self.idx = idx
        self.ctrl = 0
        self.AxiLiteInterfaceMMIO = MagicMock()
        # Track trigger channel settings for verification
        self.trigger_channel_calls: list[dict[str, Any]] = []
        self.current_channel: int = 0

    def set_acquisition_dds_parameters(self, frequency: float, phase: float, adc_samplerate: float) -> int:
        """Mock setting acquisition DDS parameters."""
        return 0

    def set_acquisition_duration(self, dur: int) -> int:
        """Mock setting acquisition duration."""
        return 0

    def set_time_of_flight(self, tof: int) -> int:
        """Mock setting time of flight."""
        return 0

    def set_decimated_output_type(self, mode: str) -> int:
        """Mock selecting output mode."""
        return 0

    def set_trigger_channel(self, channel: int) -> int:
        """Mock setting trigger channel."""
        self.trigger_channel_calls.append({"channel": channel})
        self.current_channel = channel
        return 0

    def get_trigger_channel(self) -> int:
        """Mock getting trigger channel."""
        return self.current_channel


class MockTriggerDriver:
    """Simulates the Trigger Generator driver."""

    def __init__(self) -> None:
        """Initialize a mock trigger driver."""
        self.max_hw_repetitions = 65535
        self.channel_fifo_depth = 1024
        self.drive_delay_max = 65535

    def set_number_of_shots(self, shots: int) -> int:
        """Mock setting number of shots."""
        return 0

    def set_experiment_duration(self, dur: int) -> int:
        """Mock setting experiment duration."""
        return 0

    def set_readout_delay(self, delay: int, ch: int) -> int:
        """Mock setting readout delay."""
        return 0

    def insert_drive_delay(self, ch: int, idx: int, delay: int, gen: int) -> int:
        """Mock inserting drive delay."""
        return 0

    def start_experiment(self) -> None:
        """Mock start experiment."""
        pass


class MockDMA:
    """Simulates the AXI DMA Engine behavior."""

    def __init__(self) -> None:
        """Initialize a mock DMA."""
        self.recvchannel = MagicMock()
        self.recvchannel.running = True
        self.recvchannel.idle = False

        def side_effect_transfer(buffer: object) -> None:
            if isinstance(buffer, np.ndarray):
                buffer[:] = 0x00010001

        self.recvchannel.transfer.side_effect = side_effect_transfer
        self.mmio = MagicMock()


class MockOverlay:
    """Simulates the entire PYNQ Overlay structure.

    Aggregates Generators, Acquisitions, Trigger, and DMA drivers.
    """

    def __init__(self) -> None:
        """Initialize a mock overlay."""
        self.is_healthy = True
        self.hw_specs = {
            "summary": {
                "dac_sr_hz": 4e9,
                "adc_sr_hz": 4e9,
                "adc_parallelism": 8,
                "generation_ips": 2,
                "acquisition_ips": 2,
                "num_generators": 2,
                "num_acquisitions": 2,
                # -------------------------------------------------------------
            },
            "generators": [{"id": 0}, {"id": 1}],
            "acquisitions": [
                {
                    "id": 0,
                    "parallelism": 8,
                    "max_duration_cycles": 65536,
                    "decimated_fifo_depth_words": 16384,
                    "dec_output_width_bits": 32,
                    "raw_fifo_depth_words": 16384,
                    "raw_output_width_bits": 256,
                },
                {
                    "id": 1,
                    "parallelism": 8,
                    "max_duration_cycles": 65536,
                    "decimated_fifo_depth_words": 16384,
                    "dec_output_width_bits": 32,
                    "raw_fifo_depth_words": 16384,
                    "raw_output_width_bits": 256,
                },
            ],
            "trigger": {},
        }
        self.dma = MockDMA()
        self.axis_switch = MagicMock()
        self.trigger = MockTriggerDriver()
        self.generators = [MockGeneratorDriver(0), MockGeneratorDriver(1)]
        self.acquisitions = [MockAcquisitionDriver(0), MockAcquisitionDriver(1)]

    def summary(self) -> dict:
        """Return the hardware summary."""
        return self.hw_specs["summary"]

    def configure_dac_mix_mode(self, *args: object, **kwargs: object) -> dict:
        """Mock DAC mix mode configuration."""
        return {"changed": False}

    def configure_adc_mix_mode(self, *args: object, **kwargs: object) -> dict:
        """Mock ADC mix mode configuration."""
        return {"changed": False}

    # Properties used for direct access via adapter
    @property
    def num_generators(self) -> int:
        """Return number of generators."""
        return len(self.generators)

    @property
    def num_acquisitions(self) -> int:
        """Return number of acquisitions."""
        return len(self.acquisitions)
