# file: fireq-utils/server/execution/message_handler.py
"""Server-side message orchestration for FIREQ experiments."""

import logging
from collections.abc import Callable, Iterator

from ..models.queue_items import BinaryChunk, StreamHeader, StreamTiming
from .handlers import EnvelopeHandler, ResetHandler, StatusHandler, WaveHandler
from .hardware_config import HardwareConfigurator
from .streaming import AcquisitionStreamer
from .sweep_runner import SweepRunner
from .sweep_updates import SweepUpdateApplier


class MessageHandler:
    """Orchestrator for FIREQ experiment execution (single runs and sweeps)."""

    # =========================================================================
    #                           INITIALIZATION
    # =========================================================================

    def __init__(self, adapter: object, *, logger: logging.Logger | None = None) -> None:
        """Initialize with adapter and sub-handlers.

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

        self._hw_config = HardwareConfigurator(adapter, self.status_h, self.env_h, self.wave_h, logger=self.logger)
        self._streamer = AcquisitionStreamer(adapter, logger=self.logger)
        self._sweep_updates = SweepUpdateApplier(adapter, self.wave_h)
        self._sweep_runner = SweepRunner(
            adapter,
            self._hw_config,
            self._streamer,
            self._sweep_updates,
            logger=self.logger,
        )

    # =========================================================================
    #                             PUBLIC API
    # =========================================================================

    def cleanup(self) -> None:
        """Hardware cleanup for abnormal termination (e.g., client disconnect).

        Note: ``end_sweep()`` is NOT called here: the generator's ``finally``
        block in :meth:`run_sweep` handles it on all exit paths (normal,
        exception, ``GeneratorExit`` from disconnect).
        """
        try:
            self._hw_config.disable_acquisitions()
        except Exception as e:
            self.logger.warning(f"disable_acquisitions during cleanup failed: {e}")

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
            self._hw_config.apply_full_config(config, log=log)
            params = self._streamer.resolve_params(config)

            # Yield header with n_chunks included (header_binary protocol)
            header_metadata = self._streamer.build_metadata(
                params.acquisition_indices,
                params.mode,
                params.shots,
                params.samples_per_shot,
                config_log=log,
                n_chunks=params.n_chunks,
                stream_mode="header_binary",
            )
            header_metadata.update({"cmd": cmd, "session_id": session_id, "type": "experiment_header"})
            yield StreamHeader(type="experiment_header", metadata=header_metadata)

            # Stream binary-only chunks (no per-chunk JSON)
            if params.acquisition_indices:
                for chunk in self._streamer.stream_chunks(
                    acquisition_indices=params.acquisition_indices,
                    mode=params.mode,
                    shots=params.shots,
                    samp_per_shot=params.samples_per_shot,
                    timeout=params.timeout_s,
                    validate_chunk=True,
                ):
                    timing_stats = getattr(self.adapter.acquisition, "last_timing_stats", {})
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
                "debug_timing": dict(getattr(self.adapter.acquisition, "last_timing_stats", {})),
            }
            yield StreamTiming(type="experiment_timing", metadata=timing_metadata)

        except Exception as e:
            self.logger.exception("Experiment execution sequence aborted")
            error_metadata = self._streamer.build_metadata(
                [], "decimated", 0, 0, config_log=log, ok=False, error=str(e)
            )
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
        yield from self._sweep_runner.run(
            msg=msg,
            cmd=cmd,
            session_id=session_id,
            stop_check=stop_check,
        )


__all__ = ["MessageHandler"]
