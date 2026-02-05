# file: fireq-utils/test/test_message_handler.py
from unittest.mock import MagicMock

import numpy as np
import pytest

from server import MessageHandler, OverlayAdapter, WaveCompilationError
from server.models import BinaryChunk, StreamHeader, StreamTiming

# Attempt to import Mock Hardware; fallback to local import if the file is adjacent
try:
    from test.mock_hardware import MockOverlay
except ImportError:
    from mock_hardware import MockOverlay


@pytest.fixture
def stack() -> object:
    """Provides a MessageHandler connected to MockOverlay via OverlayAdapter.

    This stack mocks the DMA engine to bypass hardware buffer calculations that are not
    valid in a simulation environment.
    """

    class HandlerStack:
        def __init__(self) -> None:
            # 1. Create Mock Hardware
            self.overlay = MockOverlay()

            # 2. Create Adapter Layer
            self.adapter = OverlayAdapter(self.overlay)

            # 3. CRITICAL: Mock the DMA Engine
            # This bypasses hardware buffer calculations that fail in simulation
            self.adapter.dma_engine = MagicMock()
            self.adapter.dma_engine.get_max_shots.return_value = 999999

            # Setup valid DMA buffer return - returns just buffer (no valid_words)
            def retrieve_side_effect(
                buffer: object,
                timeout: object = None,
                skip_timeout: bool = False,
            ) -> np.ndarray:
                # Return compact I/Q format (structured array)
                # Note: In tests, we create dummy data; real shape comes from buffer
                dtype = np.dtype([("i", "<i2"), ("q", "<i2")])
                data = np.zeros((10, 256), dtype=dtype)
                return data

            self.adapter.dma_engine.retrieve_acquisition.side_effect = retrieve_side_effect

            # Setup timing tracking attributes on DMA mock
            self.adapter.dma_engine.last_dma_wait_s = 0.001
            self.adapter.dma_engine.last_invalidate_s = 0.0001

            # Mock run_acquisition method on adapter
            # This is used by _stream_acquisition_only in MessageHandler
            def run_acquisition_side_effect(
                adc_indices: list,
                mode: str,
                shots: int,
                samp_per_shot: int,
                timeout: float | None = None,
            ) -> tuple:
                """Return data dict and timing info (fpga_wait_s, dma_overhead_s)."""
                result = {}
                for adc in adc_indices:
                    if mode == "accumulated":
                        dtype = np.dtype([("i", "<i4"), ("q", "<i4")])
                        result[adc] = np.zeros(shots, dtype=dtype)
                    else:
                        dtype = np.dtype([("i", "<i2"), ("q", "<i2")])
                        result[adc] = np.zeros((shots, samp_per_shot), dtype=dtype)
                return result, 0.001, 0.0001

            self.adapter.run_acquisition = MagicMock(side_effect=run_acquisition_side_effect)

            # Mock run_multi_acquisition - generator that yields data_dict only
            def run_multi_acquisition_side_effect(
                *,
                adc_indices: list,
                mode: str,
                shots: int,
                samp_per_shot: int,
                timeout: float | None = None,
                validate_chunk: bool = True,
            ):
                """Generator that yields data_dict."""
                result = {}
                for adc in adc_indices:
                    if mode == "accumulated":
                        dtype = np.dtype([("i", "<i4"), ("q", "<i4")])
                        result[adc] = np.zeros(shots, dtype=dtype)
                    else:
                        dtype = np.dtype([("i", "<i2"), ("q", "<i2")])
                        result[adc] = np.zeros((shots, samp_per_shot), dtype=dtype)
                # Update timing stats on adapter (simulating what the real method does)
                self.adapter.last_timing_stats = {
                    "total_ms": 1.0,
                    "fpga_wait_ms": 0.5,
                    "dma_overhead_ms": 0.1,
                    "sw_overhead_ms": 0.35,
                }
                yield result

            self.adapter.run_multi_acquisition = MagicMock(side_effect=run_multi_acquisition_side_effect)

            # Mock _compute_max_hw_shots to return a high limit (no chunking by default)
            self.adapter.acquisition._compute_max_hw_shots = MagicMock(return_value=999999)

            # 4. Initialize Handler
            self.handler = MessageHandler(self.adapter)

    return HandlerStack()


# =============================================================================
# STANDARD TESTS
# =============================================================================


def test_run_experiment_flow(stack: object) -> None:
    """Test the full experiment run flow via the public run() method."""
    config = {
        "acquisitions": [{"acq_index": 0, "output_type": "decimated", "duration": 256, "channel": 1}],
        "trigger": {"shots": 10},
        "timeout": 5.0,
    }

    # Mock the arming phase to return a dummy buffer object
    stack.adapter.dma_engine.arm_acquisition.return_value = "dummy_buffer"

    # Pre-set the acquisition's trigger channel so it's considered "active"
    stack.overlay.acquisitions[0].current_channel = 1
    stack.overlay.acquisitions[1].current_channel = 1

    # Run using the public method and consume all events
    events = list(stack.handler.run(config, cmd="run_experiment", session_id="test_session"))

    # First event should be header (header_binary protocol)
    assert isinstance(events[0], StreamHeader)
    assert events[0].type == "experiment_header"
    metadata = events[0].metadata
    assert metadata["ok"]
    assert "n_chunks" in metadata
    assert metadata["stream_mode"] == "header_binary"

    # Check that binary data chunks were produced (if any ADC was active)
    chunk_events = [e for e in events if isinstance(e, BinaryChunk)]
    if chunk_events:
        # Verify chunk structure (binary-only, no JSON metadata)
        chunk = chunk_events[0]
        assert chunk.binary_data is not None
        assert chunk.timing is not None
        assert 0 in chunk.binary_data  # ADC index 0 should be present


def test_run_sweep_optimized(stack: object) -> None:
    """Test the optimized sweep loop."""
    msg = {
        "sweep_id": "test_sweep",
        "sweep_mode": "cartesian",
        "base": {
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": "$freq"}}],
            "acquisitions": [{"acq_index": 0, "duration": 100}],
            "trigger": {"shots": 1},
        },
        "variables": [{"name": "freq", "values": [10.0, 20.0]}],
    }

    stack.adapter.dma_engine.arm_acquisition.return_value = "dummy_buffer"

    # Consume all items from the generator
    items = list(stack.handler.run_sweep(msg, cmd="run_sweep", session_id="test_session", stop_check=lambda: False))

    # Should have header + binary chunks + final status
    headers = [i for i in items if isinstance(i, StreamHeader)]
    statuses = [i for i in items if isinstance(i, StreamTiming)]

    assert len(headers) == 1
    assert headers[0].type == "sweep_header"
    assert len(statuses) == 1
    assert statuses[0].metadata["ok"]
    assert statuses[0].metadata["n_completed"] == 2


# =============================================================================
# ROBUSTNESS TESTS (EDGE CASES & ERROR HANDLING)
# =============================================================================


class TestRobustness:
    """Advanced test suite covering edge cases and error resilience."""

    def test_input_sanity_validation(self, stack: object) -> None:
        """Verify that errors during setup are caught and reported gracefully.

        **Rationale:** Even though validation is handled at the TCP layer, errors
        during setup (from adapter or drivers) should be caught and reported
        without crashing the handler.
        """
        # Scenario: Adapter's generator operations raise an error during setup
        config = {
            "acquisitions": [{"acq_index": 0, "duration": 100, "channel": 1}],
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": 100.0}}],
        }

        # Simulate an error during generator modulation setup
        # In the new architecture, generator operations are accessed via adapter.generator
        stack.adapter.generator.set_modulation = MagicMock(side_effect=ValueError("Duration must be positive"))

        # run() is a generator - consume it and check the header event
        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test"))
        header = events[0]

        assert isinstance(header, StreamHeader)
        assert header.type == "experiment_header"
        assert not header.metadata["ok"]
        assert "Duration must be positive" in header.metadata["error"]

    def test_partial_config_caching(self, stack: object) -> None:
        """Verify that partial configurations do NOT trigger unnecessary upload/compile
        steps."""
        # 1. Configuration WITHOUT 'envelopes' or 'waves'
        minimal_config = {
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": 100.0}}],
            "acquisitions": [],
            "trigger": {},
        }

        # SPY/MOCK INTERNAL HANDLERS (now return dicts, not Result objects)
        stack.handler.env_h.upload = MagicMock(return_value={})
        stack.handler.wave_h.compile = MagicMock(return_value={})

        # Run experiment - consume the generator
        events = list(stack.handler.run(minimal_config, cmd="run_experiment", session_id="test"))
        header = events[0]

        assert isinstance(header, StreamHeader)
        assert header.metadata["ok"]
        # Ensure upload/compile were NOT called (Optimization check)
        stack.handler.env_h.upload.assert_not_called()
        stack.handler.wave_h.compile.assert_not_called()

    def test_fail_fast_on_compilation_error(self, stack: object) -> None:
        """Verify the 'Fail-Fast' mechanism during the preparation stage."""
        config = {
            "waves": {"0": [{"wave_id": "w1"}]},
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": 100.0}}],
        }

        # Simulate a compilation failure (now raises exception instead of returning Result)
        stack.handler.wave_h.compile = MagicMock(side_effect=WaveCompilationError(0, "w1", "Missing dependency"))

        # MOCK THE ADAPTER METHOD to verify it is NOT called
        stack.adapter.generator_modulation = MagicMock()

        # Run experiment - consume the generator
        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test"))
        header = events[0]

        assert isinstance(header, StreamHeader)
        assert not header.metadata["ok"]
        assert "Missing dependency" in header.metadata["error"]

        # CRITICAL: The hardware setup must be skipped
        stack.adapter.generator_modulation.assert_not_called()

    def test_status_handler_resilience(self, stack: object) -> None:
        """Verify that StatusHandler does not crash if a single generator fails."""

        # MOCK THE ADAPTER.GENERATOR METHODS to inject a Side Effect (Exception)
        def side_effect(gen_index: int) -> list[str]:
            if gen_index == 1:
                raise RuntimeError("FPGA timeout")
            return ["env1", "env2"]

        stack.adapter.generator.get_envelope_names = MagicMock(side_effect=side_effect)

        # Mock other dependencies for the status check
        stack.adapter.generator.get_wave_cache = MagicMock(return_value=[])
        stack.adapter.generator.get_readout_wave_cache = MagicMock(return_value=None)

        # Execute
        statuses = stack.handler.status_h.get_all_generators_status()

        assert len(statuses) == stack.adapter.num_generators

        # Gen 0 should be OK
        assert statuses[0]["ok"] is True

        # Gen 1 should be Error, but handled gracefully (no crash)
        assert statuses[1]["ok"] is False
        assert "FPGA timeout" in statuses[1]["error"]

    def test_reset_preserve_specs_flag(self, stack: object) -> None:
        """Verify reset handler propagates the preserve_specs flag."""
        # MOCK THE ADAPTER.GENERATOR METHOD to verify arguments
        stack.adapter.generator.reset_wave_memory = MagicMock(return_value={})

        # Call reset with preserve_specs=True
        stack.handler.reset_h.reset_waves(gen_index=0, preserve_specs=True)

        # Verify adapter call
        stack.adapter.generator.reset_wave_memory.assert_called_with(gen_index=0, preserve_specs=True)

    def test_sweep_integer_casting_edge_cases(self, stack: object) -> None:
        """Verify strict type casting for discrete hardware parameters."""
        # Config sweeping a discrete parameter (nyquist_zone) with FLOAT values
        msg = {
            "sweep_id": "nz_sweep",
            "sweep_mode": "cartesian",
            "base": {"generators": [{"gen_index": 0, "drive": {"nyquist_zone": "$nz"}}]},
            "variables": [{"name": "nz", "values": [1.0, 2.0]}],  # User provides floats
        }

        # Monkey patch the adapter.generator method receiving the value
        stack.adapter.generator.set_nyquist_zone = MagicMock()

        # Run sweep - consume all items
        list(stack.handler.run_sweep(msg, cmd="run_sweep", session_id="test", stop_check=lambda: False))

        # Verify arguments passed to adapter.generator
        # We expect set_nyquist_zone(gen_index, type, value)
        calls = stack.adapter.generator.set_nyquist_zone.call_args_list
        assert len(calls) > 0

        for call_args in calls:
            # call_args.args[2] is the 'zone' argument
            zone_arg = call_args[0][2]
            assert isinstance(zone_arg, int), f"Nyquist zone {zone_arg} was not cast to int!"
            assert not isinstance(zone_arg, float)

    def test_acquisition_timeout_handling(self, stack: object) -> None:
        """Verify system stability when acquisition times out."""
        config = {
            "acquisitions": [{"acq_index": 0, "duration": 100, "channel": 1}],
            "timeout": 1.0,
        }

        # 1. Simulate a Timeout Exception from the driver
        # Monkey patch run_multi_acquisition to raise TimeoutError (used by _stream_acquisition_only)
        def timeout_side_effect(**kwargs):
            raise TimeoutError("DMA Receive Timeout")

        stack.adapter.acquisition.run_multi_acquisition = MagicMock(side_effect=timeout_side_effect)

        # 2. Run execution - consume the generator
        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test"))

        # 3. Verify Graceful Failure
        # When an exception occurs during acquisition streaming, the run() method catches it
        # and yields a second header with ok=False and the error message
        header_events = [e for e in events if isinstance(e, StreamHeader)]
        assert len(header_events) == 2, f"Expected 2 header events, got {len(header_events)}"

        # First header (setup succeeded)
        success_header = header_events[0]
        assert success_header.metadata["ok"] is True

        # Second header (error during streaming)
        error_header = header_events[1]
        assert error_header.metadata["ok"] is False
        assert "DMA Receive Timeout" in error_header.metadata["error"]

    def test_zipped_sweep_topology(self, stack: object) -> None:
        """Verify 'zipped' sweep mode behavior (Diagonal vs Cartesian).

        **Rationale:** Standard sweeps are Cartesian (all combinations). 'Zipped' sweeps
        proceed point-wise (p1 with p1, p2 with p2). This is critical for simultaneous
        parameter variation (e.g., keeping a ratio constant: freq up AND amp up). This
        test ensures the handler correctly maps the topology and doesn't accidentally
        perform a massive Cartesian grid.
        """
        msg = {
            "sweep_id": "diag_test",
            "sweep_mode": "zipped",
            "base": {
                "generators": [{"gen_index": 0, "drive": {"frequency_mhz": "$f"}}],
                "acquisitions": [{"acq_index": 0, "frequency_mhz": "$g", "duration": 10}],
            },
            "variables": [
                {"name": "f", "values": [10.0, 20.0, 30.0]},
                {"name": "g", "values": [0.1, 0.2, 0.3]},
            ],
        }

        # Monkey patch adapter
        stack.adapter.generator_modulation = MagicMock()
        stack.adapter.prepare_sweep = MagicMock()
        stack.adapter.end_sweep = MagicMock()

        # Run - consume all items
        items = list(stack.handler.run_sweep(msg, cmd="run_sweep", session_id="test", stop_check=lambda: False))

        statuses = [i for i in items if isinstance(i, StreamTiming)]
        assert len(statuses) == 1
        assert statuses[0].metadata["ok"]
        assert statuses[0].metadata["n_points"] == 3  # If Cartesian, this would be 3x3=9. Zipped is 3.

    def test_trigger_delay_propagation(self, stack: object) -> None:
        """Verify that trigger parameters are correctly propagated to the adapter.

        **Rationale:** Trigger timing is complex. The MessageHandler receives high-level
        keys like 'drive_start_index' and must pass them to the adapter's
        'trigger.program_delays'. This test ensures arguments aren't lost or swapped.
        """
        config = {
            "trigger": {
                "drive": {"1": {"delay": [[10, 1]]}},
                "readout": {"2": {"delay": 50}},
                "drive_start_index": 10,
                "shot_duration": 1000,
            },
            "generators": [],
            "acquisitions": [],
        }

        # Mock adapter.trigger methods
        stack.adapter.trigger.program_delays = MagicMock()
        stack.adapter.trigger.set_duration = MagicMock()

        # Run - consume the generator to execute the code
        list(stack.handler.run(config, cmd="run_experiment", session_id="test"))

        # Check Duration
        stack.adapter.trigger.set_duration.assert_called_with(1000)

        # Check Delays
        # Arguments: drive, readout, drive_start_index
        stack.adapter.trigger.program_delays.assert_called_with(
            drive={"1": {"delay": [[10, 1]]}},
            readout={"2": {"delay": 50}},
            drive_start_index=10,
        )

    def test_invalid_hardware_index_handling(self, stack: object) -> None:
        """Verify behavior when user requests a non-existent generator index.

        **Rationale:** If the hardware has 2 generators (indices 0, 1) and the user
        requests index 99, the low-level driver (or list access) will raise an
        IndexError. The handler must catch this and report it as a configuration error,
        not crash the server process.
        """
        config = {"generators": [{"gen_index": 99, "drive": {"frequency_mhz": 100.0}}]}

        # Simulate the adapter crashing due to invalid index
        # Note: Even if MockHardware allows it, we enforce the crash via Mock side_effect
        # to test the handler's reaction to such an event.
        stack.adapter.generator.set_modulation = MagicMock(side_effect=IndexError("Generator 99 out of range"))

        # run() is a generator - consume it
        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test"))
        header = events[0]

        assert isinstance(header, StreamHeader)
        assert header.metadata["ok"] is False
        # The mocked exception message is propagated through the handler
        assert "Generator 99 out of range" in header.metadata["error"]

        # Verify it didn't crash and returned a valid error message
        assert isinstance(header.metadata["error"], str)

    def test_config_log_completeness(self, stack: object) -> None:
        """Verify that the execution log captures key actions for audit.

        **Rationale:** In scientific experiments, data without metadata is useless. The
        'config_log' returned with the results allows researchers to verify exactly what
        was executed (e.g., "Was the drive frequency updated?"). This test ensures the
        log isn't empty.
        """
        config = {
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": 50.0}}],
            "acquisitions": [{"acq_index": 0, "channel": 1}],
            "trigger": {},
        }

        stack.adapter.generator_modulation = MagicMock()
        stack.adapter.acq_trigger2listen = MagicMock()

        # run() is a generator - consume it
        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test"))
        header = events[0]

        assert isinstance(header, StreamHeader)
        assert header.metadata["ok"]
        assert header.metadata["config_log"] is not None

        # Flatten log for search
        log_text = " ".join(header.metadata["config_log"])

        # Verify content reflects the configuration
        assert "gen 0 drive frequency: 50.0 MHz" in log_text
        assert "acq 0 listening to trigger channel 1" in log_text

    def test_sweep_string_substitution(self, stack: object) -> None:
        """Verify that string sweep variables are passed through without validation.

        **Design Note:** Input validation is handled at the TCP layer, not MessageHandler.
        The handler passes values directly to the adapter, which will fail if the value
        is incompatible with the hardware operation. This test verifies the handler
        doesn't crash when receiving string values for sweep variables.
        """
        # Scenario: Sweeping the envelope name referenced by a wave definition
        msg = {
            "sweep_id": "shape_optimization",
            "sweep_mode": "cartesian",
            "base": {"waves": {"0": [{"id": "pulse", "envelope": "$env_name"}]}},
            "variables": [{"name": "env_name", "values": ["gauss_99", "rect_01"]}],
        }

        # Mock dependencies
        stack.adapter.generator.compile_waves = MagicMock(return_value={"waves": [], "replaced": []})
        stack.adapter.prepare_sweep = MagicMock()
        stack.adapter.end_sweep = MagicMock()

        # Run sweep - no validation at MessageHandler level
        items = list(stack.handler.run_sweep(msg, cmd="run_sweep", session_id="test", stop_check=lambda: False))

        # Handler should complete without error - validation is TCP layer responsibility
        statuses = [i for i in items if isinstance(i, StreamTiming)]
        assert len(statuses) == 1
        assert statuses[0].metadata["ok"]
        assert statuses[0].metadata["n_completed"] == 2

    def test_empty_payload_behavior(self, stack: object) -> None:
        """Verify system stability when receiving an empty configuration.

        **Rationale:** This serves as a 'Null Operation' test. If a client sends an
        empty JSON `{}`, the server should essentially do nothing (no hardware
        reconfiguration) and return a success status with no data. It MUST NOT crash due
        to missing keys.
        """
        config = {}

        # Mock methods to ensure they are NOT called
        stack.adapter.generator_modulation = MagicMock()
        stack.adapter.acquisition_timing = MagicMock()

        # Run - consume the generator
        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test"))
        header = events[0]

        # Should be successful (no errors occurred)
        assert isinstance(header, StreamHeader)
        assert header.metadata["ok"]

        # Hardware setup should have been skipped
        stack.adapter.generator_modulation.assert_not_called()
        stack.adapter.acquisition_timing.assert_not_called()

    def test_readout_wave_upload_flow(self, stack: object) -> None:
        """Verify the dedicated path for uploading readout waveforms.

        **Rationale:**
        Readout waveforms (used for matched filtering or specific drive tones) are handled
        via `upload_readout_wave`, distinct from the standard `compile_waves`.
        This test ensures that if a generator config specifies a 'readout.wave',
        the specific adapter method is invoked correctly.
        """
        config = {
            "generators": [
                {
                    "gen_index": 0,
                    "readout": {
                        "frequency_mhz": 50.0,
                        "wave": {"type": "const", "length": 100},
                    },
                }
            ]
        }

        # Mock adapter.generator methods
        stack.adapter.generator.set_modulation = MagicMock()
        stack.adapter.generator.upload_readout_wave = MagicMock()

        # Run - consume the generator
        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test"))
        header = events[0]

        assert isinstance(header, StreamHeader)
        assert header.metadata["ok"]

        # Verify specific call
        stack.adapter.generator.upload_readout_wave.assert_called_with(
            gen_index=0, wave={"type": "const", "length": 100}, replace=True
        )


# =============================================================================
# BINARY ENVELOPE UPLOAD TESTS
# =============================================================================


def test_envelope_handler_binary_input(stack: object) -> None:
    """Test EnvelopeHandler with binary numpy input (new protocol 2.1)."""
    # Create binary envelope data (float32 I/Q pairs)
    samples = np.array([[0.5, 0.3], [0.6, 0.4], [0.7, 0.5]], dtype=np.float32)
    envelope_data = {(0, 0): samples}  # (gen_idx=0, env_idx=0)

    config = {
        "envelopes": {
            "0": [
                {
                    "name": "test_binary_env",
                    "for_interpolation": False,
                    "is_symmetric": False,
                    "i_even": True,
                    "q_even": False,
                    "num_samples": 3,
                }
            ]
        }
    }

    # Call upload with binary data (returns dict on success, raises on failure)
    result = stack.handler.env_h.upload(config, envelope_data)

    # Verify success - result is now a dict, not EnvelopeResult
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"


def test_envelope_handler_binary_multiple_generators(stack: object) -> None:
    """Test binary envelope upload with multiple generators."""
    # Create binary data for multiple generators
    samples_gen0 = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32)
    samples_gen1 = np.array([[0.5, 0.6], [0.7, 0.8]], dtype=np.float32)

    envelope_data = {
        (0, 0): samples_gen0,
        (1, 0): samples_gen1,
    }  # gen 0, env 0  # gen 1, env 0

    config = {
        "envelopes": {
            "0": [
                {
                    "name": "env_gen0",
                    "for_interpolation": False,
                    "is_symmetric": False,
                    "i_even": True,
                    "q_even": False,
                    "num_samples": 2,
                }
            ],
            "1": [
                {
                    "name": "env_gen1",
                    "for_interpolation": False,
                    "is_symmetric": False,
                    "i_even": True,
                    "q_even": False,
                    "num_samples": 2,
                }
            ],
        }
    }

    result = stack.handler.env_h.upload(config, envelope_data)

    # Verify success - result is now a dict
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"


def test_envelope_handler_binary_multiple_envelopes_per_generator(
    stack: object,
) -> None:
    """Test binary upload with multiple envelopes for one generator."""
    # Create binary data for multiple envelopes on same generator
    samples_env0 = np.array([[0.1, 0.2]], dtype=np.float32)
    samples_env1 = np.array([[0.3, 0.4], [0.5, 0.6]], dtype=np.float32)

    envelope_data = {
        (0, 0): samples_env0,
        (0, 1): samples_env1,
    }  # gen 0, env 0  # gen 0, env 1

    config = {
        "envelopes": {
            "0": [
                {
                    "name": "env0",
                    "for_interpolation": False,
                    "is_symmetric": False,
                    "i_even": True,
                    "q_even": False,
                    "num_samples": 1,
                },
                {
                    "name": "env1",
                    "for_interpolation": False,
                    "is_symmetric": False,
                    "i_even": True,
                    "q_even": False,
                    "num_samples": 2,
                },
            ]
        }
    }

    result = stack.handler.env_h.upload(config, envelope_data)

    # Verify success - result is now a dict
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"


def test_envelope_handler_binary_large_samples(stack: object) -> None:
    """Test binary envelope upload with large sample count."""
    # Create 1000-sample envelope to simulate real-world usage
    num_samples = 1000
    samples = np.random.rand(num_samples, 2).astype(np.float32) * 2 - 1  # [-1, 1] range

    envelope_data = {(0, 0): samples}

    config = {
        "envelopes": {
            "0": [
                {
                    "name": "large_envelope",
                    "for_interpolation": True,
                    "is_symmetric": False,
                    "i_even": True,
                    "q_even": False,
                    "num_samples": num_samples,
                }
            ]
        }
    }

    result = stack.handler.env_h.upload(config, envelope_data)

    # Verify success - result is now a dict
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"


# =============================================================================
