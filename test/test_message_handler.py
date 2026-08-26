# file: fireq-utils/test/test_message_handler.py
from unittest.mock import MagicMock

import numpy as np
import pytest

from FIREQ_SERVER import MessageHandler, OverlayAdapter, WaveCompilationError
from FIREQ_SERVER.execution.sweep_updates import SweepUpdateApplier, ValueTracker
from FIREQ_SERVER.hardware.dma_engine import DMAResult
from FIREQ_SERVER.models import BinaryChunk, StreamHeader, StreamTiming

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

            # Setup valid DMA buffer return as DMAResult
            def retrieve_side_effect(buffer: object) -> DMAResult:
                # Return compact I/Q format (structured array with dummy data)
                dtype = np.dtype([("i", "<i2"), ("q", "<i2")])
                data = np.zeros((10, 256), dtype=dtype)
                return DMAResult(data, 0.001, 0.0001)

            self.adapter.dma_engine.retrieve_acquisition.side_effect = retrieve_side_effect

            # Mock run_acquisition method on adapter
            # This is used by _stream_acquisition_only in MessageHandler
            def run_acquisition_side_effect(
                acq_ip_indices: list,
                mode: str,
                shots: int,
                samp_per_shot: int,
                timeout: float | None = None,
            ) -> tuple:
                """Return data dict and timing info (fpga_wait_s, dma_overhead_s)."""
                result = {}
                for acq_ip in acq_ip_indices:
                    if mode == "accumulated":
                        dtype = np.dtype([("i", "<i4"), ("q", "<i4")])
                        result[acq_ip] = np.zeros(shots, dtype=dtype)
                    else:
                        dtype = np.dtype([("i", "<i2"), ("q", "<i2")])
                        result[acq_ip] = np.zeros((shots, samp_per_shot), dtype=dtype)
                return result, 0.001, 0.0001

            self.adapter.run_acquisition = MagicMock(side_effect=run_acquisition_side_effect)

            # Mock run_multi_acquisition - generator that yields data_dict only
            def run_multi_acquisition_side_effect(
                *,
                acq_ip_indices: list,
                mode: str,
                shots: int,
                samp_per_shot: int,
                timeout: float | None = None,
                validate_chunk: bool = True,
            ):
                """Generator that yields data_dict."""
                result = {}
                for acq_ip in acq_ip_indices:
                    if mode == "accumulated":
                        dtype = np.dtype([("i", "<i4"), ("q", "<i4")])
                        result[acq_ip] = np.zeros(shots, dtype=dtype)
                    else:
                        dtype = np.dtype([("i", "<i2"), ("q", "<i2")])
                        result[acq_ip] = np.zeros((shots, samp_per_shot), dtype=dtype)
                # Update timing stats on acquisition ops (simulating what the real method does)
                self.adapter.acquisition._last_timing_stats = {
                    "total_ms": 1.0,
                    "fpga_wait_ms": 0.5,
                    "dma_overhead_ms": 0.1,
                    "sw_overhead_ms": 0.35,
                }
                yield result

            self.adapter.run_multi_acquisition = MagicMock(side_effect=run_multi_acquisition_side_effect)

            # Mock compute_max_hw_shots on AcquisitionOps to return a high limit (no chunking)
            self.adapter.acquisition.compute_max_hw_shots = MagicMock(return_value=999999)

            # 4. Initialize Handler
            self.handler = MessageHandler(self.adapter)

            # 5. Direct access to sweep update applier (for characterization tests)
            self.sweep_update_applier = SweepUpdateApplier(self.adapter, self.handler.wave_h)

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

    # Check that binary data chunks were produced (if any AcqIP was active)
    chunk_events = [e for e in events if isinstance(e, BinaryChunk)]
    if chunk_events:
        # Verify chunk structure (binary-only, no JSON metadata)
        chunk = chunk_events[0]
        assert chunk.binary_data is not None
        assert chunk.timing is not None
        assert 0 in chunk.binary_data  # AcqIP index 0 should be present


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


class TestExecutionCharacterization:
    """Characterization coverage for execution behavior before refactoring."""

    def test_run_reports_chunk_count_and_stream_metadata(self, stack: object) -> None:
        """Verify ``run()`` header metadata matches chunked acquisition behavior."""
        config = {
            "acquisitions": [{"acq_index": 0, "output_type": "decimated", "duration": 16, "channel": 1}],
            "trigger": {"shots": 10},
            "timeout": 2.5,
        }

        chunk_shape = np.dtype([("i", "<i2"), ("q", "<i2")])
        stream_calls: list[dict[str, object]] = []

        def run_multi_acquisition_side_effect(
            *,
            acq_indices: list[int],
            mode: str,
            shots: int,
            samp_per_shot: int,
            timeout: float | None = None,
            validate_chunk: bool = True,
        ):
            stream_calls.append(
                {
                    "acq_indices": list(acq_indices),
                    "mode": mode,
                    "shots": shots,
                    "samp_per_shot": samp_per_shot,
                    "timeout": timeout,
                    "validate_chunk": validate_chunk,
                }
            )

            for chunk_index in range(3):
                stack.adapter.acquisition._last_timing_stats = {
                    "total_ms": 1.0 + chunk_index,
                    "fpga_wait_ms": 0.5 + chunk_index,
                    "dma_overhead_ms": 0.1,
                    "sw_overhead_ms": 0.35,
                }
                yield {0: np.zeros((4, samp_per_shot), dtype=chunk_shape)}

        stack.adapter.acquisition.compute_max_hw_shots = MagicMock(return_value=4)
        stack.adapter.acquisition.run_multi_acquisition = MagicMock(side_effect=run_multi_acquisition_side_effect)

        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test_session"))

        header = next(event for event in events if isinstance(event, StreamHeader))
        binary_chunks = [event for event in events if isinstance(event, BinaryChunk)]
        timing = next(event for event in events if isinstance(event, StreamTiming))

        assert header.metadata["ok"] is True
        assert header.metadata["n_chunks"] == 3
        assert header.metadata["acq_ip_metadata"] == {0: {"dtype": "iq_int16", "shape": [10, 16]}}
        assert len(binary_chunks) == 3
        assert all(chunk.type == "experiment_binary_chunk" for chunk in binary_chunks)
        assert timing.metadata["debug_timing"]["fpga_wait_ms"] == pytest.approx(2.5)
        assert stream_calls == [
            {
                "acq_indices": [0],
                "mode": "decimated",
                "shots": 10,
                "samp_per_shot": 16,
                "timeout": 2.5,
                "validate_chunk": True,
            }
        ]

    def test_run_sweep_reports_chunked_points_and_validation_modes(self, stack: object) -> None:
        """Verify ``run_sweep()`` reports per-point chunking and validation behavior."""
        msg = {
            "sweep_id": "chunked_sweep",
            "sweep_mode": "cartesian",
            "base": {
                "generators": [{"gen_index": 0, "drive": {"frequency_mhz": "$freq"}}],
                "acquisitions": [{"acq_index": 0, "channel": 1, "duration": 16}],
                "trigger": {"shots": 10},
            },
            "variables": [{"name": "freq", "values": [10.0, 20.0]}],
        }

        chunk_shape = np.dtype([("i", "<i2"), ("q", "<i2")])
        validation_modes: list[bool] = []

        def run_multi_acquisition_side_effect(
            *,
            acq_indices: list[int],
            mode: str,
            shots: int,
            samp_per_shot: int,
            timeout: float | None = None,
            validate_chunk: bool = True,
        ):
            validation_modes.append(validate_chunk)
            for _ in range(3):
                stack.adapter.acquisition._last_timing_stats = {
                    "total_ms": 1.0,
                    "fpga_wait_ms": 0.5,
                    "dma_overhead_ms": 0.1,
                    "sw_overhead_ms": 0.35,
                }
                yield {0: np.zeros((4, samp_per_shot), dtype=chunk_shape)}

        stack.adapter.acquisition.compute_max_hw_shots = MagicMock(return_value=4)
        stack.adapter.acquisition.run_multi_acquisition = MagicMock(side_effect=run_multi_acquisition_side_effect)
        stack.adapter.acquisition.prepare_sweep = MagicMock()
        stack.adapter.acquisition.end_sweep = MagicMock()

        items = list(stack.handler.run_sweep(msg, cmd="run_sweep", session_id="test_session", stop_check=lambda: False))

        header = next(item for item in items if isinstance(item, StreamHeader))
        binary_chunks = [item for item in items if isinstance(item, BinaryChunk)]
        status = next(item for item in items if isinstance(item, StreamTiming))

        assert header.metadata["chunks_per_point"] == 3
        assert header.metadata["n_points"] == 2
        assert header.metadata["acq_ip_metadata"] == {0: {"dtype": "iq_int16", "shape": [10, 16]}}
        assert len(binary_chunks) == 6
        assert validation_modes == [True, False]
        assert status.metadata["ok"] is True
        assert status.metadata["n_completed"] == 2
        stack.adapter.acquisition.prepare_sweep.assert_called_once_with("decimated", [0])
        stack.adapter.acquisition.end_sweep.assert_called_once_with()

    def test_run_sweep_stop_check_reports_partial_completion(self, stack: object) -> None:
        """Verify ``run_sweep()`` stops between points and still finalizes cleanly."""
        msg = {
            "sweep_id": "stopped_sweep",
            "sweep_mode": "cartesian",
            "base": {
                "generators": [{"gen_index": 0, "drive": {"frequency_mhz": "$freq"}}],
                "acquisitions": [{"acq_index": 0, "channel": 1, "duration": 16}],
                "trigger": {"shots": 2},
            },
            "variables": [{"name": "freq", "values": [10.0, 20.0, 30.0]}],
        }

        chunk_shape = np.dtype([("i", "<i2"), ("q", "<i2")])
        validation_modes: list[bool] = []
        stop_state = {"calls": 0}

        def stop_check() -> bool:
            stop_state["calls"] += 1
            return stop_state["calls"] == 1

        def run_multi_acquisition_side_effect(
            *,
            acq_indices: list[int],
            mode: str,
            shots: int,
            samp_per_shot: int,
            timeout: float | None = None,
            validate_chunk: bool = True,
        ):
            validation_modes.append(validate_chunk)
            stack.adapter.acquisition._last_timing_stats = {
                "total_ms": 1.0,
                "fpga_wait_ms": 0.5,
                "dma_overhead_ms": 0.1,
                "sw_overhead_ms": 0.35,
            }
            yield {0: np.zeros((shots, samp_per_shot), dtype=chunk_shape)}

        stack.adapter.acquisition.run_multi_acquisition = MagicMock(side_effect=run_multi_acquisition_side_effect)
        stack.adapter.acquisition.prepare_sweep = MagicMock()
        stack.adapter.acquisition.end_sweep = MagicMock()

        items = list(stack.handler.run_sweep(msg, cmd="run_sweep", session_id="test_session", stop_check=stop_check))

        binary_chunks = [item for item in items if isinstance(item, BinaryChunk)]
        status = next(item for item in items if isinstance(item, StreamTiming))

        assert len(binary_chunks) == 1
        assert validation_modes == [True]
        assert status.metadata["ok"] is True
        assert status.metadata["n_points"] == 3
        assert status.metadata["n_completed"] == 1
        stack.adapter.acquisition.prepare_sweep.assert_called_once_with("decimated", [0])
        stack.adapter.acquisition.end_sweep.assert_called_once_with()

    def test_run_sweep_fast_path_updates_only_flagged_generator_fields(self, stack: object) -> None:
        """Verify generator fast-path updates reapply only the swept field."""
        msg = {
            "sweep_id": "generator_fast_path",
            "sweep_mode": "cartesian",
            "base": {
                "generators": [
                    {
                        "gen_index": 0,
                        "drive": {
                            "frequency_mhz": "$freq",
                            "nyquist_zone": 2,
                            "channel": 3,
                        },
                    }
                ]
            },
            "variables": [{"name": "freq", "values": [10.0, 20.0, 30.0]}],
        }

        stack.adapter.generator.set_modulation = MagicMock()
        stack.adapter.generator.set_nyquist_zone = MagicMock()
        stack.adapter.generator.set_trigger_listener = MagicMock()
        stack.adapter.acquisition.end_sweep = MagicMock()

        items = list(stack.handler.run_sweep(msg, cmd="run_sweep", session_id="test_session", stop_check=lambda: False))

        status = next(item for item in items if isinstance(item, StreamTiming))
        modulation_frequencies = [
            call_args.args[2]["frequency_mhz"] for call_args in stack.adapter.generator.set_modulation.call_args_list
        ]

        assert status.metadata["ok"] is True
        assert status.metadata["n_completed"] == 3
        assert modulation_frequencies == [10.0, 20.0, 30.0]
        assert stack.adapter.generator.set_nyquist_zone.call_count == 1
        assert stack.adapter.generator.set_trigger_listener.call_count == 1

    def test_run_sweep_single_point_skips_prepare_and_end_sweep(self, stack: object) -> None:
        """Verify single-point sweeps do not enter fast-path lifecycle calls."""
        msg = {
            "sweep_id": "single_point",
            "sweep_mode": "cartesian",
            "base": {
                "generators": [{"gen_index": 0, "drive": {"frequency_mhz": "$freq"}}],
                "acquisitions": [{"acq_index": 0, "channel": 1, "duration": 16}],
                "trigger": {"shots": 2},
            },
            "variables": [{"name": "freq", "values": [10.0]}],
        }

        chunk_shape = np.dtype([("i", "<i2"), ("q", "<i2")])

        def run_multi_acquisition_side_effect(
            *,
            acq_indices: list[int],
            mode: str,
            shots: int,
            samp_per_shot: int,
            timeout: float | None = None,
            validate_chunk: bool = True,
        ):
            stack.adapter.acquisition._last_timing_stats = {
                "total_ms": 1.0,
                "fpga_wait_ms": 0.5,
                "dma_overhead_ms": 0.1,
                "sw_overhead_ms": 0.35,
            }
            yield {0: np.zeros((shots, samp_per_shot), dtype=chunk_shape)}

        stack.adapter.acquisition.run_multi_acquisition = MagicMock(side_effect=run_multi_acquisition_side_effect)
        stack.adapter.acquisition.prepare_sweep = MagicMock()
        stack.adapter.acquisition.end_sweep = MagicMock()

        items = list(stack.handler.run_sweep(msg, cmd="run_sweep", session_id="test_session", stop_check=lambda: False))

        status = next(item for item in items if isinstance(item, StreamTiming))

        assert status.metadata["ok"] is True
        assert status.metadata["n_points"] == 1
        assert status.metadata["n_completed"] == 1
        stack.adapter.acquisition.prepare_sweep.assert_not_called()
        stack.adapter.acquisition.end_sweep.assert_not_called()

    def test_run_sweep_failure_after_prepare_still_ends_sweep(self, stack: object) -> None:
        """Verify prepared sweeps always call ``end_sweep()`` even after loop failure."""
        msg = {
            "sweep_id": "prepare_failure",
            "sweep_mode": "cartesian",
            "base": {
                "generators": [{"gen_index": 0, "drive": {"frequency_mhz": "$freq"}}],
                "acquisitions": [{"acq_index": 0, "channel": 1, "duration": 16}],
                "trigger": {"shots": 2},
            },
            "variables": [{"name": "freq", "values": [10.0, 20.0]}],
        }

        chunk_shape = np.dtype([("i", "<i2"), ("q", "<i2")])
        call_count = {"value": 0}

        def run_multi_acquisition_side_effect(
            *,
            acq_indices: list[int],
            mode: str,
            shots: int,
            samp_per_shot: int,
            timeout: float | None = None,
            validate_chunk: bool = True,
        ):
            call_count["value"] += 1
            if call_count["value"] == 2:
                raise RuntimeError("Injected sweep failure")

            stack.adapter.acquisition._last_timing_stats = {
                "total_ms": 1.0,
                "fpga_wait_ms": 0.5,
                "dma_overhead_ms": 0.1,
                "sw_overhead_ms": 0.35,
            }
            yield {0: np.zeros((shots, samp_per_shot), dtype=chunk_shape)}

        stack.adapter.acquisition.run_multi_acquisition = MagicMock(side_effect=run_multi_acquisition_side_effect)
        stack.adapter.acquisition.prepare_sweep = MagicMock()
        stack.adapter.acquisition.end_sweep = MagicMock()

        items = list(stack.handler.run_sweep(msg, cmd="run_sweep", session_id="test_session", stop_check=lambda: False))

        status = next(item for item in items if isinstance(item, StreamTiming))

        assert status.metadata["ok"] is False
        assert status.metadata["n_points"] == 2
        assert status.metadata["n_completed"] == 1
        assert "Injected sweep failure" in status.metadata["error"]
        stack.adapter.acquisition.prepare_sweep.assert_called_once_with("decimated", [0])
        stack.adapter.acquisition.end_sweep.assert_called_once_with()


class TestSweepUpdateCharacterization:
    """Direct characterization coverage for ``SweepUpdateApplier.apply()``."""

    def test_apply_sweep_updates_recompiles_waves_only_on_change(self, stack: object) -> None:
        """Verify wave recompilation is skipped when the wave section is unchanged."""
        tracker = ValueTracker()
        config = {
            "waves": {
                "0": [
                    {
                        "wave_id": "pulse",
                        "kind": "const",
                        "gain": 0.5,
                        "duration": 32,
                    }
                ]
            }
        }
        flags = {
            "generators": {},
            "acquisitions": {},
            "trigger": set(),
            "waves": {"waves_compile"},
        }

        stack.handler.wave_h.compile = MagicMock()

        stack.sweep_update_applier.apply(config, flags, tracker)
        stack.sweep_update_applier.apply(config, flags, tracker)
        config["waves"]["0"][0]["gain"] = 0.75
        stack.sweep_update_applier.apply(config, flags, tracker)

        assert stack.handler.wave_h.compile.call_count == 2

    def test_apply_sweep_updates_reapplies_acquisition_fields_only_when_changed(self, stack: object) -> None:
        """Verify acquisition fast-path updates are driven by flags and value changes."""
        tracker = ValueTracker()
        config = {
            "acquisitions": [
                {
                    "acq_index": 0,
                    "frequency_mhz": 50.0,
                    "phase": 0.25,
                    "channel": 2,
                    "tof": 12,
                    "duration": 128,
                }
            ]
        }
        flags = {
            "generators": {},
            "acquisitions": {0: {"acq_mod", "acq_channel", "acq_duration", "acq_tof"}},
            "trigger": set(),
            "waves": set(),
        }

        stack.adapter.acquisition.set_modulation = MagicMock()
        stack.adapter.acquisition.set_trigger_listener = MagicMock()
        stack.adapter.acquisition.set_timing = MagicMock()

        stack.sweep_update_applier.apply(config, flags, tracker)
        stack.sweep_update_applier.apply(config, flags, tracker)

        config["acquisitions"][0]["phase"] = 0.5
        config["acquisitions"][0]["channel"] = 4
        config["acquisitions"][0]["tof"] = 20
        config["acquisitions"][0]["duration"] = 256
        stack.sweep_update_applier.apply(config, flags, tracker)

        assert stack.adapter.acquisition.set_modulation.call_args_list == [
            ((0, {"frequency_mhz": 50.0, "phase": 0.25}),),
            ((0, {"frequency_mhz": 50.0, "phase": 0.5}),),
        ]
        assert stack.adapter.acquisition.set_trigger_listener.call_args_list == [
            ((0, {"channel": 2}),),
            ((0, {"channel": 4}),),
        ]
        assert stack.adapter.acquisition.set_timing.call_args_list == [
            ((0,), {"tof": 12, "duration": 128}),
            ((0,), {"tof": 20, "duration": 256}),
        ]

    def test_apply_sweep_updates_reapplies_trigger_fields_only_when_changed(self, stack: object) -> None:
        """Verify trigger fast-path updates are skipped until trigger values change."""
        tracker = ValueTracker()
        config = {
            "trigger": {
                "shot_duration": 1000,
                "drive": {"1": {"delay": [[10, 0], [20, 1]]}},
                "readout": {"2": {"delay": 50}},
                "drive_start_index": 3,
            }
        }
        flags = {
            "generators": {},
            "acquisitions": {},
            "trigger": {"trig_shot_duration", "trig_drive", "trig_readout"},
            "waves": set(),
        }

        stack.adapter.trigger.set_duration = MagicMock()
        stack.adapter.trigger.program_delays = MagicMock()

        stack.sweep_update_applier.apply(config, flags, tracker)
        stack.sweep_update_applier.apply(config, flags, tracker)

        config["trigger"]["shot_duration"] = 1200
        config["trigger"]["drive_start_index"] = 5
        stack.sweep_update_applier.apply(config, flags, tracker)

        assert stack.adapter.trigger.set_duration.call_args_list == [
            ((1000,),),
            ((1200,),),
        ]
        assert stack.adapter.trigger.program_delays.call_args_list == [
            (
                (),
                {
                    "drive": {"1": {"delay": [[10, 0], [20, 1]]}},
                    "readout": {"2": {"delay": 50}},
                    "drive_start_index": 3,
                },
            ),
            (
                (),
                {
                    "drive": {"1": {"delay": [[10, 0], [20, 1]]}},
                    "readout": {"2": {"delay": 50}},
                    "drive_start_index": 5,
                },
            ),
        ]

    def test_apply_sweep_updates_is_noop_when_flags_are_empty(self, stack: object) -> None:
        """Verify empty flags do not trigger any hardware update calls."""
        tracker = ValueTracker()
        config = {
            "waves": {"0": [{"wave_id": "pulse", "kind": "const"}]},
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": 10.0}}],
            "acquisitions": [{"acq_index": 0, "frequency_mhz": 20.0, "channel": 1, "duration": 64}],
            "trigger": {
                "shot_duration": 1000,
                "drive": {"1": {"delay": [[10, 0]]}},
                "drive_start_index": 1,
            },
        }
        flags = {
            "generators": {},
            "acquisitions": {},
            "trigger": set(),
            "waves": set(),
        }

        stack.handler.wave_h.compile = MagicMock()
        stack.adapter.generator.set_modulation = MagicMock()
        stack.adapter.generator.set_nyquist_zone = MagicMock()
        stack.adapter.generator.set_trigger_listener = MagicMock()
        stack.adapter.generator.program_drive_sequence = MagicMock()
        stack.adapter.generator.upload_readout_wave = MagicMock()
        stack.adapter.acquisition.set_modulation = MagicMock()
        stack.adapter.acquisition.set_trigger_listener = MagicMock()
        stack.adapter.acquisition.set_timing = MagicMock()
        stack.adapter.trigger.set_duration = MagicMock()
        stack.adapter.trigger.program_delays = MagicMock()

        stack.sweep_update_applier.apply(config, flags, tracker)

        stack.handler.wave_h.compile.assert_not_called()
        stack.adapter.generator.set_modulation.assert_not_called()
        stack.adapter.generator.set_nyquist_zone.assert_not_called()
        stack.adapter.generator.set_trigger_listener.assert_not_called()
        stack.adapter.generator.program_drive_sequence.assert_not_called()
        stack.adapter.generator.upload_readout_wave.assert_not_called()
        stack.adapter.acquisition.set_modulation.assert_not_called()
        stack.adapter.acquisition.set_trigger_listener.assert_not_called()
        stack.adapter.acquisition.set_timing.assert_not_called()
        stack.adapter.trigger.set_duration.assert_not_called()
        stack.adapter.trigger.program_delays.assert_not_called()


class TestHardwareConfigCharacterization:
    """Characterization coverage for the full hardware configuration path."""

    def test_run_applies_full_config_in_expected_order(self, stack: object) -> None:
        """Verify full config setup preserves the current sequencing contract."""
        events: list[str] = []
        config = {
            "envelopes": {"0": [{"name": "env0", "num_samples": 1}]},
            "waves": {"0": [{"wave_id": "w0"}]},
            "generators": [{"gen_index": 0, "drive": {"frequency_mhz": 50.0}}],
            "acquisitions": [{"acq_index": 0, "channel": 4, "duration": 64}],
            "trigger": {"shot_duration": 1000, "drive": {"1": {"delay": [[10, 0]]}}},
        }

        original_gen_mod = stack.adapter.generator.set_modulation
        original_acq_listener = stack.adapter.acquisition.set_trigger_listener
        original_acq_timing = stack.adapter.acquisition.set_timing

        stack.handler.env_h.upload = MagicMock(side_effect=lambda cfg: events.append("upload_envelopes") or {})
        stack.handler.wave_h.compile = MagicMock(side_effect=lambda cfg: events.append("compile_waves") or {})

        def record_generator_modulation(gen_index: int, signal_kind: str, mod_cfg: dict) -> object:
            events.append(f"generator_modulation_{gen_index}_{signal_kind}")
            return original_gen_mod(gen_index, signal_kind, mod_cfg)

        def record_acquisition_listener(acq_index: int, listener_cfg: dict) -> object:
            events.append(f"acquisition_channel_{acq_index}_{int(listener_cfg['channel'])}")
            return original_acq_listener(acq_index, listener_cfg)

        def record_acquisition_timing(acq_index: int, *, tof: int, duration: int) -> object:
            events.append(f"acquisition_timing_{acq_index}_{tof}_{duration}")
            return original_acq_timing(acq_index, tof=tof, duration=duration)

        stack.adapter.generator.set_modulation = MagicMock(side_effect=record_generator_modulation)
        stack.adapter.acquisition.set_trigger_listener = MagicMock(side_effect=record_acquisition_listener)
        stack.adapter.acquisition.set_timing = MagicMock(side_effect=record_acquisition_timing)
        stack.adapter.trigger.set_duration = MagicMock(side_effect=lambda duration: events.append("trigger_duration"))
        stack.adapter.trigger.program_delays = MagicMock(side_effect=lambda **kwargs: events.append("trigger_delays"))

        list(stack.handler.run(config, cmd="run_experiment", session_id="test"))

        upload_index = events.index("upload_envelopes")
        compile_index = events.index("compile_waves")
        generator_index = events.index("generator_modulation_0_drive")
        acquisition_enable_index = events.index("acquisition_channel_0_4")
        acquisition_timing_index = events.index("acquisition_timing_0_0_64")
        trigger_duration_index = events.index("trigger_duration")
        trigger_delay_index = events.index("trigger_delays")
        disable_indices = [
            index
            for index, event in enumerate(events)
            if event.startswith("acquisition_channel_") and event.endswith("_0")
        ]

        assert upload_index < compile_index < generator_index
        assert disable_indices
        assert max(disable_indices) < acquisition_enable_index
        assert acquisition_enable_index < acquisition_timing_index < trigger_duration_index < trigger_delay_index

    def test_cleanup_disables_all_acquisitions(self, stack: object) -> None:
        """Verify ``cleanup()`` disables every acquisition via trigger channel zero."""
        disable_calls: list[tuple[int, int]] = []
        original_acq_listener = stack.adapter.acquisition.set_trigger_listener

        def record_acquisition_listener(acq_index: int, listener_cfg: dict) -> object:
            disable_calls.append((acq_index, int(listener_cfg["channel"])))
            return original_acq_listener(acq_index, listener_cfg)

        stack.adapter.acquisition.set_trigger_listener = MagicMock(side_effect=record_acquisition_listener)

        stack.handler.cleanup()

        assert disable_calls == [(0, 0), (1, 0)]
        assert stack.adapter.acquisition.acq_trigger_channels == {0: 0, 1: 0}

    def test_run_sweep_applies_point_zero_setup_before_streaming(self, stack: object) -> None:
        """Verify point-zero sweep setup completes before the first acquisition stream."""
        events: list[str] = []
        msg = {
            "sweep_id": "setup_order",
            "sweep_mode": "cartesian",
            "base": {
                "envelopes": {"0": [{"name": "env0", "num_samples": 1}]},
                "waves": {"0": [{"wave_id": "w0"}]},
                "generators": [{"gen_index": 0, "drive": {"frequency_mhz": "$freq"}}],
                "acquisitions": [{"acq_index": 0, "channel": 3, "duration": 64}],
                "trigger": {"shots": 1, "shot_duration": 500, "drive": {"1": {"delay": [[5, 0]]}}},
            },
            "variables": [{"name": "freq", "values": [10.0, 20.0]}],
        }

        chunk_shape = np.dtype([("i", "<i2"), ("q", "<i2")])
        original_gen_mod = stack.adapter.generator.set_modulation
        original_acq_listener = stack.adapter.acquisition.set_trigger_listener
        original_acq_timing = stack.adapter.acquisition.set_timing

        stack.handler.env_h.upload = MagicMock(side_effect=lambda cfg: events.append("upload_envelopes") or {})
        stack.handler.wave_h.compile = MagicMock(side_effect=lambda cfg: events.append("compile_waves") or {})

        def record_generator_modulation(gen_index: int, signal_kind: str, mod_cfg: dict) -> object:
            events.append(f"generator_modulation_{gen_index}_{signal_kind}")
            return original_gen_mod(gen_index, signal_kind, mod_cfg)

        def record_acquisition_listener(acq_index: int, listener_cfg: dict) -> object:
            events.append(f"acquisition_channel_{acq_index}_{int(listener_cfg['channel'])}")
            return original_acq_listener(acq_index, listener_cfg)

        def record_acquisition_timing(acq_index: int, *, tof: int, duration: int) -> object:
            events.append(f"acquisition_timing_{acq_index}_{tof}_{duration}")
            return original_acq_timing(acq_index, tof=tof, duration=duration)

        def record_stream(
            *,
            acq_indices: list[int],
            mode: str,
            shots: int,
            samp_per_shot: int,
            timeout: float | None = None,
            validate_chunk: bool = True,
        ):
            events.append("stream_start")
            stack.adapter.acquisition._last_timing_stats = {
                "total_ms": 1.0,
                "fpga_wait_ms": 0.5,
                "dma_overhead_ms": 0.1,
                "sw_overhead_ms": 0.35,
            }
            yield {0: np.zeros((shots, samp_per_shot), dtype=chunk_shape)}

        stack.adapter.generator.set_modulation = MagicMock(side_effect=record_generator_modulation)
        stack.adapter.acquisition.set_trigger_listener = MagicMock(side_effect=record_acquisition_listener)
        stack.adapter.acquisition.set_timing = MagicMock(side_effect=record_acquisition_timing)
        stack.adapter.trigger.set_duration = MagicMock(side_effect=lambda duration: events.append("trigger_duration"))
        stack.adapter.trigger.program_delays = MagicMock(side_effect=lambda **kwargs: events.append("trigger_delays"))
        stack.adapter.acquisition.run_multi_acquisition = MagicMock(side_effect=record_stream)
        stack.adapter.acquisition.prepare_sweep = MagicMock(
            side_effect=lambda mode, acq_indices: events.append("prepare_sweep")
        )
        stack.adapter.acquisition.end_sweep = MagicMock()

        list(stack.handler.run_sweep(msg, cmd="run_sweep", session_id="test", stop_check=lambda: False))

        stream_index = events.index("stream_start")
        prepare_index = events.index("prepare_sweep")

        assert events.index("upload_envelopes") < stream_index
        assert events.index("compile_waves") < stream_index
        assert events.index("generator_modulation_0_drive") < stream_index
        assert events.index("acquisition_timing_0_0_64") < stream_index
        assert events.index("trigger_duration") < stream_index
        assert events.index("trigger_delays") < stream_index
        assert stream_index < prepare_index


class TestStreamingCharacterization:
    """Characterization coverage for streaming-specific execution behavior."""

    def test_run_reports_raw_mode_shape_using_parallelism(self, stack: object) -> None:
        """Verify raw-mode metadata scales sample shape by acquisition parallelism."""
        config = {
            "acquisitions": [{"acq_index": 0, "output_type": "raw", "duration": 16, "channel": 1}],
            "trigger": {"shots": 3},
            "timeout": 1.5,
        }

        raw_shape = np.dtype([("i", "<i2"), ("q", "<i2")])
        stream_calls: list[dict[str, object]] = []
        parallelism = int(stack.adapter.hw_specs["acquisitions"][0]["parallelism"])

        def record_raw_stream(
            *,
            acq_indices: list[int],
            mode: str,
            shots: int,
            samp_per_shot: int,
            timeout: float | None = None,
            validate_chunk: bool = True,
        ):
            stream_calls.append(
                {
                    "acq_indices": list(acq_indices),
                    "mode": mode,
                    "shots": shots,
                    "samp_per_shot": samp_per_shot,
                    "timeout": timeout,
                    "validate_chunk": validate_chunk,
                }
            )
            stack.adapter.acquisition._last_timing_stats = {
                "total_ms": 1.0,
                "fpga_wait_ms": 0.5,
                "dma_overhead_ms": 0.1,
                "sw_overhead_ms": 0.35,
            }
            yield {0: np.zeros((shots, samp_per_shot * parallelism), dtype=raw_shape)}

        stack.adapter.acquisition.run_multi_acquisition = MagicMock(side_effect=record_raw_stream)

        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test"))
        header = events[0]

        assert isinstance(header, StreamHeader)
        assert header.metadata["ok"] is True
        assert header.metadata["acq_ip_metadata"] == {0: {"dtype": "iq_int16", "shape": [3, 16 * parallelism]}}
        assert stream_calls == [
            {
                "acq_indices": [0],
                "mode": "raw",
                "shots": 3,
                "samp_per_shot": 16,
                "timeout": 1.5,
                "validate_chunk": True,
            }
        ]

    def test_run_reports_accumulated_mode_shape_and_dtype(self, stack: object) -> None:
        """Verify accumulated-mode metadata reports scalar-per-shot output correctly."""
        config = {
            "acquisitions": [{"acq_index": 0, "output_type": "accumulated", "duration": 16, "channel": 1}],
            "trigger": {"shots": 5},
        }

        accumulated_shape = np.dtype([("i", "<i4"), ("q", "<i4")])

        def record_accumulated_stream(
            *,
            acq_indices: list[int],
            mode: str,
            shots: int,
            samp_per_shot: int,
            timeout: float | None = None,
            validate_chunk: bool = True,
        ):
            stack.adapter.acquisition._last_timing_stats = {
                "total_ms": 1.0,
                "fpga_wait_ms": 0.5,
                "dma_overhead_ms": 0.1,
                "sw_overhead_ms": 0.35,
            }
            yield {0: np.zeros(shots, dtype=accumulated_shape)}

        stack.adapter.acquisition.run_multi_acquisition = MagicMock(side_effect=record_accumulated_stream)

        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test"))
        header = events[0]
        binary_chunks = [event for event in events if isinstance(event, BinaryChunk)]

        assert isinstance(header, StreamHeader)
        assert header.metadata["ok"] is True
        assert header.metadata["acq_ip_metadata"] == {0: {"dtype": "iq_int32", "shape": [5]}}
        assert len(binary_chunks) == 1
        assert 0 in binary_chunks[0].binary_data

    def test_run_filters_deaf_acquisitions_from_streaming(self, stack: object) -> None:
        """Verify channel-zero acquisitions are excluded from metadata and capture."""
        config = {
            "acquisitions": [
                {"acq_index": 0, "output_type": "decimated", "duration": 16, "channel": 0},
                {"acq_index": 1, "output_type": "decimated", "duration": 16, "channel": 2},
            ],
            "trigger": {"shots": 4},
        }

        chunk_shape = np.dtype([("i", "<i2"), ("q", "<i2")])
        stream_calls: list[dict[str, object]] = []

        def record_decimated_stream(
            *,
            acq_indices: list[int],
            mode: str,
            shots: int,
            samp_per_shot: int,
            timeout: float | None = None,
            validate_chunk: bool = True,
        ):
            stream_calls.append(
                {
                    "acq_indices": list(acq_indices),
                    "mode": mode,
                    "shots": shots,
                    "samp_per_shot": samp_per_shot,
                }
            )
            stack.adapter.acquisition._last_timing_stats = {
                "total_ms": 1.0,
                "fpga_wait_ms": 0.5,
                "dma_overhead_ms": 0.1,
                "sw_overhead_ms": 0.35,
            }
            yield {1: np.zeros((shots, samp_per_shot), dtype=chunk_shape)}

        stack.adapter.acquisition.run_multi_acquisition = MagicMock(side_effect=record_decimated_stream)

        events = list(stack.handler.run(config, cmd="run_experiment", session_id="test"))
        header = events[0]
        binary_chunks = [event for event in events if isinstance(event, BinaryChunk)]

        assert isinstance(header, StreamHeader)
        assert header.metadata["ok"] is True
        assert header.metadata["acq_ip_metadata"] == {1: {"dtype": "iq_int16", "shape": [4, 16]}}
        assert stream_calls == [{"acq_indices": [1], "mode": "decimated", "shots": 4, "samp_per_shot": 16}]
        assert len(binary_chunks) == 1
        assert 0 not in binary_chunks[0].binary_data
        assert 1 in binary_chunks[0].binary_data


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

    def test_compile_waves_partial_failure_raises_first_error(self, stack: object) -> None:
        """WaveHandler.compile() raises WaveCompilationError on first failure in batch.

        When compile_waves returns a mix of successes and failures, the handler
        raises on the first failure only, discarding subsequent error details.
        This is acceptable because replace=True makes retries idempotent.
        """
        # Mock compile_waves to return one success + one failure
        stack.adapter.generator.compile_waves = MagicMock(
            return_value={
                "gen_index": 0,
                "waves": [{"wave_id": "good", "WDW": "0x1"}],
                "replaced": [],
                "skipped": [],
                "failed": [{"wave_id": "bad", "error": "envelope not found"}],
            }
        )

        config = {"waves": {"0": [{"wave_id": "good"}, {"wave_id": "bad"}]}}

        with pytest.raises(WaveCompilationError) as exc_info:
            stack.handler.wave_h.compile(config)

        assert "bad" in str(exc_info.value)
        assert "envelope not found" in str(exc_info.value)

    def test_reset_clears_wave_memory(self, stack: object) -> None:
        """Verify reset handler calls reset_wave_memory with only gen_index."""
        stack.adapter.generator.reset_wave_memory = MagicMock(return_value={})

        stack.handler.reset_h.reset_waves(gen_index=0)

        stack.adapter.generator.reset_wave_memory.assert_called_with(gen_index=0)

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

        # Force the adapter to crash on invalid index
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
