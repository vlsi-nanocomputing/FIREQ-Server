# file: fireq-utils/server/message_handler.py
"""Server-side message orchestration for FIREQ experiments.

Translates JSON experiment configurations into hardware actions via an adapter.
Provides run() for single experiments and run_sweep() for multi-point sweeps
with optimized fast-path reconfiguration.
"""

import logging
import time
from collections.abc import Callable, Iterator
from copy import deepcopy

import numpy as np

from ..models.queue_items import BinaryChunk, StreamHeader, StreamTiming
from ..models.results import SweepStatus, SweepTimingStats
from .handlers import EnvelopeHandler, ResetHandler, StatusHandler, WaveHandler
from .sweep_engine import (
    ValueTracker,
    apply_gen_type,
    apply_sweep_point,
    extract_mod_value,
    make_hashable,
    plan_sweep,
)


class MessageHandler:
    """Orchestrator for FIREQ experiment execution (single runs and sweeps)."""

    # =========================================================================
    #                           INITIALIZATION
    # =========================================================================

    def __init__(self, adapter: object, *, logger: logging.Logger | None = None) -> None:
        """Initialize the orchestrator and its specialized sub-handlers.

        :param adapter: Hardware adapter implementing the FIREQ control surface.
        :type adapter: object
        :param logger: Optional logger for consistent tracing across sub-handlers.
        :type logger: logging.Logger | None
        """
        self.adapter = adapter
        self.logger = logger or logging.getLogger(__name__)

        self.status_h = StatusHandler(adapter, self.logger)
        self.reset_h = ResetHandler(adapter, self.logger)
        self.env_h = EnvelopeHandler(adapter, self.logger)
        self.wave_h = WaveHandler(adapter, self.logger)

    # =========================================================================
    #                             PUBLIC API
    # =========================================================================

    def cleanup(self) -> None:
        """Hardware cleanup for abnormal termination (e.g., client disconnect).

        Called by TCP server as safety net. Normal completion cleanup is handled
        internally by run() and run_sweep().
        """
        try:
            if hasattr(self.adapter, "end_sweep"):
                self.adapter.end_sweep()
        except Exception as e:
            self.logger.warning(f"end_sweep during cleanup failed: {e}")

        try:
            self._disable_acquisitions()
        except Exception as e:
            self.logger.warning(f"_disable_acquisitions during cleanup failed: {e}")

    def run(
        self,
        config: dict,
        cmd: str,
        session_id: str,
    ) -> Iterator[StreamHeader | BinaryChunk | StreamTiming]:
        """Execute a single experiment configuration.

        :param config: Full or partial experiment configuration dictionary.
        :type config: dict
        :param cmd: Command name for response tagging.
        :type cmd: str
        :param session_id: Client session ID for response tagging.
        :type session_id: str
        :return: Iterator of queue-ready items (header, binary chunks, timing).
        :rtype: Iterator[StreamHeader | BinaryChunk | StreamTiming]
        """
        log: list[str] = []

        try:
            if "envelopes" in config:
                self.env_h.upload(config)

            if "waves" in config:
                self.wave_h.compile(config)

            for gen_cfg in config.get("generators", []):
                self._configure_generator(gen_cfg, log)

            self._disable_acquisitions(log=log)
            for acq_cfg in config.get("acquisitions", []):
                self._configure_acquisition(acq_cfg, log)
            self._disable_acquisitions(config, log)

            self._configure_trigger(config.get("trigger", {}), log)

            self._reset_dma_before_run()
            adc_indices = self._normalize_acq_configs(config)
            adc_indices, mode, shots, samp_per_shot, timeout = self._extract_acq_stream_params(config, adc_indices)

            # Pre-calculate chunk count (same logic as sweep protocol)
            n_chunks = 1
            if adc_indices and shots > 0:
                max_hw_shots = min(
                    self.adapter.acquisition._compute_max_hw_shots(mode, samp_per_shot, adc) for adc in adc_indices
                )
                n_chunks = (shots + max_hw_shots - 1) // max_hw_shots if max_hw_shots > 0 else 1

            # Yield header with n_chunks included (header_binary protocol)
            header_metadata = self._build_stream_metadata(
                adc_indices,
                mode,
                shots,
                samp_per_shot,
                config_log=log,
                n_chunks=n_chunks,
                stream_mode="header_binary",
            )
            header_metadata.update({"cmd": cmd, "session_id": session_id, "type": "experiment_header"})
            yield StreamHeader(type="experiment_header", metadata=header_metadata)

            # Stream binary-only chunks (no per-chunk JSON)
            if adc_indices:
                for chunk in self._stream_acquisition_only(
                    adc_indices=adc_indices,
                    mode=mode,
                    shots=shots,
                    samp_per_shot=samp_per_shot,
                    timeout=timeout,
                    validate_chunk=True,
                ):
                    timing_stats = getattr(self.adapter, "last_timing_stats", {})
                    yield BinaryChunk(
                        type="experiment_binary_chunk",
                        binary_data=chunk,
                        timing=(
                            timing_stats.get("fpga_wait_ms", 0.0),
                            timing_stats.get("sw_overhead_ms", 0.0),
                        ),
                    )

            timing_metadata = {
                "type": "experiment_timing",
                "cmd": cmd,
                "session_id": session_id,
                "debug_timing": dict(getattr(self.adapter, "last_timing_stats", {})),
            }
            yield StreamTiming(type="experiment_timing", metadata=timing_metadata)

        except Exception as e:
            self.logger.exception("Experiment execution sequence aborted")
            error_metadata = self._build_stream_metadata([], "decimated", 0, 0, config_log=log, ok=False, error=str(e))
            error_metadata.update({"cmd": cmd, "session_id": session_id, "type": "experiment_header"})
            yield StreamHeader(type="experiment_header", metadata=error_metadata)

    def run_sweep(
        self,
        msg: dict,
        cmd: str,
        session_id: str,
        stop_check: Callable[[], bool],
    ) -> Iterator[StreamHeader | BinaryChunk | StreamTiming]:
        """Execute a multi-point sweep with optimized fast-path reconfiguration.

        :param msg: Sweep message with base config, variables, and sweep_mode.
        :type msg: dict
        :param cmd: Command name for response tagging.
        :type cmd: str
        :param session_id: Client session ID for response tagging.
        :type session_id: str
        :param stop_check: Callable returning True if sweep should abort.
        :type stop_check: Callable[[], bool]
        :return: Iterator of queue-ready items (header, binary chunks, status).
        :rtype: Iterator[StreamHeader | BinaryChunk | StreamTiming]
        """
        sweep_id = msg["sweep_id"]
        base_config = msg["base"]
        variables = msg["variables"]
        sweep_mode = msg["sweep_mode"]

        timing = SweepTimingStats()
        n_points = 0
        n_completed = 0

        try:
            # Plan creation
            t_plan_start = time.perf_counter()
            plan = plan_sweep(base_config=base_config, variables=variables, sweep_mode=sweep_mode)
            timing.plan_ms = (time.perf_counter() - t_plan_start) * 1000.0

            n_points = plan.n_points
            points_iter = plan.iter_points()
            first_point = next(points_iter)

            self.logger.info(f"Sweep '{sweep_id}': {n_points} points, flags={plan.flags}")

            log = None
            t_last_acquisition_end: float | None = None

            # Setup phase (point 0 configuration)
            t_setup_start = time.perf_counter()

            current_config = deepcopy(base_config)
            apply_sweep_point(current_config, plan.var_paths_by_name, first_point)

            acq_list = current_config.get("acquisitions", [])
            self.logger.debug(
                f"Sweep config: {len(acq_list)} acquisition(s), "
                f"acq_indices={[a.get('acq_index') for a in acq_list]}"
            )

            if "envelopes" in current_config:
                self.env_h.upload(current_config)
            if "waves" in current_config:
                self.wave_h.compile(current_config)

            for gen_cfg in current_config.get("generators", []):
                self._configure_generator(gen_cfg, log)
            self._disable_acquisitions(log=log)
            for acq_cfg in current_config.get("acquisitions", []):
                self._configure_acquisition(acq_cfg, log)
            self._disable_acquisitions(current_config, log)
            self._configure_trigger(current_config.get("trigger", {}), log)

            timing.setup_ms = (time.perf_counter() - t_setup_start) * 1000.0

            adc_indices = self._normalize_acq_configs(current_config)
            adc_indices, mode, shots, samp_per_shot, timeout = self._extract_acq_stream_params(
                current_config, adc_indices
            )
            self.logger.debug(f"Extracted adc_indices={adc_indices}, mode={mode}, shots={shots}")

            metadata_payload = self._build_stream_metadata(adc_indices, mode, shots, samp_per_shot)

            # Compute chunks_per_point based on hardware buffer limits
            if adc_indices and shots > 0:
                max_hw_shots = min(
                    self.adapter.acquisition._compute_max_hw_shots(mode, samp_per_shot, adc) for adc in adc_indices
                )
                chunks_per_point = (shots + max_hw_shots - 1) // max_hw_shots if max_hw_shots > 0 else 1
            else:
                chunks_per_point = 1

            # Yield sweep header
            header_metadata = {
                "type": "sweep_header",
                "cmd": cmd,
                "session_id": session_id,
                "sweep_id": sweep_id,
                "stream_mode": "header_binary",
                "n_points": n_points,
                "adc_metadata": metadata_payload.get("adc_metadata", {}),
                "chunks_per_point": chunks_per_point,
            }
            yield StreamHeader(type="sweep_header", metadata=header_metadata)

            # Local timing accumulators
            _has_timing = hasattr(self.adapter, "last_timing_stats")
            _hw_ms = _dma_ms = _sw_ms = 0.0
            _n_timed = 0

            # Point 0 acquisition
            if adc_indices:
                for item in self._stream_sweep_point_items(
                    adc_indices, mode, shots, samp_per_shot, timeout, validate=True
                ):
                    if _has_timing:
                        stats = self.adapter.last_timing_stats
                        _hw_ms += stats.get("fpga_wait_ms", 0.0)
                        _dma_ms += stats.get("dma_overhead_ms", 0.0)
                        _sw_ms += stats.get("sw_overhead_ms", 0.0)
                        _n_timed += 1
                    yield item

            t_last_acquisition_end = time.perf_counter()
            n_completed = 1

            if n_points == 1:
                t_finalize_start = time.perf_counter()
                self.adapter.end_sweep()
                timing.finalize_ms = (time.perf_counter() - t_finalize_start) * 1000.0
                timing.total_hardware_ms = _hw_ms
                timing.total_dma_overhead_ms = _dma_ms
                timing.total_sw_overhead_ms = _sw_ms
                timing.n_points_timed = _n_timed
                status = SweepStatus(True, sweep_id, n_points, n_completed, timing_stats=timing)
                yield StreamTiming(
                    type="sweep_status",
                    metadata={"type": "sweep_status", "cmd": cmd, "session_id": session_id, **status.to_dict()},
                )
                return

            # Prepare fast-path for points 1+
            t_prepare_start = time.perf_counter()

            if adc_indices:
                self.adapter.prepare_sweep(mode, adc_indices)

            sweep_config = current_config
            if not plan.has_envelope_vars:
                sweep_config.pop("envelopes", None)
            if not plan.has_waves_changes:
                sweep_config.pop("waves", None)

            fixed_mode = mode
            fixed_timeout = timeout
            fixed_shots = shots
            fixed_samp_per_shot = samp_per_shot

            # Create value tracker for change detection (data-driven, no closures)
            tracker = ValueTracker()

            timing.prepare_sweep_ms = (time.perf_counter() - t_prepare_start) * 1000.0

            # Points 1+ loop
            for i, point in enumerate(points_iter, start=1):
                if stop_check():
                    self.logger.info(f"Sweep stopped at point {i}")
                    break

                t_point_start = time.perf_counter()
                if t_last_acquisition_end is not None:
                    timing.inter_point_overhead_ms += (t_point_start - t_last_acquisition_end) * 1000.0

                apply_sweep_point(sweep_config, plan.var_paths_by_name, point)
                self._apply_sweep_updates(sweep_config, plan.flags, tracker)

                if adc_indices:
                    sh = int(sweep_config["trigger"]["shots"]) if plan.has_shots_var else fixed_shots
                    sp = (
                        int(sweep_config["acquisitions"][0]["duration"])
                        if plan.has_duration_var
                        else fixed_samp_per_shot
                    )
                    to = float(sweep_config["timeout"]) if plan.has_timeout_var else fixed_timeout
                    for item in self._stream_sweep_point_items(adc_indices, fixed_mode, sh, sp, to, validate=False):
                        if _has_timing:
                            stats = self.adapter.last_timing_stats
                            _hw_ms += stats.get("fpga_wait_ms", 0.0)
                            _dma_ms += stats.get("dma_overhead_ms", 0.0)
                            _sw_ms += stats.get("sw_overhead_ms", 0.0)
                            _n_timed += 1
                        yield item

                t_last_acquisition_end = time.perf_counter()
                n_completed += 1

            # Finalize timing
            timing.total_hardware_ms = _hw_ms
            timing.total_dma_overhead_ms = _dma_ms
            timing.total_sw_overhead_ms = _sw_ms
            timing.n_points_timed = _n_timed

            t_finalize_start = time.perf_counter()
            self.adapter.end_sweep()
            timing.finalize_ms = (time.perf_counter() - t_finalize_start) * 1000.0

            status = SweepStatus(True, sweep_id, n_points, n_completed, timing_stats=timing)
            yield StreamTiming(
                type="sweep_status",
                metadata={"type": "sweep_status", "cmd": cmd, "session_id": session_id, **status.to_dict()},
            )

        except Exception as e:
            self.logger.exception(f"Sweep '{sweep_id}' failed")
            try:
                t_finalize_start = time.perf_counter()
                self.adapter.end_sweep()
                timing.finalize_ms = (time.perf_counter() - t_finalize_start) * 1000.0
            except Exception as cleanup_err:
                self.logger.error(f"Failed to end sweep during cleanup: {cleanup_err}")
            status = SweepStatus(False, sweep_id, n_points, n_completed, str(e), timing_stats=timing)
            yield StreamTiming(
                type="sweep_status",
                metadata={"type": "sweep_status", "cmd": cmd, "session_id": session_id, **status.to_dict()},
            )

    # =========================================================================
    #                       SWEEP FAST-PATH UPDATES
    # =========================================================================

    def _apply_sweep_updates(
        self,
        config: dict,
        flags: dict,
        tracker: ValueTracker,
    ) -> None:
        """Apply sweep fast-path updates with value change detection.

        Called for each sweep point after apply_point() to update only
        hardware subsystems with changed values.

        :param config: Current experiment configuration (mutated by apply_point).
        :type config: dict
        :param flags: Sweep flags indicating which hardware to reconfigure.
        :type flags: dict
        :param tracker: Value tracker for change detection.
        :type tracker: ValueTracker
        """
        gen_flags = flags.get("generators", {})
        acq_flags = flags.get("acquisitions", {})
        trig_flags = flags.get("trigger", set())
        waves_flags = flags.get("waves", set())

        # Wave compilation
        if "waves_compile" in waves_flags and "waves" in config:
            val = make_hashable(config["waves"])
            if tracker.changed(("waves", "compile"), val):
                self.wave_h.compile(config)

        # Generator updates
        for gen_list_idx, gen_cfg in enumerate(config.get("generators", [])):
            gf = gen_flags.get(gen_list_idx, set())
            if not gf:
                continue

            gen_idx = int(gen_cfg["gen_index"])
            drive_cfg = gen_cfg.get("drive")
            readout_cfg = gen_cfg.get("readout")
            drive_flags = gf & {"drive_mod", "drive_nyquist", "drive_channel", "drive_fifo"}
            readout_flags = gf & {"readout_mod", "readout_nyquist", "readout_channel", "readout_wave"}

            if drive_cfg and drive_flags:
                apply_gen_type(self.adapter, gen_idx, drive_cfg, drive_flags, "drive", tracker)
            if readout_cfg and readout_flags:
                apply_gen_type(self.adapter, gen_idx, readout_cfg, readout_flags, "readout", tracker)

        # Acquisition updates
        for acq_list_idx, acq_cfg in enumerate(config.get("acquisitions", [])):
            af = acq_flags.get(acq_list_idx, set())
            if not af:
                continue

            acq_idx = int(acq_cfg["acq_index"])

            if "acq_mod" in af and "frequency_mhz" in acq_cfg:
                val = extract_mod_value(acq_cfg)
                if tracker.changed(("acq", acq_idx, "acq_mod"), val):
                    self.adapter.acquisition_modulation(acq_idx, {"frequency_mhz": val[0], "phase": val[1]})

            if "acq_channel" in af and "channel" in acq_cfg:
                val = int(acq_cfg["channel"])
                if tracker.changed(("acq", acq_idx, "acq_channel"), val):
                    self.adapter.acq_trigger2listen(acq_idx, {"ttype": "acquisition", "channel": val})

            if af & {"acq_duration", "acq_tof"} and "duration" in acq_cfg:
                val = (int(acq_cfg.get("tof", 0)), int(acq_cfg["duration"]))
                if tracker.changed(("acq", acq_idx, "acq_timing"), val):
                    self.adapter.acquisition_timing(acq_idx, tof=val[0], duration=val[1])

        # Trigger updates
        if trig_flags:
            trig_cfg = config.get("trigger", {})

            if "trig_shot_duration" in trig_flags and "shot_duration" in trig_cfg:
                val = int(trig_cfg["shot_duration"])
                if tracker.changed(("trig", "shot_duration"), val):
                    self.adapter.tg_set_duration(val)

            if trig_flags & {"trig_drive", "trig_readout"}:
                drive_cfg = trig_cfg.get("drive") if "trig_drive" in trig_flags else None
                readout_cfg = trig_cfg.get("readout") if "trig_readout" in trig_flags else None
                start_idx = trig_cfg.get("drive_start_index", 1)

                # Deep conversion to capture nested delay values
                drive_val = make_hashable(drive_cfg) if drive_cfg else None
                readout_val = make_hashable(readout_cfg) if readout_cfg else None
                val = (drive_val, readout_val, start_idx)

                if tracker.changed(("trig", "delays"), val):
                    self.adapter.tg_program_delays(
                        drive=drive_cfg,
                        readout=readout_cfg,
                        drive_start_index=start_idx,
                    )

    # =========================================================================
    #                       HARDWARE CONFIGURATION
    # =========================================================================

    def _configure_generator(self, gen_cfg: dict, log: list | None = None) -> None:
        """Configure a generator. Applies all settings present in the config dict.

        :param gen_cfg: Generator configuration dictionary.
        :type gen_cfg: dict
        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        """
        gen_index = gen_cfg["gen_index"]
        self.logger.debug(f"Configuring generator {gen_index}")

        if drive := gen_cfg.get("drive"):
            if "frequency_mhz" in drive:
                self.adapter.generator_modulation(
                    gen_index,
                    "drive",
                    {"frequency_mhz": float(drive["frequency_mhz"]), "phase": float(drive.get("phase", 0.0))},
                )
                self._log(log, f"gen {gen_index} drive frequency: {drive['frequency_mhz']} MHz")
            if "nyquist_zone" in drive:
                self.adapter.set_nyquist_zone(gen_index, "drive", int(drive["nyquist_zone"]))
            if "channel" in drive:
                self.adapter.gen_trigger2listen(gen_index, {"ttype": "drive", "channel": int(drive["channel"])})
            if "fifo" in drive:
                self.adapter.program_drive_sequence(
                    gen_index=gen_index, wave_id_list=drive["fifo"], start_index=drive.get("fifo_start_index", 1)
                )
                self._log(log, f"gen {gen_index} drive sequence programmed")

        if readout := gen_cfg.get("readout"):
            if "frequency_mhz" in readout:
                self.adapter.generator_modulation(
                    gen_index,
                    "readout",
                    {"frequency_mhz": float(readout["frequency_mhz"]), "phase": float(readout.get("phase", 0.0))},
                )
            if "nyquist_zone" in readout:
                self.adapter.set_nyquist_zone(gen_index, "readout", int(readout["nyquist_zone"]))
            if "channel" in readout:
                self.adapter.gen_trigger2listen(gen_index, {"ttype": "readout", "channel": int(readout["channel"])})
            if "wave" in readout:
                self.adapter.upload_readout_wave(gen_index=gen_index, wave=readout["wave"], replace=True)
                self._log(log, f"gen {gen_index} readout wave uploaded")

    def _configure_acquisition(self, acq_cfg: dict, log: list | None = None) -> None:
        """Configure an acquisition. Applies all settings present in the config dict.

        :param acq_cfg: Acquisition configuration dictionary.
        :type acq_cfg: dict
        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        """
        acq_index = acq_cfg["acq_index"]

        if "frequency_mhz" in acq_cfg:
            self.adapter.acquisition_modulation(
                acq_index,
                {"frequency_mhz": float(acq_cfg["frequency_mhz"]), "phase": float(acq_cfg.get("phase", 0.0))},
            )
        if "channel" in acq_cfg:
            self.adapter.acq_trigger2listen(acq_index, {"ttype": "acquisition", "channel": int(acq_cfg["channel"])})
            self._log(log, f"acq {acq_index} listening to trigger channel {acq_cfg['channel']}")
        if "duration" in acq_cfg:
            tof = int(acq_cfg.get("tof", 0))
            self.adapter.acquisition_timing(acq_index, tof=tof, duration=int(acq_cfg["duration"]))
            self._log(log, f"acq {acq_index} timing set: tof={tof}")

    def _configure_trigger(self, trigger_cfg: dict, log: list | None = None) -> None:
        """Configure trigger routing and timing. Applies all settings present in the config dict.

        :param trigger_cfg: Trigger configuration dictionary.
        :type trigger_cfg: dict
        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        """
        if not trigger_cfg:
            return

        if "shot_duration" in trigger_cfg:
            self.adapter.tg_set_duration(int(trigger_cfg["shot_duration"]))

        has_drive = "drive" in trigger_cfg
        has_readout = "readout" in trigger_cfg
        if has_drive or has_readout:
            self.adapter.tg_program_delays(
                drive=trigger_cfg.get("drive") if has_drive else None,
                readout=trigger_cfg.get("readout") if has_readout else None,
                drive_start_index=trigger_cfg.get("drive_start_index", 1),
            )
            shots = trigger_cfg.get("shots")
            msg = "trigger delays programmed" if shots is None else f"trigger delays programmed for {shots} shots"
            self._log(log, msg)

    def _disable_acquisitions(self, config: dict | None = None, log: list | None = None) -> None:
        """Disable trigger listening on acquisitions. If config given, only unused ones.

        :param config: Optional experiment configuration. If provided, only disables unused acquisitions.
        :type config: dict | None
        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        """
        total = self.status_h.num_acquisitions
        if total <= 0:
            return
        used = {int(acq["acq_index"]) for acq in config.get("acquisitions", [])} if config else set()
        for acq_index in range(total):
            if acq_index not in used:
                self.adapter.acq_trigger2listen(acq_index, {"ttype": "acquisition", "channel": 0})
                self._log(log, f"acq {acq_index} disabled (trigger channel 0)")

    # =========================================================================
    #                       ACQUISITION STREAMING
    # =========================================================================

    def _stream_acquisition_only(
        self,
        *,
        adc_indices: list[int],
        mode: str,
        shots: int,
        samp_per_shot: int,
        timeout: float,
        validate_chunk: bool = True,
    ) -> Iterator[dict[int, np.ndarray]]:
        """Stream acquisition chunks using run_multi_acquisition().

        :param adc_indices: ADC indices to capture.
        :type adc_indices: list[int]
        :param mode: Acquisition output mode.
        :type mode: str
        :param shots: Number of shots.
        :type shots: int
        :param samp_per_shot: Samples per shot.
        :type samp_per_shot: int
        :param timeout: Timeout in seconds.
        :type timeout: float
        :param validate_chunk: If True, perform input validation.
        :type validate_chunk: bool
        :return: Iterator over data_dict.
        :rtype: Iterator[dict[int, np.ndarray]]
        """
        if not adc_indices or shots <= 0:
            return

        yield from self.adapter.run_multi_acquisition(
            adc_indices=adc_indices,
            mode=mode,
            shots=shots,
            samp_per_shot=samp_per_shot,
            timeout=timeout,
            validate_chunk=validate_chunk,
        )

    def _stream_sweep_point_items(
        self,
        adc_indices: list[int],
        mode: str,
        shots: int,
        samp_per_shot: int,
        timeout: float,
        validate: bool = True,
    ) -> Iterator[BinaryChunk]:
        """Stream acquisition for one sweep point, yielding BinaryChunk items.

        :param adc_indices: ADC indices to capture.
        :type adc_indices: list[int]
        :param mode: Acquisition output mode.
        :type mode: str
        :param shots: Number of shots.
        :type shots: int
        :param samp_per_shot: Samples per shot.
        :type samp_per_shot: int
        :param timeout: Timeout in seconds.
        :type timeout: float
        :param validate: Whether to validate chunks (True for point 0).
        :type validate: bool
        :return: Iterator of BinaryChunk items.
        :rtype: Iterator[BinaryChunk]
        """
        for chunk in self._stream_acquisition_only(
            adc_indices=adc_indices,
            mode=mode,
            shots=shots,
            samp_per_shot=samp_per_shot,
            timeout=timeout,
            validate_chunk=validate,
        ):
            timing_stats = getattr(self.adapter, "last_timing_stats", {})
            yield BinaryChunk(
                type="sweep_binary_point",
                binary_data=chunk,
                timing=(
                    timing_stats.get("fpga_wait_ms", 0.0),
                    timing_stats.get("sw_overhead_ms", 0.0),
                ),
            )

    # =========================================================================
    #                         INTERNAL HELPERS
    # =========================================================================

    @staticmethod
    def _log(log: list | None, msg: str) -> None:
        """Append a message to the config log if provided.

        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        :param msg: Message to append.
        :type msg: str
        """
        if log is not None:
            log.append(msg)

    def _normalize_acq_configs(self, config: dict) -> list[int]:
        """Extract acquisition indices from config, excluding deaf acquisitions.

        :param config: Experiment configuration.
        :type config: dict
        :return: ADC indices to capture (only active ones with channel != 0).
        :rtype: list[int]
        """
        acquisitions = config.get("acquisitions", [])
        all_indices = [acq["acq_index"] for acq in acquisitions]

        # Filter out deaf acquisitions (channel == 0)
        active_indices = [idx for idx in all_indices if self.adapter._acq_trigger_channel.get(idx, 0) != 0]

        if len(active_indices) < len(all_indices):
            deaf = set(all_indices) - set(active_indices)
            self.logger.info(f"_normalize_acq_configs: filtered deaf acq(s) {deaf}, " f"active={active_indices}")
        else:
            self.logger.debug(f"_normalize_acq_configs: {len(acquisitions)} acq(s) -> indices={active_indices}")

        return active_indices

    def _extract_acq_stream_params(
        self,
        config: dict,
        adc_indices: list[int] | None = None,
    ) -> tuple[list[int], str, int, int, float]:
        """Extract acquisition stream parameters from config.

        :param config: Experiment configuration.
        :type config: dict
        :param adc_indices: Optional precomputed ADC indices.
        :type adc_indices: list[int] | None
        :return: Tuple (adc_indices, mode, shots, samp_per_shot, timeout).
        :rtype: tuple[list[int], str, int, int, float]
        """
        acquisitions = config.get("acquisitions", [])
        if not acquisitions:
            return [], "decimated", 0, 0, float(config.get("timeout", 10.0))

        if adc_indices is None:
            adc_indices = [acq["acq_index"] for acq in acquisitions]
        mode = acquisitions[0].get("output_type", "decimated")
        trigger_cfg = config.get("trigger", {})
        shots = int(trigger_cfg.get("shots", 1))
        samp_per_shot = int(acquisitions[0].get("duration", 256))
        timeout = float(config.get("timeout", 10.0))
        return adc_indices, mode, shots, samp_per_shot, timeout

    def _build_stream_metadata(
        self,
        adc_indices: list[int],
        mode: str,
        shots: int,
        samp_per_shot: int,
        *,
        config_log: list[str] | None = None,
        ok: bool = True,
        error: str | None = None,
        n_chunks: int = 1,
        stream_mode: str = "header_binary",
    ) -> dict:
        """Build metadata payload emitted before streaming chunks.

        :param adc_indices: ADC indices to capture.
        :type adc_indices: list[int]
        :param mode: Acquisition output mode.
        :type mode: str
        :param shots: Number of shots.
        :type shots: int
        :param samp_per_shot: Samples per shot.
        :type samp_per_shot: int
        :param config_log: Optional configuration log.
        :type config_log: list[str] | None
        :param ok: Success flag.
        :type ok: bool
        :param error: Optional error message.
        :type error: str | None
        :param n_chunks: Total number of binary chunks to expect.
        :type n_chunks: int
        :param stream_mode: Protocol streaming mode.
        :type stream_mode: str
        :return: Metadata payload.
        :rtype: dict
        """
        adc_metadata: dict[int, dict[str, object]] = {}
        for adc_idx in adc_indices:
            shape = self._compute_expected_shape(mode, shots, samp_per_shot, adc_idx)
            dtype = "iq_int32" if mode == "accumulated" else "iq_int16"
            adc_metadata[adc_idx] = {"dtype": dtype, "shape": shape}
        payload: dict[str, object] = {
            "ok": ok,
            "adc_metadata": adc_metadata,
            "n_chunks": n_chunks,
            "stream_mode": stream_mode,
        }
        if config_log is not None:
            payload["config_log"] = config_log
        if error:
            payload["error"] = error
        return payload

    def _reset_dma_before_run(self) -> None:
        """Perform a minimal DMA reset before a new run/sweep point."""
        try:
            dma_engine = getattr(self.adapter, "dma_engine", None)
            if dma_engine is not None and hasattr(dma_engine, "abort"):
                self.logger.info("Resetting DMA before acquisition run.")
                dma_engine.abort()
        except Exception as e:
            self.logger.warning(f"DMA reset before run failed: {e}")

    def _compute_expected_shape(self, mode: str, shots: int, samp_per_shot: int, adc_index: int) -> list[int]:
        """Compute the expected array shape for a given ADC.

        :param mode: Acquisition mode.
        :type mode: str
        :param shots: Number of shots.
        :type shots: int
        :param samp_per_shot: Samples per shot.
        :type samp_per_shot: int
        :param adc_index: ADC index.
        :type adc_index: int
        :return: Expected array shape.
        :rtype: list[int]
        """
        if mode == "accumulated":
            return [int(shots)]
        if mode == "decimated":
            return [int(shots), int(samp_per_shot)]
        if mode == "raw":
            parallelism = int(self.adapter.hw_specs["acquisitions"][adc_index].get("parallelism", 1))
            return [int(shots), int(samp_per_shot) * parallelism]
        return [int(shots), int(samp_per_shot)]


__all__ = ["MessageHandler"]
