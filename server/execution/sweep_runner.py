"""Sweep orchestration for FIREQ experiments.

Owns the multi-point sweep execution lifecycle: plan creation, point-zero
setup, fast-path preparation, per-point streaming, and final status emission.
"""

import logging
import time
from collections.abc import Callable, Iterator
from copy import deepcopy

from ..models.queue_items import BinaryChunk, StreamHeader, StreamTiming
from ..models.results import SweepStatus, SweepTimingStats
from .hardware_config import HardwareConfigurator
from .streaming import AcquisitionStreamer, AcquisitionStreamParams
from .sweep_planning import SweepPlan, apply_sweep_point, plan_sweep
from .sweep_updates import SweepUpdateApplier, ValueTracker


class SweepRunner:
    """Executes multi-point sweeps using prepared execution collaborators.

    :param adapter: Hardware adapter implementing the FIREQ control surface.
    :type adapter: object
    :param hardware_configurator: Applies full point-zero hardware setup.
    :type hardware_configurator: HardwareConfigurator
    :param acquisition_streamer: Resolves stream params and emits sweep chunks.
    :type acquisition_streamer: AcquisitionStreamer
    :param sweep_update_applier: Applies fast-path delta updates between points.
    :type sweep_update_applier: SweepUpdateApplier
    :param logger: Optional logger for sweep tracing.
    :type logger: logging.Logger | None
    """

    def __init__(
        self,
        adapter: object,
        hardware_configurator: HardwareConfigurator,
        acquisition_streamer: AcquisitionStreamer,
        sweep_update_applier: SweepUpdateApplier,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize with explicit sweep execution dependencies.

        :param adapter: Hardware adapter implementing the FIREQ control surface.
        :type adapter: object
        :param hardware_configurator: Applies full point-zero hardware setup.
        :type hardware_configurator: HardwareConfigurator
        :param acquisition_streamer: Resolves stream params and emits sweep chunks.
        :type acquisition_streamer: AcquisitionStreamer
        :param sweep_update_applier: Applies fast-path delta updates between points.
        :type sweep_update_applier: SweepUpdateApplier
        :param logger: Optional logger for sweep tracing.
        :type logger: logging.Logger | None
        """
        self._adapter = adapter
        self._hw_config = hardware_configurator
        self._streamer = acquisition_streamer
        self._sweep_updates = sweep_update_applier
        self._logger = logger or logging.getLogger(__name__)

    def run(
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
        prepare_called = False

        try:
            plan, first_point, points_iter = self._build_sweep_plan(
                sweep_id=sweep_id,
                base_config=base_config,
                variables=variables,
                sweep_mode=sweep_mode,
                timing=timing,
            )
            n_points = plan.n_points
            last_acquisition_end_time: float | None = None
            current_config, params = self._setup_first_sweep_point(
                base_config=base_config,
                plan=plan,
                first_point=first_point,
                timing=timing,
            )
            yield self._build_sweep_header(
                cmd=cmd,
                session_id=session_id,
                sweep_id=sweep_id,
                n_points=n_points,
                params=params,
            )

            # Wall-clock reference for the entire acquisition phase
            wall_clock_start_time = time.perf_counter()

            # Local timing accumulators
            has_timing = hasattr(self._adapter.acquisition, "last_timing_stats")
            hardware_ms = dma_overhead_ms = software_overhead_ms = 0.0
            n_points_timed = 0

            # Point 0 acquisition
            if params.acquisition_indices:
                yield from self._streamer.stream_sweep_point(
                    params.acquisition_indices,
                    params.mode,
                    params.shots,
                    params.samples_per_shot,
                    params.timeout_s,
                    validate=True,
                )
                if has_timing:
                    stats = self._adapter.acquisition.last_timing_stats
                    hardware_ms += stats.get("fpga_wait_ms", 0.0)
                    dma_overhead_ms += stats.get("dma_overhead_ms", 0.0)
                    software_overhead_ms += stats.get("sw_overhead_ms", 0.0)
                    n_points_timed += 1

            last_acquisition_end_time = time.perf_counter()
            n_completed = 1

            if n_points == 1:
                # No prepare_sweep was called -> no end_sweep needed
                timing.total_hardware_ms = hardware_ms
                timing.total_dma_overhead_ms = dma_overhead_ms
                timing.total_sw_overhead_ms = software_overhead_ms
                timing.n_points_timed = n_points_timed
                timing.wall_clock_ms = (time.perf_counter() - wall_clock_start_time) * 1000.0
                status = SweepStatus(True, sweep_id, n_points, n_completed, timing_stats=timing)
                yield StreamTiming(
                    type="sweep_status",
                    metadata={"type": "sweep_status", "cmd": cmd, "session_id": session_id, **status.to_dict()},
                )
                return

            sweep_config, tracker, prepare_called = self._prepare_sweep_fast_path(
                current_config=current_config,
                plan=plan,
                params=params,
                timing=timing,
            )

            # Points 1+ loop
            for point_index, point in enumerate(points_iter, start=1):
                if stop_check():
                    self._logger.info(f"Sweep stopped at point {point_index}")
                    break

                point_start_time = time.perf_counter()
                if last_acquisition_end_time is not None:
                    timing.inter_point_overhead_ms += (point_start_time - last_acquisition_end_time) * 1000.0

                apply_sweep_point(sweep_config, plan.var_paths_by_name, point)
                self._sweep_updates.apply(sweep_config, plan.flags, tracker)

                if params.acquisition_indices:
                    shots, samples_per_shot, timeout_s = AcquisitionStreamer.resolve_sweep_point_params(
                        sweep_config,
                        plan,
                        params,
                    )
                    yield from self._streamer.stream_sweep_point(
                        params.acquisition_indices,
                        params.mode,
                        shots,
                        samples_per_shot,
                        timeout_s,
                        validate=False,
                    )
                    if has_timing:
                        stats = self._adapter.acquisition.last_timing_stats
                        hardware_ms += stats.get("fpga_wait_ms", 0.0)
                        dma_overhead_ms += stats.get("dma_overhead_ms", 0.0)
                        software_overhead_ms += stats.get("sw_overhead_ms", 0.0)
                        n_points_timed += 1

                last_acquisition_end_time = time.perf_counter()
                n_completed += 1

            # Finalize timing
            timing.total_hardware_ms = hardware_ms
            timing.total_dma_overhead_ms = dma_overhead_ms
            timing.total_sw_overhead_ms = software_overhead_ms
            timing.n_points_timed = n_points_timed

            finalize_start_time = time.perf_counter()
            self._adapter.acquisition.end_sweep()
            prepare_called = False  # Mark as handled — skip finally cleanup
            timing.finalize_ms = (time.perf_counter() - finalize_start_time) * 1000.0
            timing.wall_clock_ms = (time.perf_counter() - wall_clock_start_time) * 1000.0

            status = SweepStatus(True, sweep_id, n_points, n_completed, timing_stats=timing)
            yield StreamTiming(
                type="sweep_status",
                metadata={"type": "sweep_status", "cmd": cmd, "session_id": session_id, **status.to_dict()},
            )

        except Exception as error:
            self._logger.exception(f"Sweep '{sweep_id}' failed")
            # end_sweep() is handled by the finally block below
            status = SweepStatus(False, sweep_id, n_points, n_completed, str(error), timing_stats=timing)
            yield StreamTiming(
                type="sweep_status",
                metadata={"type": "sweep_status", "cmd": cmd, "session_id": session_id, **status.to_dict()},
            )

        finally:
            if prepare_called:
                try:
                    self._adapter.acquisition.end_sweep()
                except Exception as cleanup_error:
                    self._logger.error(f"Failed to end sweep during cleanup: {cleanup_error}")

    def _build_sweep_plan(
        self,
        sweep_id: str,
        base_config: dict,
        variables: list[dict],
        sweep_mode: str,
        timing: SweepTimingStats,
    ) -> tuple[SweepPlan, dict, Iterator[dict]]:
        """Build the sweep plan and its point iterator.

        :param sweep_id: Sweep identifier used for logging.
        :type sweep_id: str
        :param base_config: Base experiment configuration.
        :type base_config: dict
        :param variables: Sweep variable definitions.
        :type variables: list[dict]
        :param sweep_mode: Sweep topology mode.
        :type sweep_mode: str
        :param timing: Timing accumulator updated with planning duration.
        :type timing: SweepTimingStats
        :return: Sweep plan, first point, and iterator over remaining points.
        :rtype: tuple[SweepPlan, dict, Iterator[dict]]
        """
        plan_start_time = time.perf_counter()
        plan = plan_sweep(base_config=base_config, variables=variables, sweep_mode=sweep_mode)
        timing.plan_ms = (time.perf_counter() - plan_start_time) * 1000.0

        points_iter = plan.iter_points()
        first_point = next(points_iter)

        self._logger.info(f"Sweep '{sweep_id}': {plan.n_points} points, flags={plan.flags}")
        return plan, first_point, points_iter

    def _setup_first_sweep_point(
        self,
        base_config: dict,
        plan: SweepPlan,
        first_point: dict,
        timing: SweepTimingStats,
    ) -> tuple[dict, AcquisitionStreamParams]:
        """Apply the first sweep point and configure hardware for it.

        :param base_config: Base experiment configuration.
        :type base_config: dict
        :param plan: Sweep plan metadata.
        :type plan: SweepPlan
        :param first_point: First sweep point values.
        :type first_point: dict
        :param timing: Timing accumulator updated with setup duration.
        :type timing: SweepTimingStats
        :return: Config mutated with the first point and resolved stream params.
        :rtype: tuple[dict, AcquisitionStreamParams]
        """
        setup_start_time = time.perf_counter()

        current_config = deepcopy(base_config)
        apply_sweep_point(current_config, plan.var_paths_by_name, first_point)

        acquisition_configs = current_config.get("acquisitions", [])
        self._logger.debug(
            f"Sweep config: {len(acquisition_configs)} acquisition(s), "
            f"acquisition_indices={[acquisition.get('acq_index') for acquisition in acquisition_configs]}"
        )

        self._hw_config.apply_full_config(current_config, log=None)
        timing.setup_ms = (time.perf_counter() - setup_start_time) * 1000.0

        params = self._streamer.resolve_params(current_config)
        self._logger.debug(
            f"Extracted acquisition_indices={params.acquisition_indices}, " f"mode={params.mode}, shots={params.shots}"
        )
        return current_config, params

    def _build_sweep_header(
        self,
        cmd: str,
        session_id: str,
        sweep_id: str,
        n_points: int,
        params: AcquisitionStreamParams,
    ) -> StreamHeader:
        """Build the header item emitted before sweep data streaming.

        :param cmd: Command name for response tagging.
        :type cmd: str
        :param session_id: Client session ID for response tagging.
        :type session_id: str
        :param sweep_id: Sweep identifier.
        :type sweep_id: str
        :param n_points: Total sweep points.
        :type n_points: int
        :param params: Resolved stream parameters for point zero.
        :type params: AcquisitionStreamParams
        :return: Sweep header queue item.
        :rtype: StreamHeader
        """
        metadata_payload = self._streamer.build_metadata(
            params.acquisition_indices,
            params.mode,
            params.shots,
            params.samples_per_shot,
        )
        header_metadata = {
            "type": "sweep_header",
            "cmd": cmd,
            "session_id": session_id,
            "sweep_id": sweep_id,
            "stream_mode": "header_binary",
            "n_points": n_points,
            "acq_ip_metadata": metadata_payload.get("acq_ip_metadata", {}),
            "chunks_per_point": params.n_chunks,
        }
        return StreamHeader(type="sweep_header", metadata=header_metadata)

    def _prepare_sweep_fast_path(
        self,
        current_config: dict,
        plan: SweepPlan,
        params: AcquisitionStreamParams,
        timing: SweepTimingStats,
    ) -> tuple[dict, ValueTracker, bool]:
        """Prepare fast-path sweep execution for points after point zero.

        :param current_config: Current config after point-zero setup.
        :type current_config: dict
        :param plan: Sweep plan metadata.
        :type plan: SweepPlan
        :param params: Stream parameters resolved from point zero.
        :type params: AcquisitionStreamParams
        :param timing: Timing accumulator updated with prepare duration.
        :type timing: SweepTimingStats
        :return: Sweep config to mutate, value tracker, and prepare-called flag.
        :rtype: tuple[dict, ValueTracker, bool]
        """
        prepare_start_time = time.perf_counter()
        prepare_called = False

        if params.acquisition_indices:
            self._adapter.acquisition.prepare_sweep(params.mode, params.acquisition_indices)
            prepare_called = True

        sweep_config = current_config
        if not plan.has_envelope_vars:
            sweep_config.pop("envelopes", None)
        if not plan.has_waves_changes:
            sweep_config.pop("waves", None)

        tracker = ValueTracker()
        timing.prepare_sweep_ms = (time.perf_counter() - prepare_start_time) * 1000.0
        return sweep_config, tracker, prepare_called


__all__ = ["SweepRunner"]
