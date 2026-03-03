# file: fireq-utils/server/execution/handlers.py
"""Specialized handlers for FIREQ operations.

Adapter wrappers for specific concerns:
- ``StatusHandler``: Read-only status/inspection operations
- ``ResetHandler``: Recovery-oriented reset operations
- ``EnvelopeHandler``: Envelope upload orchestration
- ``WaveHandler``: Wave compilation orchestration
"""

import logging

import numpy as np

from ..models.exceptions import EnvelopeUploadError, WaveCompilationError
from ..models.results import HardwareStatusResult, ResetResult


class StatusHandler:
    """Read-only operations, separated from experiment execution."""

    def __init__(self, adapter: object, logger: logging.Logger | None = None) -> None:
        """Initialize with adapter.

        :param adapter: OverlayAdapter instance.
        :type adapter: OverlayAdapter
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        """
        self.adapter = adapter
        self.logger = logger or logging.getLogger(__name__)
        self._hw_summary = adapter.summary()

    @property
    def hw_summary(self) -> dict:
        """Hardware summary for handshake.

        :return: Summary payload.
        :rtype: dict
        """
        return self._hw_summary

    @property
    def num_generators(self) -> int:
        """Number of generators.

        :return: Number of generators.
        :rtype: int
        """
        return self._hw_summary.get("num_generators", 0)

    @property
    def num_acquisitions(self) -> int:
        """Number of acquisitions.

        :return: Number of acquisitions.
        :rtype: int
        """
        return self._hw_summary.get("num_acquisitions", 0)

    def get_rf_mapping(self) -> dict:
        """Return RF topology mapping for generators/acquisitions.

        :return: Dict containing generator and acquisition RF mappings.
        :rtype: dict
        """
        return self.adapter.rf_mapping()

    def get_all_generators_status(self) -> list[dict]:
        """Get status for all generators.

        :return: List of generator status dicts.
        :rtype: list[dict]
        """
        statuses = []
        for gen_idx in range(self.num_generators):
            status = self.get_gen_status(gen_idx)
            statuses.append(status.to_dict())
        return statuses

    def get_gen_status(self, gen_index: int) -> HardwareStatusResult:
        """Retrieve the current state of a specific generator.

        :param gen_index: Target generator index.
        :type gen_index: int
        :return: Structured hardware status.
        :rtype: HardwareStatusResult
        """
        try:
            envelopes = self.adapter.generator.get_envelope_names(gen_index)
            wave_cache = self.adapter.generator.get_wave_cache(gen_index)
            readout_wave = self.adapter.generator.get_readout_wave_cache(gen_index)

            ro_dict = readout_wave.__dict__ if readout_wave else None

            return HardwareStatusResult(
                ok=True,
                gen_index=gen_index,
                envelopes=envelopes,
                waves_count=len(wave_cache),
                readout_wave=ro_dict,
                hw_summary=self.adapter.summary(),
            )
        except Exception as e:
            self.logger.error(f"Status check failed for gen {gen_index}: {e}")
            return HardwareStatusResult(ok=False, gen_index=gen_index, envelopes=[], waves_count=0, error=str(e))

    def get_system_info(self) -> dict:
        """Return hardware summary for handshake/status.

        :return: Summary payload.
        :rtype: dict
        """
        return self.adapter.summary()


class ResetHandler:
    """Recovery-oriented reset operations for generator-owned memories."""

    def __init__(self, adapter: object, logger: logging.Logger | None = None) -> None:
        """Initialize the ResetHandler.

        :param adapter: OverlayAdapter instance.
        :type adapter: OverlayAdapter
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        """
        self.adapter = adapter
        self.logger = logger or logging.getLogger(__name__)

    def reset_waves(self, gen_index: int, preserve_wave_specs: bool = True) -> ResetResult:
        """Reset wave memory for a generator.

        :param gen_index: Target generator index.
        :type gen_index: int
        :param preserve_wave_specs: If True, keeps definitions but invalidates compiled WDWs.
        :type preserve_wave_specs: bool
        :return: Outcome of the wave reset.
        :rtype: ResetResult
        """
        try:
            res = self.adapter.generator.reset_wave_memory(
                gen_index=gen_index,
                preserve_wave_specs=preserve_wave_specs,
            )
            return ResetResult(ok=True, gen_index=gen_index, action="wave_reset", details=res)
        except Exception as e:
            return ResetResult(ok=False, gen_index=gen_index, action="wave_reset", details={}, error=str(e))

    def reset_envelopes(self, gen_index: int) -> ResetResult:
        """Reset envelope memory for a generator.

        :param gen_index: Target generator index.
        :type gen_index: int
        :return: Outcome of the envelope reset.
        :rtype: ResetResult
        """
        try:
            res = self.adapter.generator.reset_envelopes(gen_index=gen_index)
            return ResetResult(ok=True, gen_index=gen_index, action="envelope_reset", details=res)
        except Exception as e:
            return ResetResult(ok=False, gen_index=gen_index, action="envelope_reset", details={}, error=str(e))

    def reset_all_generators(self, preserve_wave_specs: bool = False) -> list[dict]:
        """Reset waves and envelopes for ALL generators.

        :param preserve_wave_specs: Whether to preserve wave specs.
        :type preserve_wave_specs: bool
        :return: List of results (one per generator).
        :rtype: list[dict]
        """
        results = []
        summary = self.adapter.summary()
        num_gens = summary.get("num_generators", 0)
        for gen_idx in range(num_gens):
            wave_res = self.reset_waves(gen_idx, preserve_wave_specs=preserve_wave_specs)
            env_res = self.reset_envelopes(gen_idx)
            results.append(
                {
                    "gen_index": gen_idx,
                    "waves": wave_res.to_dict(),
                    "envelopes": env_res.to_dict(),
                }
            )

        return results


class EnvelopeHandler:
    """Envelope upload handler."""

    def __init__(self, adapter: object, logger: logging.Logger | None = None) -> None:
        """Initialize the EnvelopeHandler.

        :param adapter: OverlayAdapter instance.
        :type adapter: OverlayAdapter
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        """
        self.adapter = adapter
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def validate_metadata(msg: dict) -> tuple[int, bool]:
        """Validate envelope metadata and count envelopes.

        Called from receiver thread before binary frame reception.
        Checks that each envelope has 'num_samples' and no 'samples_iq'.

        :param msg: Message containing 'envelopes' section.
        :type msg: dict
        :return: Tuple of (total_envelope_count, has_invalid_metadata).
        :rtype: tuple[int, bool]
        """
        total, invalid = 0, False
        for envelopes in msg.get("envelopes", {}).values():
            for e in envelopes:
                total += 1
                if "samples_iq" in e or "num_samples" not in e:
                    invalid = True
        return total, invalid

    def upload(
        self,
        config: dict,
        envelope_data: dict[tuple[int, int], np.ndarray] | None = None,
    ) -> dict[int, dict[str, list[str]]]:
        """Process the 'envelopes' section of the configuration.

        :param config: Dictionary containing envelope specifications (metadata).
        :type config: dict
        :param envelope_data: Binary envelope data mapping (gen_idx, env_idx) to float32
            I/Q arrays (shape: N×2). Required for envelope upload.
        :type envelope_data: dict[tuple[int, int], np.ndarray] | None
        :return: Upload summary per generator with loaded/skipped envelope names.
        :rtype: dict[int, dict[str, list[str]]]
        :raises EnvelopeUploadError: If any envelope fails to upload.
        """
        result: dict[int, dict[str, list[str]]] = {}

        envelopes_cfg = config.get("envelopes", {})
        if envelope_data is None:
            raise EnvelopeUploadError(-1, "N/A", "Binary frames with num_samples metadata required")

        for gen_index_str, envelopes in envelopes_cfg.items():
            gen_index = int(gen_index_str)

            envelopes_with_samples = []
            for env_idx, e in enumerate(envelopes):
                if "samples_iq" in e:
                    raise EnvelopeUploadError(
                        gen_index, e.get("name", "unknown"), "Metadata invalid; provide binary frames"
                    )
                envelope = dict(e)
                envelope_name = envelope.get("name", "unknown")

                if (gen_index, env_idx) in envelope_data:
                    envelope["samples_iq"] = envelope_data[(gen_index, env_idx)]
                else:
                    raise EnvelopeUploadError(gen_index, envelope_name, f"Missing binary data (env_idx={env_idx})")

                envelopes_with_samples.append(envelope)

            res = self.adapter.generator.upload_envelopes(
                gen_index=gen_index,
                envelopes=envelopes_with_samples,
                auto_pad_noninterp=True,
            )

            gen_result = {
                "loaded": res.get("loaded", []),
                "skipped": res.get("skipped", []),
            }

            if res.get("failed"):
                first_fail = res["failed"][0]
                raise EnvelopeUploadError(gen_index, first_fail["name"], first_fail["error"])

            result[gen_index] = gen_result
            self.logger.debug(f"Gen {gen_index}: loaded={gen_result['loaded']}, skipped={gen_result['skipped']}")

        return result


class WaveHandler:
    """Wave compilation handler."""

    def __init__(self, adapter: object, logger: logging.Logger | None = None) -> None:
        """Initialize the WaveHandler.

        :param adapter: OverlayAdapter instance.
        :type adapter: OverlayAdapter
        :param logger: Optional logger instance.
        :type logger: logging.Logger | None
        """
        self.adapter = adapter
        self.logger = logger or logging.getLogger(__name__)

    def compile(self, config: dict) -> dict[int, dict[str, list[str]]]:
        """Process the 'waves' section of the configuration.

        :param config: Dictionary containing wave definitions and optional replace flag.
        :type config: dict
        :return: Compilation summary per generator with wave IDs, replaced, and skipped lists.
        :rtype: dict[int, dict[str, list[str]]]
        :raises WaveCompilationError: If any wave fails to compile.
        """
        payload: dict[int, dict[str, list[str]]] = {}

        waves_cfg = config.get("waves", {})
        for gen_index_str, waves in waves_cfg.items():
            gen_index = int(gen_index_str)
            res = self.adapter.generator.compile_waves(gen_index=gen_index, waves=waves, replace=True)

            gen_payload = {
                "waves": [w.get("wave_id") for w in res.get("waves", [])],
                "replaced": res.get("replaced", []),
                "skipped": res.get("skipped", []),
            }

            if res.get("failed"):
                first_fail = res["failed"][0]
                raise WaveCompilationError(gen_index, first_fail["wave_id"], first_fail["error"])

            payload[gen_index] = gen_payload
            self.logger.debug(f"Gen {gen_index}: compiled={gen_payload['waves']}, replaced={gen_payload['replaced']}")

        return payload


__all__ = [
    "StatusHandler",
    "ResetHandler",
    "EnvelopeHandler",
    "WaveHandler",
]
