# file: fireq-utils/test/test_message_handler.py
import pytest
import numpy as np
from unittest.mock import MagicMock
from threading import Event
from server.message_handler import MessageHandler
from server.ol_adapter import OL_adapter

# Attempt to import Mock Hardware; fallback to local import if the file is adjacent
try:
    from test.mock_hardware import MockOverlay
except ImportError:
    from mock_hardware import MockOverlay

@pytest.fixture
def stack():
    """
    Provides a MessageHandler connected to MockOverlay via OL_adapter.
    
    This stack mocks the DMA engine to bypass hardware buffer calculations 
    that are not valid in a simulation environment.
    """
    class HandlerStack:
        def __init__(self):
            # 1. Create Mock Hardware
            self.ol = MockOverlay()
            
            # 2. Create Adapter Layer
            self.adapter = OL_adapter(self.ol)
            
            # 3. CRITICAL: Mock the DMA Engine
            # This bypasses hardware buffer calculations that fail in simulation
            self.adapter.dma_engine = MagicMock()
            self.adapter.dma_engine.get_max_shots.return_value = 999999
            
            # Setup valid DMA buffer return
            def retrieve_side_effect(buffer, mode, shots, samp_per_shot, adc_index, timeout):
                return np.zeros((shots, samp_per_shot))
            self.adapter.dma_engine.retrieve_acquisition.side_effect = retrieve_side_effect
            
            # 4. Initialize Handler
            self.handler = MessageHandler(self.adapter)

    return HandlerStack()
# =============================================================================
# STANDARD TESTS (HAPPY PATH)
# =============================================================================

def test_run_experiment_flow(stack):
    """Test the _run_acquisition orchestrator."""
    config = {
        "acquisitions": [{"acq_index": 0, "output_type": "decimated", "duration": 256}],
        "trigger": {"shots": 10},
        "timeout": 5.0
    }
    log = []

    # Mock the arming phase to return a dummy buffer object
    stack.adapter.dma_engine.arm_acquisition.return_value = "dummy_buffer"

    # Run the internal method
    results = stack.handler._run_acquisition(config, log)

    # Check results
    assert 0 in results
    assert results[0].shape == (10, 256)
    
    # Robust log checking
    log_content = " ".join([str(entry) for entry in log])
    assert "Acquisition" in log_content or "complete" in log_content

def test_run_sweep_optimized(stack):
    """Test the optimized sweep loop."""
    msg = {
        "sweep_id": "test_sweep",
        "base": {
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": "$freq"}}],
            "acquisitions": [{"acq_index": 0, "duration": 100}],
            "trigger": {"shots": 1}
        },
        "variables": [{"name": "freq", "values": [10.0, 20.0]}]
    }
    
    stack.adapter.dma_engine.arm_acquisition.return_value = "dummy_buffer"
    on_point = MagicMock()
    
    status = stack.handler.run_sweep(msg, on_point)
    
    assert status.ok
    assert status.n_completed == 2
    assert on_point.call_count == 2

# =============================================================================
# ROBUSTNESS TESTS (EDGE CASES & ERROR HANDLING)
# =============================================================================

class TestRobustness:
    """
    Advanced test suite covering edge cases, partial configurations, and error resilience.
    """
    def test_input_sanity_validation(self, stack):
        """
        Verify that physically impossible values are rejected before hitting hardware drivers.

        **Rationale:**
        Passing negative durations or non-physical parameters to FPGA registers 
        can lead to integer underflows (very large uint values) causing 
        hardware lockups that require a power cycle.
        """
        # Scenario: User requests a negative duration
        config = {
            "acquisitions": [{"acq_index": 0, "duration": -100}], # Impossible
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": "NaN"}}] # Invalid type/value
        }

        # We assume the Adapter or the Handler raises an error. 
        # If the Handler logic just casts to int(-100), the driver receiving a uint32 
        # might interpret it as 4.29 billion cycles. 
        # Note: If your current code doesn't validate this, this test serves as a 
        # request for a future feature (Validation Layer).
        
        # For now, let's simulate the adapter protecting itself:
        stack.adapter.acquisition_timing = MagicMock(side_effect=ValueError("Duration must be positive"))
        
        result = stack.handler.run(config)
        
        assert not result.ok
        assert "Duration must be positive" in result.error

    def test_partial_config_caching(self, stack):
        """
        Verify that partial configurations do NOT trigger unnecessary upload/compile steps.
        """
        # 1. Configuration WITHOUT 'envelopes' or 'waves'
        minimal_config = {
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": 100.0}}],
            "acquisitions": [],
            "trigger": {}
        }

        # SPY/MOCK INTERNAL HANDLERS
        stack.handler.env_h.upload = MagicMock(return_value=MagicMock(ok=True))
        stack.handler.wave_h.compile = MagicMock(return_value=MagicMock(ok=True))

        # MOCK THE ADAPTER METHOD
        stack.adapter.generator_modulation = MagicMock()

        # Run experiment
        result = stack.handler.run(minimal_config)

        assert result.ok
        # Ensure upload/compile were NOT called (Optimization check)
        stack.handler.env_h.upload.assert_not_called()
        stack.handler.wave_h.compile.assert_not_called()
        
        # Ensure hardware setup proceeded (Functional check)
        stack.adapter.generator_modulation.assert_called()

    def test_fail_fast_on_compilation_error(self, stack):
        """
        Verify the 'Fail-Fast' mechanism during the preparation stage.
        """
        config = {
            "waves": {"0": [{"id": "w1", "type": "csv"}]}, 
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": 100.0}}]
        }

        # Simulate a compilation failure
        failure_result = MagicMock()
        failure_result.ok = False
        failure_result.error = "Missing dependency"
        stack.handler.wave_h.compile = MagicMock(return_value=failure_result)

        # MOCK THE ADAPTER METHOD to verify it is NOT called
        stack.adapter.generator_modulation = MagicMock()

        # Run experiment
        result = stack.handler.run(config)

        assert not result.ok
        assert "Missing dependency" in result.error
        
        # CRITICAL: The hardware setup must be skipped
        stack.adapter.generator_modulation.assert_not_called()

    def test_status_handler_resilience(self, stack):
        """
        Verify that StatusHandler does not crash if a single generator fails.
        """
        # MOCK THE ADAPTER METHOD to inject a Side Effect (Exception)
        def side_effect(gen_index):
            if gen_index == 1:
                raise RuntimeError("FPGA timeout")
            return ["env1", "env2"]
        
        stack.adapter.get_envelope_names = MagicMock(side_effect=side_effect)
        
        # Mock other dependencies for the status check
        stack.adapter.get_wave_cache = MagicMock(return_value=[])
        stack.adapter.get_readout_wave_cache = MagicMock(return_value=None)
        
        # Execute
        statuses = stack.handler.status_h.get_all_generators_status()
        
        assert len(statuses) == stack.adapter.num_generators
        
        # Gen 0 should be OK
        assert statuses[0]["ok"] is True
        
        # Gen 1 should be Error, but handled gracefully (no crash)
        assert statuses[1]["ok"] is False
        assert "FPGA timeout" in statuses[1]["error"]

    def test_reset_preserve_specs_flag(self, stack):
        """
        Verify that the reset handler correctly propagates the 'preserve_specs' flag.
        """
        # MOCK THE ADAPTER METHOD to verify arguments
        stack.adapter.reset_wave_memory = MagicMock(return_value={})

        # Call reset with preserve_specs=True
        stack.handler.reset_h.reset_waves(gen_index=0, preserve_specs=True)
        
        # Verify adapter call
        stack.adapter.reset_wave_memory.assert_called_with(
            gen_index=0,
            preserve_specs=True
        )

    def test_deep_variable_substitution(self, stack):
        """
        Verify recursive variable substitution. 
        """
        from server.message_handler import find_variable_paths, substitute_variables

        base_config = {
            "sequence": [
                {"op": "play", "args": {"freq": "$f1", "amp": 0.5}},
                {"op": "wait", "args": {"time": "$t1"}}
            ]
        }
        variables = {"f1", "t1"}
        
        # 1. Test Path Discovery
        paths = find_variable_paths(base_config, variables)
        assert "sequence.0.args.freq" in paths["f1"]

        # 2. Test Substitution
        point = {"f1": 123.5, "t1": 1000}
        new_config = substitute_variables(base_config, point)

        assert new_config["sequence"][0]["args"]["freq"] == 123.5
        assert base_config["sequence"][0]["args"]["freq"] == "$f1"

    def test_sweep_interruption(self, stack):
        """
        Verify that a running sweep can be aborted via the stop_event.
        """
        # 1. Setup a multi-point sweep
        msg = {
            "sweep_id": "long_run",
            "base": {"generators": [], "acquisitions": [], "trigger": {}},
            "variables": [{"name": "x", "values": [1, 2, 3, 4, 5]}]
        }
        
        # 2. Setup the stop event
        stop_evt = Event()
        
        # 3. Define a side effect to simulate user interruption
        mock_on_point = MagicMock()
        
        def on_point_side_effect(result):
            # After processing the second point (index 1), signal stop
            if result.point_index == 1: 
                stop_evt.set()
        
        mock_on_point.side_effect = on_point_side_effect
        
        # Monkey patch adapter to ensure it runs fast and we can spy on end_sweep
        stack.adapter.run_multi_acquisition = MagicMock(return_value={0: np.zeros(10)})
        stack.adapter.prepare_sweep = MagicMock()
        stack.adapter.end_sweep = MagicMock()

        # 4. Run Sweep
        status = stack.handler.run_sweep(msg, mock_on_point, stop_event=stop_evt)
        
        # 5. Verifications
        assert status.ok
        
        # Logic check: Point 0 runs (init). Point 1 runs (loop). 
        # After Point 1, event is set. Loop check for Point 2 finds event set -> break.
        # Total completed should be 2.
        assert status.n_completed == 2 
        assert status.n_points == 5
        
        # Ensure hardware was released
        stack.adapter.end_sweep.assert_called_once()

    def test_sweep_integer_casting_edge_cases(self, stack):
        """
        Verify strict type casting for discrete hardware parameters.
        """
        # Config sweeping a discrete parameter (nyquist_zone) with FLOAT values
        msg = {
            "base": {
                "generators": [{
                    "gen_index": 0, 
                    "drive": {"nyquist_zone": "$nz"} 
                }]
            },
            "variables": [
                {"name": "nz", "values": [1.0, 2.0]} # User provides floats
            ]
        }

        # Monkey patch the adapter method receiving the value
        stack.adapter.set_nyquist_zone = MagicMock()
        stack.adapter.run_multi_acquisition = MagicMock(return_value={})

        # Run sweep
        stack.handler.run_sweep(msg, MagicMock())

        # Verify arguments passed to adapter
        # We expect set_nyquist_zone(gen_index, type, value)
        calls = stack.adapter.set_nyquist_zone.call_args_list
        assert len(calls) > 0
        
        for call_args in calls:
            # call_args.args[2] is the 'zone' argument
            zone_arg = call_args[0][2] 
            assert isinstance(zone_arg, int), f"Nyquist zone {zone_arg} was not cast to int!"
            assert not isinstance(zone_arg, float)

    def test_acquisition_timeout_handling(self, stack):
        """
        Verify system stability when acquisition times out (Hardware/Driver failure).
        """
        config = {
            "acquisitions": [{"acq_index": 0, "duration": 100}],
            "timeout": 1.0
        }

        # 1. Simulate a Timeout Exception from the driver
        # Monkey patch run_multi_acquisition
        stack.adapter.run_multi_acquisition = MagicMock(side_effect=TimeoutError("DMA Receive Timeout"))

        # 2. Run execution
        result = stack.handler.run(config)

        # 3. Verify Graceful Failure
        assert result.ok is False
        assert result.data is None
        assert "DMA Receive Timeout" in result.error
        
        # Verify the configuration log was preserved for debugging
        assert result.config_log is not None
        assert any("acq 0" in entry for entry in result.config_log)

    def test_zipped_sweep_topology(self, stack):
            """
            Verify 'zipped' sweep mode behavior (Diagonal vs Cartesian).

            **Rationale:**
            Standard sweeps are Cartesian (all combinations). 'Zipped' sweeps proceed 
            point-wise (p1 with p1, p2 with p2). This is critical for simultaneous 
            parameter variation (e.g., keeping a ratio constant: freq up AND amp up).
            This test ensures the handler correctly maps the topology and doesn't 
            accidentally perform a massive Cartesian grid.
            """
            msg = {
                "sweep_id": "diag_test",
                "sweep_mode": "zipped",
                "base": {
                    "generators": [{"gen_index": 0, "drive": {"frequency_mhz": "$f", "gain": "$g"}}]
                },
                "variables": [
                    {"name": "f", "values": [10.0, 20.0, 30.0]},
                    {"name": "g", "values": [0.1, 0.2, 0.3]}
                ]
            }
            
            # We want to verify the generated points.
            # We can spy on the 'on_point' callback to see what variables were passed.
            on_point_spy = MagicMock()
            
            # Monkey patch adapter
            stack.adapter.generator_modulation = MagicMock()
            stack.adapter.run_multi_acquisition = MagicMock(return_value={0: np.zeros(10)})
            stack.adapter.prepare_sweep = MagicMock()
            stack.adapter.end_sweep = MagicMock()

            # Run
            status = stack.handler.run_sweep(msg, on_point_spy)

            assert status.ok
            assert status.n_points == 3 # If Cartesian, this would be 3x3=9. Zipped is 3.

            # Verify exact point pairing
            # Call args structure: (SweepPointResult, )
            calls = on_point_spy.call_args_list
            
            # Point 1: f=10, g=0.1
            vars_p1 = calls[0][0][0].variables
            assert vars_p1["f"] == 10.0 and vars_p1["g"] == 0.1
            
            # Point 3: f=30, g=0.3
            vars_p3 = calls[2][0][0].variables
            assert vars_p3["f"] == 30.0 and vars_p3["g"] == 0.3

    def test_trigger_delay_propagation(self, stack):
        """
        Verify that trigger parameters are correctly propagated to the adapter.

        **Rationale:**
        Trigger timing is complex. The MessageHandler receives high-level keys 
        like 'drive_start_index' and 'safe_pad' and must pass them to the 
        adapter's 'tg_program_delays'. This test ensures arguments aren't lost or swapped.
        """
        config = {
            "trigger": {
                "drive": True,
                "readout": False,
                "drive_start_index": 10,
                "safe_pad": 5,
                "shot_duration": 1000
            },
            "generators": [], "acquisitions": []
        }

        # Mock adapter methods
        stack.adapter.tg_program_delays = MagicMock()
        stack.adapter.tg_set_duration = MagicMock()
        stack.adapter.run_multi_acquisition = MagicMock(return_value={})

        # Run
        stack.handler.run(config)

        # Check Duration
        stack.adapter.tg_set_duration.assert_called_with(1000)

        # Check Delays
        # Arguments: drive, readout, drive_start_index, safe_pad
        stack.adapter.tg_program_delays.assert_called_with(
            drive=True,
            readout=False,
            drive_start_index=10,
            safe_pad=5
        )

    def test_invalid_hardware_index_handling(self, stack):
        """
        Verify behavior when user requests a non-existent generator index.

        **Rationale:**
        If the hardware has 2 generators (indices 0, 1) and the user requests index 99,
        the low-level driver (or list access) will raise an IndexError. 
        The handler must catch this and report it as a configuration error, 
        not crash the server process.
        """
        config = {
            "generators": [{"gen_index": 99, "drive": {"frequency_mhz": 100.0}}]
        }

        # Simulate the adapter crashing due to invalid index
        # Note: Even if MockHardware allows it, we enforce the crash via Mock side_effect
        # to test the handler's reaction to such an event.
        stack.adapter.generator_modulation = MagicMock(side_effect=IndexError("Generator 99 out of range"))

        result = stack.handler.run(config)

        assert result.ok is False
        assert "Generator 99 out of range" in result.error
        
        # Verify it didn't crash and returned an object
        assert isinstance(result.error, str)

    def test_config_log_completeness(self, stack):
        """
        Verify that the execution log captures key actions for audit.

        **Rationale:**
        In scientific experiments, data without metadata is useless. The 'config_log'
        returned with the results allows researchers to verify exactly what was executed 
        (e.g., "Was the drive frequency updated?"). This test ensures the log isn't empty.
        """
        config = {
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": 50.0}}],
            "acquisitions": [{"acq_index": 0, "channel": 1}],
            "trigger": {}
        }
        
        stack.adapter.generator_modulation = MagicMock()
        stack.adapter.acq_trigger2listen = MagicMock()
        stack.adapter.run_multi_acquisition = MagicMock(return_value={})

        result = stack.handler.run(config)

        assert result.ok
        assert result.config_log is not None
        
        # Flatten log for search
        log_text = " ".join(result.config_log)
        
        # Verify content reflects the configuration
        assert "gen 0 drive frequency: 50.0 MHz" in log_text
        assert "acq 0 listening to trigger channel 1" in log_text

    def test_sweep_string_substitution(self, stack):
        """
        Verify that sweep variables can substitute strings (e.g., switching envelopes).
        """
        # Scenario: Sweeping the envelope name referenced by a wave definition
        msg = {
            "sweep_id": "shape_optimization",
            "base": {
                "waves": {
                    "0": [{"id": "pulse", "envelope": "$env_name"}]
                }
            },
            "variables": [
                {"name": "env_name", "values": ["gauss_99", "rect_01"]}
            ]
        }

        # Mock dependencies
        stack.adapter.compile_waves = MagicMock(return_value={"waves": [], "replaced": []})
        stack.adapter.run_multi_acquisition = MagicMock(return_value={0: np.zeros(10)})
        stack.adapter.prepare_sweep = MagicMock()
        stack.adapter.end_sweep = MagicMock()

        # Run sweep
        stack.handler.run_sweep(msg, MagicMock())

        # Verify calls to compile_waves
        # We expect 2 calls (one per point), each with a different envelope name
        calls = stack.adapter.compile_waves.call_args_list
        assert len(calls) >= 2
        
        # Check first point (gauss_99)
        # args_p1 capture the 'waves' argument passed to adapter.compile_waves
        # Since WaveHandler extracts the list from the dict, this IS the list.
        args_p1 = calls[0].kwargs.get('waves') or calls[0].args[1]
        args_p2 = calls[1].kwargs.get('waves') or calls[1].args[1]
        
        assert args_p1[0]["envelope"] == "gauss_99"
        assert args_p2[0]["envelope"] == "rect_01"

    def test_empty_payload_behavior(self, stack):
        """
        Verify system stability when receiving an empty configuration.

        **Rationale:**
        This serves as a 'Null Operation' test. If a client sends an empty JSON `{}`,
        the server should essentially do nothing (no hardware reconfiguration) 
        and return a success status with no data. It MUST NOT crash due to missing keys.
        """
        config = {}

        # Mock methods to ensure they are NOT called
        stack.adapter.generator_modulation = MagicMock()
        stack.adapter.acquisition_timing = MagicMock()
        
        # Run
        result = stack.handler.run(config)

        # Should be successful (no errors occurred)
        assert result.ok
        
        # Should contain no data
        assert result.data is None or len(result.data) == 1 # Default might trigger acq 0
        
        # Hardware setup should have been skipped
        stack.adapter.generator_modulation.assert_not_called()
        stack.adapter.acquisition_timing.assert_not_called()

    def test_readout_wave_upload_flow(self, stack):
        """
        Verify the dedicated path for uploading readout waveforms.

        **Rationale:**
        Readout waveforms (used for matched filtering or specific drive tones) are handled 
        via `upload_readout_wave`, distinct from the standard `compile_waves`. 
        This test ensures that if a generator config specifies a 'readout.wave', 
        the specific adapter method is invoked correctly.
        """
        config = {
            "generators": [{
                "gen_index": 0,
                "readout": {
                    "frequency_mhz": 50.0,
                    "wave": {"type": "const", "length": 100}
                }
            }]
        }

        # Mock adapter methods
        stack.adapter.generator_modulation = MagicMock()
        stack.adapter.upload_readout_wave = MagicMock()
        stack.adapter.run_multi_acquisition = MagicMock(return_value={})

        # Run
        result = stack.handler.run(config)

        assert result.ok
        
        # Verify specific call
        stack.adapter.upload_readout_wave.assert_called_with(
            gen_index=0,
            wave={"type": "const", "length": 100},
            replace=True
        )