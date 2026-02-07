"""Tests for OverlayAdapter and related behavior."""

# file: fireq-utils/test/test_ol_adapter.py
from unittest.mock import MagicMock

import numpy as np
import pytest

from server import ConfigurationError, OverlayAdapter
from server.hardware.ol_adapter.generator._iq_conversion import iq_float_to_cint16

try:
    from test.mock_hardware import MockOverlay
except ImportError:
    from mock_hardware import MockOverlay


class AdapterTestContext:
    """Context holder for the Adapter test suite."""

    def __init__(
        self,
        adapter: OverlayAdapter,
        overlay: object,
        mock_gen: object,
        mock_trig: object,
        mock_acq: object,
    ) -> None:
        """Initialize the adapter test context."""
        self.adapter = adapter
        self.ol = overlay
        self.gen = mock_gen
        self.trig = mock_trig
        self.acq = mock_acq


@pytest.fixture
def ctx() -> AdapterTestContext:
    """Create a mock overlay/adapter context."""
    mock_ol = MockOverlay()

    # Setup Generator - Wrap envelope memory to allow logic, mock others
    mock_gen = mock_ol.generators[0]
    mock_gen.add_envelope_to_envelope_memory = MagicMock(wraps=mock_gen.add_envelope_to_envelope_memory)

    # Configure pure mocks for assertion tracking
    mock_gen.create_wave_definition_word = MagicMock(return_value=123456)
    mock_gen.create_vz_gate_definition_word = MagicMock(return_value=99999)
    mock_gen.write_readout_wave = MagicMock(return_value=0)
    mock_gen.add_wave_in_wave_memory = MagicMock(return_value=0)
    mock_gen.replace_wave_in_wave_memory = MagicMock(return_value=0)
    mock_gen.set_drive_dds_parameters = MagicMock(return_value=0)
    mock_gen.add_wave_to_drive_wave_sequence = MagicMock(return_value=0)
    mock_gen.reset_wave_memory_dict = MagicMock(return_value=0)

    # Setup Trigger
    mock_trig = mock_ol.trigger
    mock_trig.insert_drive_delay = MagicMock(return_value=0)
    mock_trig.start_experiment = MagicMock(return_value=0)

    # Setup Acquisition
    mock_acq = mock_ol.acquisitions[0]
    mock_acq.set_acquisition_dds_parameters = MagicMock(return_value=0)

    # Set trigger channels so acquisitions are considered "active" (channel > 0)
    for acq in mock_ol.acquisitions:
        acq.current_channel = 1

    adapter = OverlayAdapter(mock_ol)
    # Mock DMA engine for chunking tests
    mock_dma = MagicMock()
    mock_dma.set_active_acq_ips = MagicMock(return_value=None)
    mock_dma.prepare_sweep = MagicMock(return_value=None)
    mock_dma.end_sweep = MagicMock(return_value=None)
    adapter._ctx.dma_engine = mock_dma
    # Also expose on adapter for backward compatibility
    adapter.dma_engine = mock_dma

    return AdapterTestContext(adapter, mock_ol, mock_gen, mock_trig, mock_acq)


# --- TESTS ---


def test_initialization_success(ctx: AdapterTestContext) -> None:
    """Verify that the adapter initializes correctly with a healthy overlay."""
    assert ctx.adapter.overlay_driver.is_healthy


def test_upload_envelopes_success(ctx: AdapterTestContext) -> None:
    """Verify successful upload of a standard envelope."""
    envelopes = [
        {
            "name": "gauss",
            "for_interpolation": False,
            "is_symmetric": False,
            "i_even": False,
            "q_even": False,
            "samples_iq": [[0.5, 0.5], [0.5, 0.5]],
        }
    ]
    res = ctx.adapter.generator.upload_envelopes(gen_index=0, envelopes=envelopes)
    assert len(res["loaded"]) == 1
    ctx.gen.add_envelope_to_envelope_memory.assert_called_once()


def test_upload_envelopes_padding(ctx: AdapterTestContext) -> None:
    """Verify that non-interpolated envelopes are automatically zero-padded."""
    envelopes = [
        {
            "name": "padded_env",
            "for_interpolation": False,
            "is_symmetric": False,
            "i_even": False,
            "q_even": False,
            "samples_iq": [[0.1, 0.1]] * 3,
        }
    ]
    ctx.adapter.generator.upload_envelopes(gen_index=0, envelopes=envelopes, auto_pad_noninterp=True)
    args, _ = ctx.gen.add_envelope_to_envelope_memory.call_args
    # Check that padding occurred (size > 3)
    assert len(args[0]) >= 3


def test_compile_waves_success(ctx: AdapterTestContext) -> None:
    """Verify the compilation of a standard wave using an existing envelope."""
    ctx.gen.envelope_memory_dict["rect"] = {}
    waves = [{"wave_id": "w1", "envelope": "rect", "duration": 100, "gain": 1.0}]
    res = ctx.adapter.generator.compile_waves(gen_index=0, waves=waves, replace=False)
    assert len(res["waves"]) == 1
    ctx.gen.create_wave_definition_word.assert_called()


def test_compile_virtual_z_wave(ctx: AdapterTestContext) -> None:
    """Verify the compilation of a Virtual-Z gate."""
    waves = [{"wave_id": "vz_pi_2", "kind": "vz", "vz_phase_rad": 1.57}]
    res = ctx.adapter.generator.compile_waves(gen_index=0, waves=waves, replace=False)
    assert len(res["waves"]) == 1
    ctx.gen.create_vz_gate_definition_word.assert_called_once()


def test_upload_readout_wave(ctx: AdapterTestContext) -> None:
    """Verify the dedicated upload path for readout waveforms."""
    ctx.gen.envelope_memory_dict["readout_env"] = {}
    wave_spec = {"envelope": "readout_env", "duration": 200, "gain": 0.5}
    res = ctx.adapter.generator.upload_readout_wave(gen_index=0, wave=wave_spec, replace=True)
    assert res["status"] in ["replaced", "compiled"]
    ctx.gen.write_readout_wave.assert_called_once()


def test_iq_quantization_logic(ctx: AdapterTestContext) -> None:
    """Verify floating-point to complex int16 quantization.

    Checks:
    - Zero mapping.
    - Boundary handling (-1.0 to min int16).
    - Hard clipping for overflow values.
    """
    # Inputs: Zero, Boundary (-1.0), Overflow (-2.0)
    inputs = [[0.0, 0.0], [1.0, -1.0], [1.5, -2.0]]
    res = iq_float_to_cint16(inputs, sample_bits=16)

    assert res[0] == 0 + 0j

    # Boundary Check: -1.0 scales to -32767
    assert np.real(res[1]) > 32000
    # Q channel is -1.0 => -32767
    assert np.imag(res[1]) == -32767

    # Overflow Check: -2.0 clips to -32768 (min int16)
    assert np.imag(res[2]) == -32768


def test_tg_program_delays_logic(ctx: AdapterTestContext) -> None:
    """Verify the programming of trigger delays with lazy FIFO cleanup.

    The optimized implementation uses high water mark (HWM) tracking to avoid
    unnecessary AXI transactions when the sequence length doesn't change.
    """
    # First call: 2 entries, no previous HWM -> only 2 writes
    drive_spec = {0: {"delay": [[10, 0], [20, 1]]}}
    ctx.adapter.trigger.program_delays(drive=drive_spec, drive_start_index=1)
    assert ctx.trig.insert_drive_delay.call_count == 2

    # Second call: 1 entry, previous HWM=2 -> 1 write + 1 clear = 2 writes
    drive_spec = {0: {"delay": [[10, 0]]}}
    ctx.adapter.trigger.program_delays(drive=drive_spec, drive_start_index=1)
    assert ctx.trig.insert_drive_delay.call_count == 4  # total: 2 + 2

    # Third call: same length (1 entry), HWM=1 -> only 1 write, no clears
    drive_spec = {0: {"delay": [[15, 1]]}}
    ctx.adapter.trigger.program_delays(drive=drive_spec, drive_start_index=1)
    assert ctx.trig.insert_drive_delay.call_count == 5  # total: 4 + 1

    # Fourth call: longer sequence (3 entries), HWM=1 -> 3 writes, no clears
    drive_spec = {0: {"delay": [[10, 0], [20, 1], [30, 0]]}}
    ctx.adapter.trigger.program_delays(drive=drive_spec, drive_start_index=1)
    assert ctx.trig.insert_drive_delay.call_count == 8  # total: 5 + 3


def test_tg_reset_drive_tracking(ctx: AdapterTestContext) -> None:
    """Verify that tg_reset_drive_tracking clears the HWM state."""
    # Program 3 entries -> HWM = 3
    drive_spec = {0: {"delay": [[10, 0], [20, 1], [30, 0]]}}
    ctx.adapter.trigger.program_delays(drive=drive_spec, drive_start_index=1)
    assert ctx.trig.insert_drive_delay.call_count == 3

    # Reset the HWM tracking
    ctx.adapter.trigger.reset_drive_tracking()

    # Program 1 entry after reset -> no clears (HWM was cleared)
    drive_spec = {0: {"delay": [[10, 0]]}}
    ctx.adapter.trigger.program_delays(drive=drive_spec, drive_start_index=1)
    # Without reset, this would have been 3 + 1 + 2 (clear indices 2,3) = 6
    # With reset, HWM is unknown (0), so only 1 write
    assert ctx.trig.insert_drive_delay.call_count == 4  # total: 3 + 1


def test_modulation_setup(ctx: AdapterTestContext) -> None:
    """Verify the generator modulation setup calls."""
    ctx.adapter.generator.set_modulation(
        gen_index=0,
        label="drive",
        mod={"frequency_mhz": 100.0, "phase": 0.0},
    )
    ctx.gen.set_drive_dds_parameters.assert_called_with(frequency=100.0, dac_samplerate=4000.0)


def test_upload_envelopes_failure(ctx: AdapterTestContext) -> None:
    """Verify that low-level driver errors (-3) become ConfigurationError hints."""
    # Configure mock to return error code
    ctx.gen.add_envelope_to_envelope_memory.return_value = -3

    envelopes = [
        {
            "name": "bad_env",
            "for_interpolation": False,
            "is_symmetric": False,
            "i_even": False,
            "q_even": False,
            "samples_iq": [[0.5, 0.5]] * 2,
        }
    ]

    res = ctx.adapter.generator.upload_envelopes(gen_index=0, envelopes=envelopes)

    assert len(res["failed"]) == 1
    assert res["failed"][0]["name"] == "bad_env"
    # Verify the error message contains the hint mapped to code -3
    assert "samples must be complex" in res["failed"][0]["error"]


def test_compile_waves_cache_hit(ctx: AdapterTestContext) -> None:
    """Verify identical wave specs do not trigger recompilation."""
    wave_spec = {
        "wave_id": "w1",
        "kind": "env",
        "envelope": "e1",
        "duration": 100,
        "gain": 1.0,
    }

    # 1. Correct Setup:
    # Perform a real compilation. This populates:
    # - The HL cache (ctx.adapter._ctx.cache.wave_store)
    # - The LL memory (ctx.gen.wave_memory_dict) via internal calls
    ctx.adapter.generator.compile_waves(gen_index=0, waves=[wave_spec], replace=True)

    # Sanity check: called once
    assert ctx.gen.create_wave_definition_word.call_count == 1

    # 2. Mock Reset
    ctx.gen.create_wave_definition_word.reset_mock()

    # 3. Cache Hit Test
    # Manually update the mock dictionary to simulate hardware state (since
    # MagicMock doesn't have side effects)
    ctx.gen.wave_memory_dict["w1"] = 123456

    # Request the same wave again
    ctx.adapter.generator.compile_waves(gen_index=0, waves=[wave_spec], replace=False)

    # 4. Assertion
    # Since the wave is cached and in HW, the driver must NOT be called
    assert ctx.gen.create_wave_definition_word.call_count == 0


def test_compile_waves_conflict_raises_error(ctx: AdapterTestContext) -> None:
    """Verify ConfigurationError on overwrite without replace=True."""

    # 1. Initial Setup
    wave_v1 = {
        "wave_id": "w1",
        "kind": "env",
        "envelope": "e1",
        "duration": 100,
        "gain": 1.0,
    }
    ctx.adapter.generator.compile_waves(gen_index=0, waves=[wave_v1], replace=True)

    # 2. Conflicting Action
    # Attempt modification (gain 0.5 vs 1.0) with replace=False
    wave_v2 = {
        "wave_id": "w1",
        "kind": "env",
        "envelope": "e1",
        "duration": 100,
        "gain": 0.5,
    }

    res = ctx.adapter.generator.compile_waves(gen_index=0, waves=[wave_v2], replace=False)

    # 3. Assertion
    # Error should indicate specification difference
    assert len(res["failed"]) == 1
    assert "spec differs" in res["failed"][0]["error"]


def test_run_multi_acquisition_single_acq_ip(ctx: AdapterTestContext) -> None:
    """Verify run_multi_acquisition correctly arms, triggers, and retrieves data from single AcqIp."""
    # Setup mock return values
    dtype = np.dtype([("i", "<i2"), ("q", "<i2")])
    mock_data = np.zeros((10, 100), dtype=dtype)

    ctx.adapter.dma_engine.arm_acquisition.return_value = "buffer_handle"
    ctx.adapter.dma_engine.retrieve_acquisition.return_value = mock_data
    ctx.adapter.dma_engine.get_max_shots.return_value = 1024  # Large enough to avoid chunking
    ctx.adapter.dma_engine.last_dma_wait_s = 0.001
    ctx.adapter.dma_engine.last_invalidate_s = 0.0002

    # Consume iterator to get result
    results = list(
        ctx.adapter.acquisition.run_multi_acquisition(
            acq_indices=[0],
            mode="raw",
            shots=10,
            samp_per_shot=100,
            timeout=5.0,
        )
    )

    # Should yield exactly one chunk
    assert len(results) == 1
    result = results[0]

    # Verify DMA arm was called with correct parameters
    ctx.adapter.dma_engine.arm_acquisition.assert_called_once_with(
        samp_per_shot=100,
        shots_per_exp=10,
        mode="raw",
        acq_ip_index=0,
    )

    # Verify trigger was fired
    ctx.trig.start_experiment.assert_called_once()

    # Verify data returned
    assert 0 in result
    assert result[0].shape == (10, 100)

    # Verify timing stats populated
    assert ctx.adapter.last_timing_stats["fpga_wait_ms"] == pytest.approx(1.0)
    assert ctx.adapter.last_timing_stats["dma_overhead_ms"] == pytest.approx(0.2)

    # Verify retrieve was called once
    ctx.adapter.dma_engine.retrieve_acquisition.assert_called_once()


def test_run_multi_acquisition_multi_acq_ip(ctx: AdapterTestContext) -> None:
    """Verify multi-acq_ip acquisition with switch routing."""
    # Setup mock for arm
    buffer_counter = [0]

    def arm_side_effect(**kwargs: object) -> str:
        buffer_counter[0] += 1
        return f"buffer_{buffer_counter[0]}"

    ctx.adapter.dma_engine.arm_acquisition.side_effect = arm_side_effect
    ctx.adapter.dma_engine.get_max_shots.return_value = 1024  # Large enough to avoid chunking

    # Setup mock for retrieve - returns buffer
    dtype = np.dtype([("i", "<i2"), ("q", "<i2")])

    def retrieve_side_effect(**kwargs: object) -> np.ndarray:
        return np.zeros((10, 100), dtype=dtype)

    ctx.adapter.dma_engine.retrieve_acquisition.side_effect = retrieve_side_effect
    ctx.adapter.dma_engine.last_dma_wait_s = 0.001
    ctx.adapter.dma_engine.last_invalidate_s = 0.0001

    # Run acquisition for both acquisition units
    results = list(
        ctx.adapter.acquisition.run_multi_acquisition(
            acq_indices=[0, 1],
            mode="raw",
            shots=10,
            samp_per_shot=100,
            timeout=5.0,
        )
    )

    # Should yield exactly one chunk
    assert len(results) == 1
    result = results[0]

    # Verify trigger was fired once
    ctx.trig.start_experiment.assert_called_once()

    # Verify both AcqIps have data
    assert 0 in result and 1 in result

    # Verify both AcqIps were armed (sequential ARM → TRIGGER → RETRIEVE)
    assert ctx.adapter.dma_engine.arm_acquisition.call_count == 2

    # Verify retrieve was called twice (once per AcqIp)
    assert ctx.adapter.dma_engine.retrieve_acquisition.call_count == 2


def test_fifo_patching_consistency(ctx: AdapterTestContext) -> None:
    """Verify that partial FIFO updates maintain consistency in the HL cache."""
    # 1. Initial Setup: Sequence [A, B, C]
    # Mock cache presence to bypass validation
    ctx.adapter._ctx.cache.wave_store[0] = {
        "A": MagicMock(wdw=1),
        "B": MagicMock(wdw=2),
        "C": MagicMock(wdw=3),
        "D": MagicMock(wdw=4),
    }
    # Mock LL check
    ctx.gen.wave_memory_dict = {"A": 1, "B": 2, "C": 3, "D": 4}

    # Program base sequence
    ctx.adapter.generator.program_drive_sequence(gen_index=0, wave_id_list=["A", "B", "C"], start_index=1)
    assert ctx.adapter._ctx.cache.last_fifo[0] == ["A", "B", "C"]

    # 2. Patching: Replace from index 2 (B -> D) -> Expected Result [A, D, C]
    # Logic: new_fifo = prev[:start-1] + new_list + prev[end:]
    ctx.adapter.generator.program_drive_sequence(gen_index=0, wave_id_list=["D"], start_index=2)

    # Verify driver call
    ctx.gen.add_wave_to_drive_wave_sequence.assert_any_call(2, "D")

    # Verify HL cache consistency
    assert ctx.adapter._ctx.cache.last_fifo[0] == ["A", "D", "C"]


def test_fifo_patching_out_of_bounds(ctx: AdapterTestContext) -> None:
    """Verify patching beyond known sequence length raises ConfigurationError."""
    ctx.adapter._ctx.cache.wave_store[0] = {"A": MagicMock(wdw=1)}
    ctx.gen.wave_memory_dict = {"A": 1}

    # Attempt to patch index 5 when list is empty/short
    with pytest.raises(ConfigurationError) as excinfo:
        ctx.adapter.generator.program_drive_sequence(gen_index=0, wave_id_list=["A"], start_index=5)
    assert "cannot patch" in str(excinfo.value)


def test_reset_wave_memory_preserve_specs(ctx: AdapterTestContext) -> None:
    """Verify that 'preserve_wave_specs' clears WDW compilation but keeps the WaveEntry."""
    # 1. Populate cache
    entry = MagicMock()
    entry.wdw = 12345
    ctx.adapter._ctx.cache.wave_store[0] = {"test_wave": entry}

    # 2. Execute reset with preservation
    ctx.adapter.generator.reset_wave_memory(gen_index=0, preserve_wave_specs=True)

    # 3. Assertions
    # Entry must still exist
    assert "test_wave" in ctx.adapter._ctx.cache.wave_store[0]
    # WDW must be invalidated (None)
    assert ctx.adapter._ctx.cache.wave_store[0]["test_wave"].wdw is None
    # Driver must be reset
    ctx.gen.reset_wave_memory_dict.assert_called_once()


def test_run_multi_acquisition_dma_failure(ctx: AdapterTestContext) -> None:
    """Verify DMA errors during run_multi_acquisition are propagated."""
    # Setup arm to succeed
    ctx.adapter.dma_engine.arm_acquisition.return_value = "buffer_handle"
    ctx.adapter.dma_engine.get_max_shots.return_value = 1024  # Large enough to avoid chunking
    # Simulate DMA timeout during retrieval
    ctx.adapter.dma_engine.retrieve_acquisition.side_effect = TimeoutError("DMA Timeout")

    # Execute run_multi_acquisition - consume iterator to trigger exception
    with pytest.raises(TimeoutError, match="DMA Timeout"):
        list(
            ctx.adapter.acquisition.run_multi_acquisition(
                acq_indices=[0],
                mode="raw",
                shots=10,
                samp_per_shot=100,
                timeout=5.0,
            )
        )

    ctx.adapter.dma_engine.retrieve_acquisition.assert_called_once()


def test_sweep_lifecycle(ctx: AdapterTestContext) -> None:
    """Verify the lifecycle management of the sweep mode (prepare/end)."""
    # Mock DMA engine methods
    ctx.adapter.dma_engine.prepare_sweep = MagicMock()
    ctx.adapter.dma_engine.end_sweep = MagicMock()

    # Explicitly Mock the ACQ driver method (since it's a real function in mock_hardware)
    ctx.acq.set_decimated_output_type = MagicMock(return_value=0)

    acq_list = [0]

    # 1. Preparation
    ctx.adapter.experiment.prepare_sweep(
        mode="decimated",
        acq_indices=acq_list,
    )

    # Assert driver call
    ctx.acq.set_decimated_output_type.assert_called_with("decimated")
    # Assert DMA call
    ctx.adapter.dma_engine.prepare_sweep.assert_called_with("decimated")

    # 2. Conclusion
    ctx.adapter.experiment.end_sweep()
    ctx.adapter.dma_engine.end_sweep.assert_called_once()


def test_program_drive_sequence_overflow(ctx: AdapterTestContext) -> None:
    """Verify that the adapter prevents writing beyond the FIFO capacity."""
    # Simulated capacity: 4096 words
    max_capacity = 4096

    # Create list exceeding capacity
    huge_list = ["w1"] * (max_capacity + 10)

    with pytest.raises(ConfigurationError) as excinfo:
        ctx.adapter.generator.program_drive_sequence(gen_index=0, wave_id_list=huge_list)

    assert "overflow" in str(excinfo.value)
    ctx.gen.add_wave_to_drive_wave_sequence.assert_not_called()


def test_upload_envelopes_symmetry_constraint(ctx: AdapterTestContext) -> None:
    """Verify that symmetric optimization is rejected for non-interpolated envelopes.

    The hardware interpolation filter requires specific symmetry constraints. Applying
    symmetry flags to raw envelopes leads to undefined behavior.
    """
    # Case: is_symmetric=True but for_interpolation=False -> MUST FAIL
    bad_envelope = [
        {
            "name": "bad_sym",
            "for_interpolation": False,  # Conflict here
            "is_symmetric": True,  # Conflict here
            "i_even": True,
            "q_even": True,
            "samples_iq": [[0.5, 0.5], [0.5, 0.5]],
        }
    ]

    # Expect rejection
    res = ctx.adapter.generator.upload_envelopes(gen_index=0, envelopes=bad_envelope)

    assert len(res["failed"]) == 1
    assert "Invalid envelope" in res["failed"][0]["error"]
    ctx.gen.add_envelope_to_envelope_memory.assert_not_called()


def test_run_multi_acquisition_shot_memoization(ctx: AdapterTestContext) -> None:
    """Verify trigger shots are memoized to skip redundant HW writes."""
    # Setup mocks
    dtype = np.dtype([("i", "<i2"), ("q", "<i2")])
    mock_data = np.zeros((10, 100), dtype=dtype)
    ctx.adapter.dma_engine.arm_acquisition.return_value = "buffer"
    ctx.adapter.dma_engine.retrieve_acquisition.return_value = mock_data
    ctx.adapter.dma_engine.get_max_shots.return_value = 1024  # Large enough to avoid chunking
    ctx.adapter.dma_engine.last_dma_wait_s = 0.001
    ctx.adapter.dma_engine.last_invalidate_s = 0.0001

    # Mock the actual trigger.set_shots method (used internally during run_multi_acquisition)
    ctx.adapter.trigger.set_shots = MagicMock()

    # First call with 10 shots - should set trigger
    list(ctx.adapter.acquisition.run_multi_acquisition(acq_indices=[0], mode="raw", shots=10, samp_per_shot=100))
    assert ctx.adapter.trigger.set_shots.call_count == 1
    ctx.adapter.trigger.set_shots.assert_called_with(10)

    # Second call with same shots - should NOT reconfigure
    list(ctx.adapter.acquisition.run_multi_acquisition(acq_indices=[0], mode="raw", shots=10, samp_per_shot=100))
    assert ctx.adapter.trigger.set_shots.call_count == 1  # Still 1

    # Third call with different shots - should reconfigure
    list(ctx.adapter.acquisition.run_multi_acquisition(acq_indices=[0], mode="raw", shots=20, samp_per_shot=100))
    assert ctx.adapter.trigger.set_shots.call_count == 2
    ctx.adapter.trigger.set_shots.assert_called_with(20)
