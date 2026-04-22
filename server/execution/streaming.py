# file: fireq-utils/server/execution/streaming.py
"""Acquisition streaming for FIREQ experiments.

Encapsulates stream parameter resolution, chunk-count computation,
metadata building, and acquisition data emission.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from ..models.queue_items import BinaryChunk
from .sweep_planning import SweepPlan


@dataclass(frozen=True, slots=True)
class AcquisitionStreamParams:
    """Resolved acquisition streaming parameters.

    Bundles acquisition index normalization, stream-parameter extraction,
    and chunk-count computation into a single immutable container.

    :param acquisition_indices: Active acquisition IP indices.
    :type acquisition_indices: list[int]
    :param mode: Acquisition output mode (``decimated``, ``accumulated``, ``raw``).
    :type mode: str
    :param shots: Number of trigger shots.
    :type shots: int
    :param samples_per_shot: Samples captured per shot.
    :type samples_per_shot: int
    :param timeout_s: Acquisition timeout in seconds.
    :type timeout_s: float
    :param n_chunks: Number of hardware buffer chunks per acquisition.
    :type n_chunks: int
    """

    acquisition_indices: list[int]
    mode: str
    shots: int
    samples_per_shot: int
    timeout_s: float
    n_chunks: int


class AcquisitionStreamer:
    """Resolves stream parameters, builds metadata, and emits acquisition data.

    :param adapter: Hardware adapter implementing the FIREQ control surface.
    :type adapter: object
    :param logger: Optional logger for consistent tracing.
    :type logger: logging.Logger | None
    """

    def __init__(self, adapter: object, *, logger: logging.Logger | None = None) -> None:
        """Initialize with explicit dependencies.

        :param adapter: Hardware adapter implementing the FIREQ control surface.
        :type adapter: object
        :param logger: Optional logger for consistent tracing.
        :type logger: logging.Logger | None
        """
        self._adapter = adapter
        self._logger = logger or logging.getLogger(__name__)

    # =========================================================================
    #                             PUBLIC API
    # =========================================================================

    def resolve_params(self, config: dict) -> AcquisitionStreamParams:
        """Resolve acquisition streaming parameters from a configured experiment.

        Combines acquisition index normalization, stream parameter extraction,
        and chunk-count computation into a single frozen result.

        :param config: Experiment configuration (already applied to hardware).
        :type config: dict
        :return: Resolved streaming parameters.
        :rtype: AcquisitionStreamParams
        """
        acquisition_indices = self._normalize_acquisition_configs(config)
        acquisition_indices, mode, shots, samples_per_shot, timeout_s = self._extract_acquisition_stream_params(
            config, acquisition_indices
        )

        n_chunks = 1
        if acquisition_indices and shots > 0:
            max_hw_shots = min(
                self._adapter.acquisition.compute_max_hw_shots(mode, samples_per_shot, acquisition_ip_index)
                for acquisition_ip_index in acquisition_indices
            )
            n_chunks = (shots + max_hw_shots - 1) // max_hw_shots if max_hw_shots > 0 else 1

        return AcquisitionStreamParams(
            acquisition_indices=acquisition_indices,
            mode=mode,
            shots=shots,
            samples_per_shot=samples_per_shot,
            timeout_s=timeout_s,
            n_chunks=n_chunks,
        )

    @staticmethod
    def resolve_sweep_point_params(
        sweep_config: dict,
        plan: SweepPlan,
        base_params: AcquisitionStreamParams,
    ) -> tuple[int, int, float]:
        """Resolve per-point stream parameters for a sweep iteration.

        For swept fields (shots, duration, timeout), reads the current value
        from the mutated sweep config. For non-swept fields, falls back to
        the base parameters established during initial setup.

        :param sweep_config: Current experiment config (mutated by apply_sweep_point).
        :type sweep_config: dict
        :param plan: Sweep plan with variable metadata.
        :type plan: SweepPlan
        :param base_params: Stream parameters from initial setup.
        :type base_params: AcquisitionStreamParams
        :return: Resolved (shots, samples_per_shot, timeout_s).
        :rtype: tuple[int, int, float]
        """
        shots = int(sweep_config["trigger"]["shots"]) if plan.has_shots_var else base_params.shots
        samples_per_shot = (
            int(sweep_config["acquisitions"][0]["duration"]) if plan.has_duration_var else base_params.samples_per_shot
        )
        timeout_s = float(sweep_config["timeout"]) if plan.has_timeout_var else base_params.timeout_s
        return shots, samples_per_shot, timeout_s

    def build_metadata(
        self,
        acquisition_indices: list[int],
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

        :param acquisition_indices: Acquisition IP indices to capture.
        :type acquisition_indices: list[int]
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
        acquisition_ip_metadata: dict[int, dict[str, object]] = {}
        for acquisition_ip_index in acquisition_indices:
            shape = self._compute_expected_shape(mode, shots, samp_per_shot, acquisition_ip_index)
            dtype = "iq_int32" if mode == "accumulated" else "iq_int16"
            acquisition_ip_metadata[acquisition_ip_index] = {"dtype": dtype, "shape": shape}
        payload: dict[str, object] = {
            "ok": ok,
            "acq_ip_metadata": acquisition_ip_metadata,
            "n_chunks": n_chunks,
            "stream_mode": stream_mode,
        }
        if config_log is not None:
            payload["config_log"] = config_log
        if error:
            payload["error"] = error
        return payload

    def stream_chunks(
        self,
        *,
        acquisition_indices: list[int],
        mode: str,
        shots: int,
        samp_per_shot: int,
        timeout: float,
        validate_chunk: bool = True,
    ) -> Iterator[dict[int, np.ndarray]]:
        """Stream acquisition chunks using run_multi_acquisition().

        :param acquisition_indices: Acquisition IP indices to capture.
        :type acquisition_indices: list[int]
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
        if not acquisition_indices or shots <= 0:
            return

        yield from self._adapter.acquisition.run_multi_acquisition(
            acq_indices=acquisition_indices,
            mode=mode,
            shots=shots,
            samp_per_shot=samp_per_shot,
            timeout=timeout,
            validate_chunk=validate_chunk,
        )

    def stream_sweep_point(
        self,
        acquisition_indices: list[int],
        mode: str,
        shots: int,
        samp_per_shot: int,
        timeout: float,
        validate: bool = True,
    ) -> Iterator[BinaryChunk]:
        """Stream acquisition for one sweep point, yielding BinaryChunk items.

        :param acquisition_indices: Acquisition IP indices to capture.
        :type acquisition_indices: list[int]
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
        for chunk in self.stream_chunks(
            acquisition_indices=acquisition_indices,
            mode=mode,
            shots=shots,
            samp_per_shot=samp_per_shot,
            timeout=timeout,
            validate_chunk=validate,
        ):
            timing_stats = getattr(self._adapter.acquisition, "last_timing_stats", {})
            yield BinaryChunk(
                type="sweep_binary_point",
                binary_data=chunk,
                timing=(
                    timing_stats.get("fpga_wait_ms", 0.0),
                    timing_stats.get("sw_overhead_ms", 0.0),
                ),
            )

    # =========================================================================
    #                          PRIVATE HELPERS
    # =========================================================================

    def _normalize_acquisition_configs(self, config: dict) -> list[int]:
        """Extract acquisition indices from config, excluding deaf acquisitions.

        :param config: Experiment configuration.
        :type config: dict
        :return: Acquisition IP indices to capture (only active ones with channel != 0).
        :rtype: list[int]
        """
        acquisitions = config.get("acquisitions", [])
        all_indices = [acquisition["acq_index"] for acquisition in acquisitions]

        # Filter out deaf acquisitions (channel == 0)
        active_indices = [
            acquisition_index
            for acquisition_index in all_indices
            if self._adapter.acquisition.acq_trigger_channels.get(acquisition_index, 0) != 0
        ]

        if len(active_indices) < len(all_indices):
            deaf = set(all_indices) - set(active_indices)
            self._logger.info(
                f"_normalize_acquisition_configs: filtered deaf acquisition(s) {deaf}, active={active_indices}"
            )
        else:
            self._logger.debug(
                f"_normalize_acquisition_configs: {len(acquisitions)} acquisition(s) -> indices={active_indices}"
            )

        return active_indices

    @staticmethod
    def _extract_acquisition_stream_params(
        config: dict,
        acquisition_indices: list[int] | None = None,
    ) -> tuple[list[int], str, int, int, float]:
        """Extract acquisition stream parameters from config.

        :param config: Experiment configuration.
        :type config: dict
        :param acquisition_indices: Optional precomputed acquisition IP indices.
        :type acquisition_indices: list[int] | None
        :return: Tuple (acquisition_indices, mode, shots, samp_per_shot, timeout).
        :rtype: tuple[list[int], str, int, int, float]
        """
        acquisitions = config.get("acquisitions", [])
        if not acquisitions:
            return [], "decimated", 0, 0, float(config.get("timeout", 10.0))

        if acquisition_indices is None:
            acquisition_indices = [acquisition["acq_index"] for acquisition in acquisitions]
        mode = acquisitions[0].get("output_type", "decimated")
        trigger_cfg = config.get("trigger", {})
        shots = int(trigger_cfg.get("shots", 1))
        samp_per_shot = int(acquisitions[0].get("duration", 256))
        timeout = float(config.get("timeout", 10.0))
        return acquisition_indices, mode, shots, samp_per_shot, timeout

    def _compute_expected_shape(
        self,
        mode: str,
        shots: int,
        samp_per_shot: int,
        acquisition_index: int,
    ) -> list[int]:
        """Compute the expected array shape for a given acquisition ip.

        :param mode: Acquisition mode.
        :type mode: str
        :param shots: Number of shots.
        :type shots: int
        :param samp_per_shot: Samples per shot.
        :type samp_per_shot: int
        :param acquisition_index: Acquisition IP index.
        :type acquisition_index: int
        :return: Expected array shape.
        :rtype: list[int]
        """
        if mode == "accumulated":
            return [int(shots)]
        if mode == "decimated":
            return [int(shots), int(samp_per_shot)]
        if mode == "raw":
            parallelism = int(self._adapter.hw_specs["acquisitions"][acquisition_index].get("parallelism", 1))
            return [int(shots), int(samp_per_shot) * parallelism]
        return [int(shots), int(samp_per_shot)]


__all__ = ["AcquisitionStreamer", "AcquisitionStreamParams"]
