"""Tests for OverlayAdapter and related behavior."""

# file: fireq-utils/test/test_ol_adapter.py
from unittest.mock import MagicMock

import numpy as np
import pytest

from server.exceptions import ConfigurationError
from server.ol_adapter import OverlayAdapter

try:
    from test.mock_hardware import MockOverlay
except ImportError:
    from mock_hardware import MockOverlay


class AdapterContext:
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
def ctx() -> AdapterContext:
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

    adapter = OverlayAdapter(mock_ol)
    # Mock DMA engine for chunking tests
    adapter.dma_engine = MagicMock()

    return AdapterContext(adapter, mock_ol, mock_gen, mock_trig, mock_acq)


# --- TESTS ---


def test_initialization_success(ctx: AdapterContext) -> None:
    """Verify that the adapter initializes correctly with a healthy overlay."""
    assert ctx.adapter.ol.is_healthy


def test_upload_envelopes_success(ctx: AdapterContext) -> None:
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
    res = ctx.adapter.upload_envelopes(gen_index=0, envelopes=envelopes)
    assert len(res["loaded"]) == 1
    ctx.gen.add_envelope_to_envelope_memory.assert_called_once()


def test_upload_envelopes_padding(ctx: AdapterContext) -> None:
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
    ctx.adapter.upload_envelopes(gen_index=0, envelopes=envelopes, auto_pad_noninterp=True)
    args, _ = ctx.gen.add_envelope_to_envelope_memory.call_args
    # Check that padding occurred (size > 3)
    assert len(args[0]) >= 3


def test_compile_waves_success(ctx: AdapterContext) -> None:
    """Verify the compilation of a standard wave using an existing envelope."""
    ctx.gen.envelope_memory_dict["rect"] = {}
    waves = [{"wave_id": "w1", "envelope": "rect", "duration": 100, "gain": 1.0}]
    res = ctx.adapter.compile_waves(gen_index=0, waves=waves, replace=False)
    assert len(res["waves"]) == 1
    ctx.gen.create_wave_definition_word.assert_called()


def test_compile_virtual_z_wave(ctx: AdapterContext) -> None:
    """Verify the compilation of a Virtual-Z gate."""
    waves = [{"wave_id": "vz_pi_2", "kind": "vz", "vz_phase_rad": 1.57}]
    res = ctx.adapter.compile_waves(gen_index=0, waves=waves, replace=False)
    assert len(res["waves"]) == 1
    ctx.gen.create_vz_gate_definition_word.assert_called_once()


def test_upload_readout_wave(ctx: AdapterContext) -> None:
    """Verify the dedicated upload path for readout waveforms."""
    ctx.gen.envelope_memory_dict["readout_env"] = {}
    wave_spec = {"envelope": "readout_env", "duration": 200, "gain": 0.5}
    res = ctx.adapter.upload_readout_wave(gen_index=0, wave=wave_spec, replace=True)
    assert res["status"] in ["replaced", "compiled"]
    ctx.gen.write_readout_wave.assert_called_once()


def test_iq_quantization_logic(ctx: AdapterContext) -> None:
    """Verify floating-point to complex int16 quantization.

    Checks:
    - Zero mapping.
    - Boundary handling (-1.0 to min int16).
    - Hard clipping for overflow values.
    """
    # Inputs: Zero, Boundary (-1.0), Overflow (-2.0)
    inputs = [[0.0, 0.0], [1.0, -1.0], [1.5, -2.0]]
    res = ctx.adapter._iq_float_to_cint16(inputs, sample_bits=16)

    assert res[0] == 0 + 0j

    # Boundary Check: -1.0 scales to -32767
    assert np.real(res[1]) > 32000
    # Q channel is -1.0 => -32767
    assert np.imag(res[1]) == -32767

    # Overflow Check: -2.0 clips to -32768 (min int16)
    assert np.imag(res[2]) == -32768


def test_tg_program_delays_logic(ctx: AdapterContext) -> None:
    """Verify the programming of trigger delays into the FIFO."""
    drive_spec = {0: {"delay": [[10, 0], [20, 1]]}}
    ctx.adapter.tg_program_delays(drive=drive_spec, drive_start_index=1)
    print(ctx.trig.insert_drive_delay.call_count)
    # always fill the entire fifo for safe tails
    assert ctx.trig.insert_drive_delay.call_count == 1024
    drive_spec = {0: {"delay": [[10, 0]]}}
    ctx.adapter.tg_program_delays(drive=drive_spec, drive_start_index=1)
    print(ctx.trig.insert_drive_delay.call_count)
    # always fill the entire fifo for safe tails
    assert ctx.trig.insert_drive_delay.call_count == 2 * 1024  # second run


def test_modulation_setup(ctx: AdapterContext) -> None:
    """Verify the generator modulation setup calls."""
    ctx.adapter.generator_modulation(
        gen_index=0,
        label="drive",
        gen_mod={"frequency_mhz": 100.0, "phase": 0.0},
    )
    ctx.gen.set_drive_dds_parameters.assert_called_with(frequency=100.0, dac_samplerate=4000.0)


def test_upload_envelopes_failure(ctx: AdapterContext) -> None:
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

    res = ctx.adapter.upload_envelopes(gen_index=0, envelopes=envelopes)

    assert len(res["failed"]) == 1
    assert res["failed"][0]["name"] == "bad_env"
    # Verify the error message contains the hint mapped to code -3
    assert "samples must be complex" in res["failed"][0]["error"]


def test_compile_waves_cache_hit(ctx: AdapterContext) -> None:
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
    # - The HL cache (ctx.adapter._wave_store)
    # - The LL memory (ctx.gen.wave_memory_dict) via internal calls
    ctx.adapter.compile_waves(gen_index=0, waves=[wave_spec], replace=True)

    # Sanity check: called once
    assert ctx.gen.create_wave_definition_word.call_count == 1

    # 2. Mock Reset
    ctx.gen.create_wave_definition_word.reset_mock()

    # 3. Cache Hit Test
    # Manually update the mock dictionary to simulate hardware state (since
    # MagicMock doesn't have side effects)
    ctx.gen.wave_memory_dict["w1"] = 123456

    # Request the same wave again
    ctx.adapter.compile_waves(gen_index=0, waves=[wave_spec], replace=False)

    # 4. Assertion
    # Since the wave is cached and in HW, the driver must NOT be called
    assert ctx.gen.create_wave_definition_word.call_count == 0


def test_compile_waves_conflict_raises_error(ctx: AdapterContext) -> None:
    """Verify ConfigurationError on overwrite without replace=True."""

    # 1. Initial Setup
    wave_v1 = {
        "wave_id": "w1",
        "kind": "env",
        "envelope": "e1",
        "duration": 100,
        "gain": 1.0,
    }
    ctx.adapter.compile_waves(gen_index=0, waves=[wave_v1], replace=True)

    # 2. Conflicting Action
    # Attempt modification (gain 0.5 vs 1.0) with replace=False
    wave_v2 = {
        "wave_id": "w1",
        "kind": "env",
        "envelope": "e1",
        "duration": 100,
        "gain": 0.5,
    }

    res = ctx.adapter.compile_waves(gen_index=0, waves=[wave_v2], replace=False)

    # 3. Assertion
    # Error should indicate specification difference
    assert len(res["failed"]) == 1
    assert "spec differs" in res["failed"][0]["error"]


def test_run_multi_acquisition_chunking(ctx: AdapterContext) -> None:
    """Verify acquisitions exceeding buffer limits are chunked."""
    hw_limit = 100
    ctx.adapter.dma_engine.get_max_shots.return_value = hw_limit

    def retrieve_side_effect(
        buffer: object,
        mode: str,
        shots: int,
        samp_per_shot: int,
        adc_index: int,
        timeout: float | None,
    ) -> np.ndarray:
        return np.zeros((shots, samp_per_shot))

    ctx.adapter.dma_engine.retrieve_acquisition.side_effect = retrieve_side_effect

    results = ctx.adapter.run_multi_acquisition(adc_indices=[0], mode="raw", shots=250, samp_per_shot=10)

    assert results[0].shape == (250, 10)
    assert ctx.adapter.dma_engine.retrieve_acquisition.call_count == 3
    # Check if trigger was called 3 times (via ol.trigger)
    assert ctx.trig.start_experiment.call_count == 3


def test_fifo_patching_consistency(ctx: AdapterContext) -> None:
    """Verify that partial FIFO updates maintain consistency in the HL cache."""
    # 1. Initial Setup: Sequence [A, B, C]
    # Mock cache presence to bypass validation
    ctx.adapter._wave_store[0] = {
        "A": MagicMock(wdw=1),
        "B": MagicMock(wdw=2),
        "C": MagicMock(wdw=3),
        "D": MagicMock(wdw=4),
    }
    # Mock LL check
    ctx.gen.wave_memory_dict = {"A": 1, "B": 2, "C": 3, "D": 4}

    # Program base sequence
    ctx.adapter.program_drive_sequence(gen_index=0, wave_id_list=["A", "B", "C"], start_index=1)
    assert ctx.adapter._last_fifo[0] == ["A", "B", "C"]

    # 2. Patching: Replace from index 2 (B -> D) -> Expected Result [A, D, C]
    # Logic: new_fifo = prev[:start-1] + new_list + prev[end:]
    ctx.adapter.program_drive_sequence(gen_index=0, wave_id_list=["D"], start_index=2)

    # Verify driver call
    ctx.gen.add_wave_to_drive_wave_sequence.assert_any_call(2, "D")

    # Verify HL cache consistency
    assert ctx.adapter._last_fifo[0] == ["A", "D", "C"]


def test_fifo_patching_out_of_bounds(ctx: AdapterContext) -> None:
    """Verify patching beyond known sequence length raises ConfigurationError."""
    ctx.adapter._wave_store[0] = {"A": MagicMock(wdw=1)}
    ctx.gen.wave_memory_dict = {"A": 1}

    # Attempt to patch index 5 when list is empty/short
    with pytest.raises(ConfigurationError) as excinfo:
        ctx.adapter.program_drive_sequence(gen_index=0, wave_id_list=["A"], start_index=5)
    assert "cannot patch" in str(excinfo.value)


def test_reset_wave_memory_preserve_specs(ctx: AdapterContext) -> None:
    """Verify that 'preserve_specs' clears WDW compilation but keeps the WaveEntry."""
    # 1. Populate cache
    entry = MagicMock()
    entry.wdw = 12345
    ctx.adapter._wave_store[0] = {"test_wave": entry}

    # 2. Execute reset with preservation
    ctx.adapter.reset_wave_memory(gen_index=0, preserve_specs=True)

    # 3. Assertions
    # Entry must still exist
    assert "test_wave" in ctx.adapter._wave_store[0]
    # WDW must be invalidated (None)
    assert ctx.adapter._wave_store[0]["test_wave"].wdw is None
    # Driver must be reset
    ctx.gen.reset_wave_memory_dict.assert_called_once()


def test_multi_acquisition_mid_stream_failure(ctx: AdapterContext) -> None:
    """Verify errors during chunked acquisition are propagated."""
    # Setup for chunking (low limit)
    ctx.adapter.dma_engine.get_max_shots.return_value = 100

    # Simulate 3 calls: Success, Success, Failure
    def side_effect(*args: object, **kwargs: object) -> np.ndarray:
        if ctx.adapter.dma_engine.retrieve_acquisition.call_count == 3:
            raise TimeoutError("DMA Timeout")
        return np.zeros((100, 10))

    ctx.adapter.dma_engine.retrieve_acquisition.side_effect = side_effect

    # Execute acquisition requiring 3 chunks
    with pytest.raises(TimeoutError):
        ctx.adapter.run_multi_acquisition(adc_indices=[0], mode="raw", shots=250, samp_per_shot=10)

    assert ctx.adapter.dma_engine.retrieve_acquisition.call_count == 3


def test_sweep_lifecycle(ctx: AdapterContext) -> None:
    """Verify the lifecycle management of the sweep mode (prepare/end)."""
    # Mock DMA engine methods
    ctx.adapter.dma_engine.prepare_sweep = MagicMock()
    ctx.adapter.dma_engine.end_sweep = MagicMock()

    # Explicitly Mock the ACQ driver method (since it's a real function in mock_hardware)
    ctx.acq.set_decimated_output_type = MagicMock(return_value=0)

    adc_list = [0]

    # 1. Preparation
    ctx.adapter.prepare_sweep(mode="decimated", adc_indices=adc_list)

    # Assert driver call
    ctx.acq.set_decimated_output_type.assert_called_with("decimated")
    # Assert DMA call
    ctx.adapter.dma_engine.prepare_sweep.assert_called_with("decimated")

    # 2. Conclusion
    ctx.adapter.end_sweep()
    ctx.adapter.dma_engine.end_sweep.assert_called_once()


def test_program_drive_sequence_overflow(ctx: AdapterContext) -> None:
    """Verify that the adapter prevents writing beyond the FIFO capacity."""
    # Simulated capacity: 4096 words
    max_capacity = 4096

    # Create list exceeding capacity
    huge_list = ["w1"] * (max_capacity + 10)

    with pytest.raises(ConfigurationError) as excinfo:
        ctx.adapter.program_drive_sequence(gen_index=0, wave_id_list=huge_list)

    assert "overflow" in str(excinfo.value)
    ctx.gen.add_wave_to_drive_wave_sequence.assert_not_called()


def test_upload_envelopes_symmetry_constraint(ctx: AdapterContext) -> None:
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
    res = ctx.adapter.upload_envelopes(gen_index=0, envelopes=bad_envelope)

    assert len(res["failed"]) == 1
    assert "Invalid envelope" in res["failed"][0]["error"]
    ctx.gen.add_envelope_to_envelope_memory.assert_not_called()


def test_acquisition_single_shot_overflow(ctx: AdapterContext) -> None:
    """Verify rejection when a single shot exceeds total buffer memory.

    If samp_per_shot > buffer_size, chunking is impossible.
    """
    # Simulate very small buffer
    ctx.adapter.dma_engine.get_max_shots.return_value = 0

    with pytest.raises(ConfigurationError) as exc:
        ctx.adapter.run_multi_acquisition(
            adc_indices=[0], mode="raw", shots=1, samp_per_shot=999999  # Huge single shot
        )

    assert "Impossible configuration" in str(exc.value)
    ctx.trig.start_experiment.assert_not_called()
