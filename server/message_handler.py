# file: fireq-utils/server/message_handler.py
"""Server-side message orchestration for FIREQ experiments.

This module translates high-level JSON-like experiment configurations into concrete
hardware actions through an adapter (``OverlayAdapter``). It provides:

- Result containers to standardize success/error reporting.
- Sweep utilities for variable substitution and point generation.
- Specialized handlers (status/reset/envelope/wave) to isolate concerns.
- A high-level ``MessageHandler`` that executes single experiments and optimized sweeps.

Design intent
-------------
The code favors execution stages (upload -> compile -> configure -> run),
and uses a sweep "fast path" to reduce repeated reconfiguration when only
numeric parameters change between points. Instead of reconfiguring each IP
for every sweeping point, the idea is to reconfigure only that specific
parameters actually changing, to speed up execution.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from itertools import product
from threading import Event

import numpy as np

# ====================================================
#        DATA STRUCTURES
# ====================================================


@dataclass
class HardwareStatusResult:
    """Structured status snapshot for a single generator.

    This is an object meant to return user-friendly status queries.

    Invariants
    ----------
    - When ``ok`` is True, the fields ``envelopes`` and ``waves_count`` reflect the current
    generator caches, and ``hw_summary`` is included for context/debugging.
    - When ``ok`` is False, ``error`` contains a human-readable failure reason and other
    fields may be partial defaults.

    Notes:
    -----
    The payload is intentionally JSON-friendly: it is designed to be sent over a network
    OR logged without carrying heavy binary buffers.
    """

    ok: bool
    gen_index: int
    envelopes: list[str]
    waves_count: int
    readout_wave: dict | None = None
    hw_summary: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        :return: Dict representation of the status.
        :rtype: dict
        """
        # Keep the status payload JSON-safe and lightweight with summaries only.

        return {
            "ok": self.ok,
            "gen_index": self.gen_index,
            "envelopes": self.envelopes,
            "waves_count": self.waves_count,
            "readout_wave": self.readout_wave,
            "hw_summary": self.hw_summary,
            "error": self.error,
        }


@dataclass
class ResetResult:
    """Outcome of a reset operation on a generator-owned memory region.

    Reset operations are used to recover from stale state (e.g., compiled waves referring
    to removed envelopes) or to enforce a clean execution environment for a new session.

    Fields
    ------
    - ``action`` identifies the reset type (e.g., wave_reset, envelope_reset).
    - ``details`` contains adapter-specific metadata for debugging (kept optional).
    """

    ok: bool
    gen_index: int
    action: str
    details: dict
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        :return: Dict representation of the reset outcome.
        :rtype: dict
        """
        return {
            "ok": self.ok,
            "gen_index": self.gen_index,
            "action": self.action,
            "details": self.details,
            "error": self.error,
        }


@dataclass
class EnvelopeResult:
    """Result of an envelope upload stage.

    The upload stage is separated from wave compilation because envelopes should be
    reused across many experiments/sweep points, and transferring large sample arrays is
    expensive compared to referencing cached envelope names.
    """

    ok: bool
    result: dict[int, dict[str, list[str]]]  # {gen_idx: {loaded, skipped, failed}}
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        :return: Dict representation of the upload result.
        :rtype: dict
        """
        return {
            "ok": self.ok,
            "result": {str(k): v for k, v in self.result.items()},
            "error": self.error,
        }


@dataclass
class WaveResult:
    """Result of a wave compilation stage.

    Wave compilation resolves references (e.g., envelope names) and produces/updates the
    hardware-side "wave definition words" (WDWs). On failure, ``error`` should explain
    the first blocking issue (e.g., missing envelope), and ``payload`` may contain per-gen
    details useful for debugging.
    """

    ok: bool
    payload: dict[int, dict[str, list[str]]]  # {gen_idx: {waves, replaced, skipped, failed}}
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        :return: Dict representation of the compilation result.
        :rtype: dict
        """
        return {
            "ok": self.ok,
            "payload": {str(k): v for k, v in self.payload.items()},
            "error": self.error,
        }


@dataclass
class ExperimentResult:
    """Result of a single experiment execution.

    This encapsulates the end-to-end outcome of ``MessageHandler.run()``:
    - optional preparation steps (envelope upload, wave compilation),
    - hardware configuration (generators/acquisitions/trigger),
    - acquisition execution and returned samples.

    ``config_log`` is designed as a review/debug aid: it captures high-level applied
    settings without requiring access to hardware internals.
    """

    ok: bool
    data: dict[int, np.ndarray] | None = None
    error: str | None = None
    config_log: list[str] | None = None

    def to_dict(self) -> dict:
        """Convert result to dictionary, formatting NumPy arrays for JSON.

        :return: Dict with I/Q data and execution logs.
        :rtype: dict
        """
        d = {"ok": self.ok}
        if self.ok and self.data is not None:
            d["data"] = {}
            for adc_idx, arr in self.data.items():
                if arr is not None:
                    d["data"][adc_idx] = {
                        "I": np.real(arr).tolist(),
                        "Q": np.imag(arr).tolist(),
                    }
        if self.error:
            d["error"] = self.error
        if self.config_log:
            d["config_log"] = self.config_log
        return d

    def to_metadata_dict(self) -> dict:
        """Generate metadata dictionary for binary transmission protocol.

        :return: Dict with metadata for binary reconstruction.
        :rtype: dict
        """
        d = {"ok": self.ok}
        if self.ok and self.data is not None:
            d["adc_metadata"] = {}
            for adc_idx, arr in self.data.items():
                if arr is not None:
                    d["adc_metadata"][adc_idx] = {
                        "dtype": str(arr.dtype),
                        "shape": list(arr.shape),
                    }
        else:
            d["adc_metadata"] = {}
        if self.error:
            d["error"] = self.error
        if self.config_log:
            d["config_log"] = self.config_log
        return d

    def get_binary_data(self) -> dict[int, np.ndarray]:
        """Return raw numpy arrays for binary transmission.

        :return: Dictionary mapping ADC index to numpy array.
        :rtype: Dict[int, np.ndarray]
        """
        return self.data if self.data else {}


@dataclass
class SweepPointResult:
    """Result emitted for each sweep point.

    The sweep loop reports progress incrementally through ``on_point`` so the caller can
    either stream the result or collect them in larger chunks.

    ``point`` stores the resolved variable values (already cast to int/float as required).
    """

    point_index: int
    n_total: int
    variables: dict[str, object]
    data: dict[int, np.ndarray]

    def to_dict(self) -> dict:
        """Convert sweep point data to a JSON-friendly dictionary."""
        d = {
            "point_index": self.point_index,
            "n_total": self.n_total,
            "variables": self.variables,
            "data": {},
        }
        for adc_idx, arr in self.data.items():
            if arr is not None:
                d["data"][adc_idx] = {
                    "I": np.real(arr).tolist(),
                    "Q": np.imag(arr).tolist(),
                }
        return d

    def to_metadata_dict(self) -> dict:
        """Generate metadata dictionary for binary transmission protocol.

        :return: Dict with sweep point info and array metadata.
        :rtype: dict
        """
        d = {
            "point_index": self.point_index,
            "n_total": self.n_total,
            "variables": self.variables,
            "adc_metadata": {},
        }
        for adc_idx, arr in self.data.items():
            if arr is not None:
                d["adc_metadata"][adc_idx] = {
                    "dtype": str(arr.dtype),
                    "shape": list(arr.shape),
                }
        return d

    def get_binary_data(self) -> dict[int, np.ndarray]:
        """Return raw numpy arrays for binary transmission.

        :return: Dictionary mapping ADC index to numpy array.
        :rtype: Dict[int, np.ndarray]
        """
        return self.data


@dataclass
class SweepStatus:
    """Final sweep summary.

    This is the "end-of-run" status of ``MessageHandler.run_sweep()`` and is meant to be
    small and robust: it reports whether the sweep completed successfully, how many points
    were requested vs completed, and the first blocking error if any.
    """

    ok: bool
    sweep_id: str
    n_points: int
    n_completed: int
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert sweep status to a dictionary."""
        return {
            "ok": self.ok,
            "sweep_id": self.sweep_id,
            "n_points": self.n_points,
            "n_completed": self.n_completed,
            "error": self.error,
        }


# ====================================================
#                 SWEEP HELPERS
# ====================================================


def find_variable_paths(obj: object, var_names: set[str], path: str = "") -> dict[str, set[str]]:
    """Discover where sweep variables are used inside a nested config structure.

    A "variable use" is detected when a string equals ``"$<name>"`` where ``name`` is
    one of ``var_names``. The function returns a mapping:

        var_name -> set(paths)

    where each path is a dot-separated string that may include list indices, e.g.
    ``"generators.0.frequency_mhz"``.

    Motivation
    ---------
    This function has optimization and speed up purposes.
    These paths are recomputed so the "sweep fast-path" can selectively reconfigure only
    the hardware blocks impacted by the variables, instead of re-running the full setup.

    :param obj: Arbitrary nested structure (dict/list/scalars) representing the base config.
    :type obj: object
    :param var_names: Variable names without the ``$`` prefix.
    :type var_names: set[str]
    :param path: Internal recursion state (do not set manually).
    :type path: str
    :return: Map from variable name to the set of config paths where it appears.
    :rtype: dict[str, set[str]]
    """
    out: dict[str, set[str]] = {v: set() for v in var_names}

    if isinstance(obj, dict):
        for key, value in obj.items():
            new_path = f"{path}.{key}" if path else key
            sub = find_variable_paths(value, var_names, new_path)
            for v in var_names:
                out[v].update(sub[v])

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            sub = find_variable_paths(item, var_names, f"{path}.{i}")
            for v in var_names:
                out[v].update(sub[v])

    # DESIGN NOTE:
    # Variables are represented as JSON-serializable strings ("$NAME") so that:
    # - configs remain portable (no Python-only objects),
    # - sweep substitution is explicit and inspectable,
    # - the same config can be logged/transmitted without custom encoders.
    elif isinstance(obj, str):
        if obj.startswith("$"):
            clean_val = obj[1:]  # Use slicing to remove exactly one '$'
            if clean_val in var_names:
                out[clean_val].add(path)

    return out


def classify_variable_paths(variable_paths: set[str]) -> dict[str, bool]:
    """Classify whether generators/acquisitions/trigger require reconfiguration in a sweep.

    The returned dict is used to decide whether the sweep loop can use a selective setup
    for subsequent points, or whether a full reconfiguration is needed.

    :param variable_paths: Flattened set of dot-paths where variables appear.
    :type variable_paths: set[str]
    :return: Flags indicating which subsystems are affected by the sweep variables.
    :rtype: dict[str, bool]
    """
    return {
        "generator": any(p.startswith("generators") for p in variable_paths),
        "acquisition": any(p.startswith("acquisitions") for p in variable_paths),
        "trigger": any(p.startswith("trigger") for p in variable_paths),
        "waves": any(p.startswith("waves") for p in variable_paths),
    }


def substitute_variables(config: dict, point: dict[str, object]) -> dict:
    """Create a point-specific config by replacing variable placeholders.

    This replaces any string leaf equal to ``"$VAR"`` with ``point["VAR"]``.

    Safety / expectations
    ---------------------
    - Intended for numeric fields (timings, gains, frequencies, etc.).
    - The base config should not use ``$``-prefixed strings for unrelated purposes,
    otherwise they will be substituted.

    :param config: Base experiment configuration containing ``"$VAR"`` placeholders.
    :type config: dict
    :param point: Concrete variable assignment for one sweep point.
    :type point: dict[str, object]
    :return: New config dict with placeholders replaced (original ``config`` unchanged).
    :rtype: dict
    """

    def substitute(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: substitute(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [substitute(item) for item in obj]
        elif isinstance(obj, str):
            if obj.startswith("$"):
                clean_val = obj[1:]  # Use slicing to remove exactly one '$'
                if clean_val not in point:
                    raise ValueError(f"Variable ${clean_val} not found in sweep point")
                return point[clean_val]
            return obj
        return obj

    return substitute(config)


def generate_sweep_points(
    variables: list[dict],
    mode: str = "cartesian",
    var_cast: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Generate the list of sweep points from variable specifications.

    Supported variable formats
    --------------------------
    Each variable spec supports either:
    - explicit list: ``{"name": "X", "values": [...]}`` , or
    - linspace spec: ``{"name": "X", "start": a, "stop": b, "num": n}``.

    Casting policy
    --------------
    The ``var_cast`` mapping controls whether each variable is coerced to int or float.
    This is critical because some hardware fields are discrete (cycle counts, indices)
    while others are continuous (gain, phase, frequency).

    Sweep modes
    -----------
    - ``cartesian``: full Cartesian product of all variable values.
    - ``zipped``: point-wise zip; all variables must produce the same number of values.

    Example:
    Given two variables to sweep, said x with points [1, 2, 3] and y with [a, b, c].
    - "Cartesian": sweep all the 9 "xy" possible configurations :
        [(1,a) , (1, b), (1,c),
         (2,a) , (2, b), (2,c),
         (1,a) , (1, b), (1,c) ]
    - "zipped" : pointwise sweep (Cartesian-mode main diagonal)
        [(1,a), (2,b) , (3,c)]

    :param variables: List of variable specifications.
    :type variables: list[dict]
    :param mode: "cartesian" or "zipped".
    :type mode: str
    :param var_cast: Map ``var_name -> "int"|"float"`` to enforce HW-appropriate types.
    :type var_cast: dict[str, str]
    :return: List of points; each point is ``{var_name: value}``.
    :rtype: list[dict[str, Any]]
    :raises ValueError: If ``mode`` is unknown or zipped lengths are inconsistent.
    """
    if not variables:
        return [{}]

    var_cast = var_cast or {}

    var_names = [v["name"] for v in variables]
    var_values: list[list[object]] = []

    for v in variables:
        name = v["name"]
        kind = var_cast.get(name, "float")  # "int" or "float"

        if "values" in v:
            vals = v["values"]
            if kind == "int":
                vals = [int(np.rint(x)) for x in vals]
            elif kind == "float":
                vals = [float(x) for x in vals]
            else:
                vals = list(vals)
            var_values.append(vals)
            continue

        # Nuova spec: server-side linspace
        start = v["start"]
        stop = v["stop"]
        num = int(v["num"])
        space = v.get("space", "lin")

        if space != "lin":
            raise ValueError(f"Unknown/unsupported space='{space}' for variable '{name}'")

        axis = np.linspace(start, stop, num)

        if kind == "int":
            axis = np.rint(axis).astype(int)  # Round to nearest integer
            vals = axis.tolist()
        else:
            vals = axis.astype(float).tolist()

        var_values.append(vals)

    # DESIGN NOTE:
    # cartesian explores the full combinatorial space; zipped enforces aligned dimensions.
    # Use both because they map to distinct experimental intents (grid search vs coordinated scan).
    if mode == "cartesian":
        return [dict(zip(var_names, combo, strict=False)) for combo in product(*var_values)]
    elif mode == "zipped":
        return [dict(zip(var_names, values, strict=False)) for values in zip(*var_values, strict=False)]
    else:
        raise ValueError(f"Unknown sweep mode: {mode}")


# ====================================================
#              SPECIALIZED HANDLERS
# ====================================================


class StatusHandler:
    """Status/inspection API over the hardware adapter.

    This handler exists to keep read-only operations separate from experiment execution,
    so status calls remain safe and do not accidentally mutate hardware state.

    It also caches the hardware summary because it is assumed immutable at runtime and
    is frequently used for handshake/status responses (e.g. opening single-client
    connections many times).
    """

    def __init__(self, adapter: object, logger: logging.Logger | None = None) -> None:
        """Initialize the status handler with an adapter."""
        self.adapter = adapter
        self.logger = logger or logging.getLogger(__name__)
        # Cache hardware summary: expected immutable at runtime and frequently reused
        # (handshake/status).
        self._hw_summary = adapter.summary()

    @property
    def hw_summary(self) -> dict:
        """Hardware summary for handshake."""
        return self._hw_summary

    @property
    def num_generators(self) -> int:
        """Return the number of generators reported by the hardware summary."""
        return self._hw_summary.get("num_generators", 0)

    @property
    def num_acquisitions(self) -> int:
        """Return the number of acquisitions reported by the hardware summary."""
        return self._hw_summary.get("num_acquisitions", 0)

    def get_all_generators_status(self) -> list[dict]:
        """Get status for ALL generators in one call.

        Useful for 'status' command without manual iteration.
        """
        statuses = []
        for gen_idx in range(self.num_generators):
            status = self.get_gen_status(gen_idx)
            statuses.append(status.to_dict())
        return statuses

    def get_gen_status(self, gen_index: int) -> HardwareStatusResult:
        """Retrieve the current state of a specific generator from hardware and cache.

        :param gen_index: Target generator index.
        :type gen_index: int
        :return: Structured hardware status.
        :rtype: HardwareStatusResult
        """
        try:
            envelopes = self.adapter.get_envelope_names(gen_index)
            # Expose only high-level cache metadata (counts/names), not samples.
            wave_cache = self.adapter.get_wave_cache(gen_index)
            readout_wave = self.adapter.get_readout_wave_cache(gen_index)

            ro_dict = readout_wave.__dict__ if readout_wave else None

            return HardwareStatusResult(
                ok=True,
                gen_index=gen_index,
                envelopes=envelopes,
                waves_count=len(wave_cache),
                readout_wave=ro_dict,
                hw_summary=self.adapter.summary,
            )
        except Exception as e:
            self.logger.error(f"Status check failed for gen {gen_index}: {e}")
            return HardwareStatusResult(ok=False, gen_index=gen_index, envelopes=[], waves_count=0, error=str(e))

    def get_system_info(self) -> dict:
        """Return hardware summary for handshake/status."""
        return self.adapter.summary


class ResetHandler:
    """Recovery-oriented reset operations for generator-owned memories.

    Separating reset logic from the main execution path makes it explicit the operation
    of discarding cached state (waves/envelopes) and why. This is useful both for safety
    and for reviewability.
    """

    def __init__(self, adapter: object, logger: logging.Logger | None = None) -> None:
        """Initialize the ResetHandler.

        :param adapter: OverlayAdapter instance.
        :type adapter: OverlayAdapter
        """
        self.adapter = adapter
        self.logger = logger or logging.getLogger(__name__)

    def reset_waves(self, gen_index: int, preserve_specs: bool = True) -> ResetResult:
        """Reset wave memory for a generator.

        :param gen_index: Target generator index.
        :type gen_index: int
        :param preserve_specs: If True, keeps definitions but invalidates compiled WDWs.
        :type preserve_specs: bool
        :return: Outcome of the wave reset.
        :rtype: ResetResult
        """
        try:
            res = self.adapter.reset_wave_memory(
                gen_index=gen_index,
                # "preserve_specs" keeps logical wave definitions while invalidating compiled
                # artifacts (WDWs). This enables fast recompilation without forcing the client
                # to rebuild the full registry.
                preserve_specs=preserve_specs,
            )
            return ResetResult(ok=True, gen_index=gen_index, action="wave_reset", details=res)
        except Exception as e:
            return ResetResult(
                ok=False,
                gen_index=gen_index,
                action="wave_reset",
                details={},
                error=str(e),
            )

    def reset_envelopes(self, gen_index: int) -> ResetResult:
        """Reset envelope memory for a generator.

        :param gen_index: Target generator index.
        :type gen_index: int
        :return: Outcome of the envelope reset.
        :rtype: ResetResult
        """
        try:
            res = self.adapter.reset_envelopes(gen_index=gen_index)
            return ResetResult(ok=True, gen_index=gen_index, action="envelope_reset", details=res)
        except Exception as e:
            return ResetResult(
                ok=False,
                gen_index=gen_index,
                action="envelope_reset",
                details={},
                error=str(e),
            )

    def reset_all_generators(self, preserve_wave_specs: bool = False) -> list[ResetResult]:
        """Reset waves and envelopes for ALL generators.

        Returns list of results (one per generator).
        """
        results = []
        summary = self.adapter.summary()
        num_gens = summary.get("num_generators", 0)
        for gen_idx in range(num_gens):
            wave_res = self.reset_waves(gen_idx, preserve_specs=preserve_wave_specs)
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
    """Envelope upload handler.

    Envelopes can be large (sample arrays) and are expensive to transfer. This stage is
    thus isolated so experiments and sweep points can reuse already-uploaded envelopes
    by name, minimizing I/O and latency.
    """

    def __init__(self, adapter: object, logger: logging.Logger | None = None) -> None:
        """Initialize the EnvelopeHandler.

        :param adapter: OverlayAdapter instance.
        :type adapter: OverlayAdapter
        """
        self.adapter = adapter
        self.logger = logger or logging.getLogger(__name__)

    def upload(
        self,
        config: dict,
        envelope_data: dict[tuple[int, int], np.ndarray] | None = None,
    ) -> EnvelopeResult:
        """Process the 'envelopes' section of the configuration.

        :param config: Dictionary containing envelope specifications (metadata).
        :type config: dict
        :param envelope_data: Binary envelope data mapping (gen_idx, env_idx) to float32
            I/Q arrays (shape: N×2). Required for envelope upload.
        :type envelope_data: Optional[Dict[Tuple[int, int], np.ndarray]]
        :return: Detailed result of the upload process.
        :rtype: EnvelopeResult
        """
        result: dict[int, dict[str, list[str]]] = {}

        try:
            envelopes_cfg = config.get("envelopes", {})
            if envelope_data is None:
                raise ValueError("Envelope upload requires binary frames with num_samples metadata.")
            for gen_index_str, envelopes in envelopes_cfg.items():
                gen_index = int(gen_index_str)

                # Inject binary data into envelope config if provided
                envelopes_with_samples = []
                for env_idx, e in enumerate(envelopes):
                    if "samples_iq" in e:
                        raise ValueError("Envelope metadata is invalid. Provide binary envelope frames.")
                    envelope = dict(e)  # Make a copy

                    # Get samples: binary data only.
                    if (gen_index, env_idx) in envelope_data:
                        envelope["samples_iq"] = envelope_data[(gen_index, env_idx)]
                    else:
                        error_msg = (
                            f"Missing binary data for envelope '{envelope.get('name', 'unknown')}' "
                            f"on gen {gen_index} (env_idx={env_idx})."
                        )
                        self.logger.error(error_msg)
                        result[gen_index] = {
                            "loaded": [],
                            "skipped": [],
                            "failed": [error_msg],
                        }
                        return EnvelopeResult(ok=False, result=result, error=error_msg)

                    envelopes_with_samples.append(envelope)

                # Upload is separated from compilation so
                # large envelope buffers are transferred at most once per session.
                res = self.adapter.upload_envelopes(
                    gen_index=gen_index,
                    envelopes=envelopes_with_samples,
                    auto_pad_noninterp=True,
                )

                # Build per-generator result
                gen_result = {
                    "loaded": res.get("loaded", []),
                    "skipped": res.get("skipped", []),
                    "failed": [],
                }

                if res.get("failed"):
                    gen_result["failed"] = [f"{f['name']}: {f['error']}" for f in res["failed"]]
                    failed = ", ".join(gen_result["failed"])
                    error_msg = f"Failed envelopes on gen {gen_index}: {failed}"
                    self.logger.error(error_msg)
                    result[gen_index] = gen_result
                    return EnvelopeResult(ok=False, result=result, error=error_msg)

                result[gen_index] = gen_result
                self.logger.info(f"Gen {gen_index}: loaded={gen_result['loaded']}, " f"skipped={gen_result['skipped']}")

            return EnvelopeResult(ok=True, result=result)
        except Exception as e:
            self.logger.exception("Envelope upload failed")
            return EnvelopeResult(ok=False, result=result, error=str(e))


class WaveHandler:
    """Wave compilation handler.

    This stage resolves envelope references and produces generator-side compiled wave
    descriptors. It is isolated because compilation failures should be reported with
    clear per-generator diagnostics.
    """

    def __init__(self, adapter: object, logger: logging.Logger | None = None) -> None:
        """Initialize the WaveHandler.

        :param adapter: OverlayAdapter instance.
        :type adapter: OverlayAdapter
        """
        self.adapter = adapter
        self.logger = logger or logging.getLogger(__name__)

    def compile(self, config: dict) -> WaveResult:
        """Process the 'waves' section of the configuration.

        :param config: Dictionary containing wave definitions and optional replace flag.
        :type config: dict
        :return: Detailed result of the compilation.
        :rtype: WaveResult
        """
        payload: dict[int, dict[str, list[str]]] = {}

        try:
            waves_cfg = config.get("waves", {})
            # Accept replace parameter from client config, default to True for backward compatibility
            replace = bool(config.get("replace", True))

            for gen_index_str, waves in waves_cfg.items():
                gen_index = int(gen_index_str)
                res = self.adapter.compile_waves(gen_index=gen_index, waves=waves, replace=replace)

                # Build per-generator payload
                gen_payload = {
                    "waves": [w.get("wave_id") for w in res.get("waves", [])],
                    "replaced": res.get("replaced", []),
                    "skipped": res.get("skipped", []),
                    "failed": [],
                }

                # Fail fast on missing dependencies (e.g., missing envelope) to avoid
                # running partially-defined hardware state.
                if res.get("failed"):
                    gen_payload["failed"] = [f"{f['wave_id']}: {f['error']}" for f in res["failed"]]
                    failed = ", ".join(gen_payload["failed"])
                    error_msg = f"Compilation failed on gen {gen_index}: {failed}"
                    payload[gen_index] = gen_payload
                    return WaveResult(ok=False, payload=payload, error=error_msg)

                payload[gen_index] = gen_payload
                self.logger.info(
                    f"Gen {gen_index}: compiled={gen_payload['waves']}, " f"replaced={gen_payload['replaced']}"
                )

            return WaveResult(ok=True, payload=payload)
        except Exception as e:
            self.logger.exception("Wave compilation failed")
            return WaveResult(ok=False, payload=payload, error=str(e))


# ====================================================
#              MAIN MESSAGE ORCHESTRATOR
# ====================================================


class MessageHandler:
    """High-level orchestrator for FIREQ experiment execution.

    This class provides two main entry points:
    - ``run``: execute a single experiment end-to-end.
    - ``run_sweep``: execute a multi-point sweep with an optimized "fast path".

    Architecture
    ------------
    The handler composes specialized sub-handlers (status/reset/envelope/wave) to keep
    responsibilities separated.

    Sweep "fast-path" contract
    ------------------------
    The sweep optimizer assumes that the *structure* of the experiment is unchanged across
    points (same number of generators/acquisitions, same routing/topology). Only numeric
    leaf parameters change via specific, early-declared variables.
    """

    # Field type classification for sweep variable casting
    # These define which hardware fields require integer vs float precision
    INT_FIELDS = frozenset(
        [
            "duration",
            "tof",
            "shots",
            "shot_duration",
            "channel",
            "nyquist_zone",
            "drive_start_index",
            "fifo_start_index",
            "delay",
        ]
    )
    FLOAT_FIELDS = frozenset(["frequency_mhz", "phase", "gain"])
    BOOL_FIELDS = frozenset(["keep_last"])
    TOP_LEVEL_KEYS = frozenset(["envelopes", "waves", "generators", "acquisitions", "trigger", "timeout"])
    SWEEP_MSG_KEYS = frozenset(["sweep_id", "variables", "sweep_mode", "base"])
    META_KEYS = frozenset(["cmd", "session_id"])
    SWEEP_META_KEYS = frozenset(["cmd", "session_id", "batch_size"])
    ENVELOPE_KEYS = frozenset(["name", "for_interpolation", "is_symmetric", "i_even", "q_even", "num_samples"])
    WAVE_ENV_KEYS = frozenset(["wave_id", "kind", "envelope", "duration", "gain", "switch_iq", "keep_last"])
    WAVE_VZ_KEYS = frozenset(["wave_id", "kind", "vz_phase_rad"])
    GENERATOR_KEYS = frozenset(["gen_index", "drive", "readout"])
    DRIVE_KEYS = frozenset(
        [
            "frequency_mhz",
            "phase",
            "nyquist_zone",
            "channel",
            "fifo",
            "fifo_start_index",
        ]
    )
    READOUT_KEYS = frozenset(["frequency_mhz", "phase", "nyquist_zone", "channel", "wave"])
    ACQUISITION_KEYS = frozenset(
        [
            "acq_index",
            "acquisition",
            "frequency_mhz",
            "phase",
            "channel",
            "duration",
            "tof",
            "output_type",
        ]
    )
    TRIGGER_KEYS = frozenset(["shots", "shot_duration", "drive", "readout", "drive_start_index"])
    TRIGGER_SPEC_KEYS = frozenset(["delay"])

    def __init__(self, adapter: object, *, logger: logging.Logger | None = None) -> None:
        """Initialize the orchestrator and its specialized sub-handlers.

        Sub-handlers are built once so they can reuse cached adapter information.

        :param adapter: Hardware adapter implementing the FIREQ control surface.
        :type adapter: OL_adapter
        :param logger: Optional logger used across all sub-handlers for consistent
            tracing.
        :type logger: logging.Logger | None
        """
        self.adapter = adapter
        self.logger = logger or logging.getLogger(__name__)

        # Composition: specialized custom handlers are initialized here
        self.status_h = StatusHandler(adapter, self.logger)
        self.reset_h = ResetHandler(adapter, self.logger)
        self.env_h = EnvelopeHandler(adapter, self.logger)
        self.wave_h = WaveHandler(adapter, self.logger)

        # Mapping from acq_index to DMA adc_index.
        # Keep identity here; DMA routing is hardcoded in the engine for this bitstream.
        self.acq_to_adc_mapping: dict[int, int] | None = None

    def _map_acq_to_adc(self, acq_index: int) -> int:
        """Map acquisition IP index to DMA ADC index.

        For firmware with shared DMA and AXI switch, the mapping may be inverted.
        If no mapping is configured, returns the acq_index unchanged (identity mapping).

        :param acq_index: Acquisition IP index from config.
        :type acq_index: int
        :return: Corresponding DMA ADC index.
        :rtype: int
        """
        if self.acq_to_adc_mapping is None:
            return acq_index
        return self.acq_to_adc_mapping.get(acq_index, acq_index)

    def _validate_keys(self, obj: dict, allowed: set[str], ctx: str) -> None:
        if not isinstance(obj, dict):
            raise ValueError(f"{ctx} must be a dict")
        unexpected = set(obj.keys()) - allowed
        if unexpected:
            raise ValueError(f"Unexpected keys in {ctx}: {sorted(unexpected)}")

    def _validate_config(self, config: dict) -> None:
        allowed = set(self.TOP_LEVEL_KEYS | self.META_KEYS)
        self._validate_keys(config, allowed, "config")

        envelopes_cfg = config.get("envelopes")
        if envelopes_cfg is not None:
            if not isinstance(envelopes_cfg, dict):
                raise ValueError("config.envelopes must be a dict")
            for gen_key, envelopes in envelopes_cfg.items():
                if not isinstance(envelopes, list):
                    raise ValueError(f"config.envelopes[{gen_key}] must be a list")
                for i, envelope in enumerate(envelopes):
                    self._validate_keys(
                        envelope,
                        set(self.ENVELOPE_KEYS),
                        f"config.envelopes[{gen_key}][{i}]",
                    )

        waves_cfg = config.get("waves")
        if waves_cfg is not None:
            if not isinstance(waves_cfg, dict):
                raise ValueError("config.waves must be a dict")
            for gen_key, waves in waves_cfg.items():
                if not isinstance(waves, list):
                    raise ValueError(f"config.waves[{gen_key}] must be a list")
                for i, wave in enumerate(waves):
                    kind = str(wave.get("kind", "env")).lower()
                    if kind not in ("env", "vz"):
                        raise ValueError(f"config.waves[{gen_key}][{i}] has unknown kind '{kind}'")
                    allowed = self.WAVE_ENV_KEYS if kind == "env" else self.WAVE_VZ_KEYS
                    self._validate_keys(
                        wave,
                        set(allowed),
                        f"config.waves[{gen_key}][{i}]",
                    )

        generators_cfg = config.get("generators")
        if generators_cfg is not None:
            if not isinstance(generators_cfg, list):
                raise ValueError("config.generators must be a list")
            for i, gen_cfg in enumerate(generators_cfg):
                self._validate_keys(gen_cfg, set(self.GENERATOR_KEYS), f"config.generators[{i}]")
                drive = gen_cfg.get("drive")
                if drive is not None:
                    self._validate_keys(drive, set(self.DRIVE_KEYS), f"config.generators[{i}].drive")
                readout = gen_cfg.get("readout")
                if readout is not None:
                    self._validate_keys(
                        readout,
                        set(self.READOUT_KEYS),
                        f"config.generators[{i}].readout",
                    )

        acquisitions_cfg = config.get("acquisitions")
        if acquisitions_cfg is not None:
            if not isinstance(acquisitions_cfg, list):
                raise ValueError("config.acquisitions must be a list")
            for i, acq_cfg in enumerate(acquisitions_cfg):
                self._validate_keys(acq_cfg, set(self.ACQUISITION_KEYS), f"config.acquisitions[{i}]")

        trigger_cfg = config.get("trigger")
        if trigger_cfg is not None:
            self._validate_keys(trigger_cfg, set(self.TRIGGER_KEYS), "config.trigger")
            for key in ("drive", "readout"):
                mapping = trigger_cfg.get(key)
                if mapping is None:
                    continue
                if not isinstance(mapping, dict):
                    raise ValueError(f"config.trigger.{key} must be a dict")
                for ch_key, spec in mapping.items():
                    self._validate_keys(
                        spec,
                        set(self.TRIGGER_SPEC_KEYS),
                        f"config.trigger.{key}[{ch_key}]",
                    )

    def _validate_sweep_message(self, msg: dict, allow_inline_config: bool) -> None:
        if not isinstance(msg, dict):
            raise ValueError("sweep message must be a dict")
        allowed = set(self.SWEEP_MSG_KEYS | self.SWEEP_META_KEYS)
        if allow_inline_config:
            allowed.update(self.TOP_LEVEL_KEYS)
        unexpected = set(msg.keys()) - allowed
        if unexpected:
            raise ValueError(f"Unexpected keys in sweep message: {sorted(unexpected)}")

    # =========================================================================
    #           EXPERIMENT EXECUTION METHODS
    # =========================================================================

    def run(self, config: dict) -> ExperimentResult:
        """Execute a single experiment configuration.

        Execution stages
        ----------------
        1) Optional memory preparation:
        - upload envelopes (if present)
        - compile waves (if present)

        2) Hardware configuration:
        - configure generators
        - configure acquisitions
        - configure trigger routing
        3) Acquisition run and data return.

        Partial configs
        ---------------
        The method supports partial configs to enable reuse of previously uploaded/compiled
        state. For example, omitting ``"envelopes"`` assumes they are already present on the
        hardware session.

        :param config: Full or partial experiment configuration dictionary.
        :type config: dict
        :return: ExperimentResult with acquired data on success, or error info on failure.
        :rtype: ExperimentResult
        """
        # Intentionally collect a concise, human-readable config_log instead of
        # dumping full configs.
        # This supports reproducibility and debugging without logging large buffers or
        # device-specific internals that would make reviews "noisy" and non-portable.
        log = []

        try:
            self._validate_config(config)
            # Stage 1: preparation steps, kept modular to allow caching across runs.
            # Preparation stages are optional by design to enable caching across runs:
            # - envelopes can be uploaded once and referenced by name;
            # - waves can be compiled once and reused as long as their dependencies do not change.
            # This keeps latency and bandwidth bounded when running repeated experiments.

            if "envelopes" in config:
                res = self.env_h.upload(config)
                if not res.ok:
                    raise Exception(f"Envelope preparation failed: {res.error}")

            if "waves" in config:
                res = self.wave_h.compile(config)
                if not res.ok:
                    raise Exception(f"Wave compilation failed: {res.error}")

            # Stage 2: IPs configuration
            for gen_cfg in config.get("generators", []):
                self._setup_generator(gen_cfg, log)

            self._disable_all_acquisitions(log)
            for acq_cfg in config.get("acquisitions", []):
                self._setup_acquisition(acq_cfg, log)
            self._disable_unused_acquisitions(config, log)

            trigger_cfg = config.get("trigger", {})
            self._setup_trigger(trigger_cfg, log)

            # 3. run the experiment and acquire data
            self._reset_dma_before_run()
            data = self._run_acquisition(config, log)

            return ExperimentResult(ok=True, data=data, config_log=log)

        except Exception as e:
            self.logger.exception("Experiment execution sequence aborted")
            return ExperimentResult(ok=False, error=str(e), config_log=log)

    def run_sweep(
        self,
        msg: dict,
        on_point: Callable[[SweepPointResult], None],
        stop_event: Event | None = None,
        on_plan: Callable[[list[dict[str, object]]], None] | None = None,
    ) -> SweepStatus:
        """Execute a multi-point sweep with an optimized "fast path".

        High-level algorithm
        --------------------
        - Parse sweep definition (base config + variables + mode).
        - Detect where variables appear in the config (paths).
        - Per-variable casting (int vs float) based on affected HW fields.
        - Run the first point with full preparation + full configuration.
        - Enter sweep mode (``adapter.prepare_sweep``) and for remaining points:
        selectively reconfigure only affected subsystems, then acquire.

        Key assumption
        --------------
        All sweep points share the same experiment topology (same generators/acquisitions/trigger
        structure). Only numeric leaf parameters are swept.

        :param msg: Sweep message containing ``base`` (or full config),
                    ``variables``, and ``sweep_mode``.
        :type msg: dict
        :param on_point: Callback invoked for each point with ``SweepPointResult``.
        :type on_point: Callable[[SweepPointResult], None]
        :param stop_event: Optional threading event to stop early.
        :type stop_event: threading.Event | None
        :return: Final sweep status summary.
        :rtype: SweepStatus
        :raises ValueError: If variable casting is ambiguous (touches both int and float fields).
        """
        sweep_id = msg.get("sweep_id", "unnamed")
        has_base = "base" in msg
        self._validate_sweep_message(msg, allow_inline_config=not has_base)
        base_config = msg.get("base", msg)
        self._validate_config(base_config)
        variables = msg.get("variables", [])
        sweep_mode = msg.get("sweep_mode", "cartesian")

        # Validate that sweep has at least one variable
        if not variables:
            raise ValueError("Sweep requires at least one variable. Use run() for single experiments.")

        var_names = {v["name"] for v in variables}

        # 1) Path per variable
        # Precompute variable usage paths once: enables selective reconfiguration
        # for the fast path.
        var_to_paths = find_variable_paths(base_config, var_names)

        # 2) Flatten for classify_variable_paths (which requires a set)
        variable_paths: set[str] = set()
        for ps in var_to_paths.values():
            variable_paths.update(ps)
        setup_needed = classify_variable_paths(variable_paths)

        # 3) Field-based casting enforces HW semantics:
        # timing/index fields are discrete ints, analog parameters are floats.
        gen_paths = {p for p in variable_paths if p.startswith("generators")}
        acq_paths = {p for p in variable_paths if p.startswith("acquisitions")}
        trig_paths = {p for p in variable_paths if p.startswith("trigger")}

        var_cast: dict[str, str] = {}
        for name, paths in var_to_paths.items():
            if not paths:
                self.logger.warning(f"Sweep variable '{name}' not used in base config")
                var_cast[name] = "float"
                continue

            # NOTE:
            # A single variable must not simultaneously drive discrete-time fields
            # (ints) and analog knobs (floats).
            # Enforcing this constraint avoids silent rounding/casting errors

            touches_int = False
            touches_float = False
            touches_bool = False
            for p in paths:
                last = p.split(".")[-1]
                if last in self.INT_FIELDS:
                    touches_int = True
                if last in self.FLOAT_FIELDS:
                    touches_float = True
                if last in self.BOOL_FIELDS:
                    touches_bool = True

            # A single variable must not drive both discrete and continuous fields:
            # that would be ambiguous and error-prone.
            if touches_bool and (touches_int or touches_float):
                raise ValueError(f"Variable '{name}' touches both bool and numeric fields: {sorted(paths)}")
            if touches_int and touches_float:
                raise ValueError(f"Variable '{name}' touches both int and float fields: {sorted(paths)}")

            if touches_int:
                var_cast[name] = "int"
            elif touches_float:
                var_cast[name] = "float"
            elif touches_bool:
                # Generate sweep points as ints (0/1), then coerce to bool.
                var_cast[name] = "int"
            else:
                # Field not in known classifications - default to float for safety
                self.logger.warning(
                    f"Variable '{name}' touches unclassified fields {sorted(paths)} - " f"defaulting to float casting"
                )
                var_cast[name] = "float"

        # 4) Generate sweep points with appropriate cast
        points = generate_sweep_points(variables, sweep_mode, var_cast)
        if self.BOOL_FIELDS:
            for p in points:
                for key in list(p.keys()):
                    if key in var_cast and var_cast[key] == "int":
                        # Only coerce fields declared as bool.
                        # This keeps other int fields untouched.
                        for path in var_to_paths.get(key, set()):
                            if path.split(".")[-1] in self.BOOL_FIELDS:
                                p[key] = bool(p[key])
                                break
        n_points = len(points)

        if on_plan is not None:
            on_plan(points)

        self.logger.info(f"Sweep '{sweep_id}': {n_points} points, setup_needed={setup_needed}")

        n_completed = 0
        # Avoid using logs for sweep experiments: there are too many points and
        # it would result in huge payload.
        log = None

        try:
            # 1. Materialize the first point config: this is the only point that
            # gets full preparation + full configuration.

            first_config = substitute_variables(base_config, points[0])

            if "envelopes" in first_config:
                res = self.env_h.upload(first_config)
                if not res.ok:
                    raise Exception(res.error)
            if "waves" in first_config:
                res = self.wave_h.compile(first_config)
                if not res.ok:
                    raise Exception(res.error)

            for gen_cfg in first_config.get("generators", []):
                self._setup_generator(gen_cfg, log)
            self._disable_all_acquisitions(log)
            for acq_cfg in first_config.get("acquisitions", []):
                self._setup_acquisition(acq_cfg, log)
            self._disable_unused_acquisitions(first_config, log)
            self._setup_trigger(first_config.get("trigger", {}), log)

            # Run first experiment with full validation
            self._reset_dma_before_run()
            data = self._run_acquisition(first_config, log)
            try:
                on_point(SweepPointResult(0, n_points, points[0], data))
            except Exception as cb_err:
                self.logger.error(f"Callback failed at point 0: {cb_err}")
                # Continue execution - callback failure should not abort sweep
            n_completed = 1

            if n_points == 1:
                return SweepStatus(True, sweep_id, n_points, n_completed)

            # 2. Prepare for optimized sweep
            # Enter sweep mode: subsequent points can reuse pre-validated acquisition
            # setup and reduce control overhead.
            adc_indices = self._get_adc_indices(first_config)
            mode = self._get_acq_mode(first_config)

            # "prepare_sweep()" switches the software into a state optimized for repeated points.
            # Therefore it is require to execute "end_sweep()" on every exit path
            # (success or exception) to avoid leaving the system in an ambiguous
            # execution mode for subsequent commands.
            self.adapter.prepare_sweep(mode, adc_indices)

            sweep_base = base_config
            if "envelopes" in sweep_base:
                sweep_base = {k: v for k, v in sweep_base.items() if k != "envelopes"}
            if (not setup_needed["waves"]) and "waves" in sweep_base:
                sweep_base = {k: v for k, v in sweep_base.items() if k != "waves"}

            # 3. Optimized loop over remaining points
            for i, point in enumerate(points[1:], start=1):
                if stop_event and stop_event.is_set():
                    self.logger.info(f"Sweep stopped at point {i}")
                    break
                # For each point, only variables change; rely on selective setup to
                # skip expensive full reconfiguration.
                config = substitute_variables(sweep_base, point)

                # Recompile waves if needed (WDW update)
                if setup_needed["waves"]:
                    if "waves" in config:
                        self.wave_h.compile(config)

                # Reconfigure only variable parameters
                if setup_needed["generator"]:
                    for gen_list_index, gen_cfg in enumerate(config.get("generators", [])):
                        local_gen_paths = {p for p in gen_paths if p.startswith(f"generators.{gen_list_index}.")}
                        if local_gen_paths:
                            self._setup_generator_selective(gen_cfg, local_gen_paths, log)

                if setup_needed["acquisition"]:
                    for acq_list_index, acq_cfg in enumerate(config.get("acquisitions", [])):
                        local_acq_paths = {p for p in acq_paths if p.startswith(f"acquisitions.{acq_list_index}.")}
                        if local_acq_paths:
                            self._setup_acquisition_selective(acq_cfg, local_acq_paths, log)

                if setup_needed["trigger"] and trig_paths:
                    self._setup_trigger_selective(config.get("trigger", {}), trig_paths, log)

                data = self._run_acquisition(config, log)
                try:
                    on_point(SweepPointResult(i, n_points, point, data))
                except Exception as cb_err:
                    self.logger.error(f"Callback failed at point {i}: {cb_err}")
                    # Continue execution - callback failure should not abort sweep
                n_completed += 1

            # Always close sweep mode to return the adapter/hardware to a clean state
            # for subsequent commands.
            self.adapter.end_sweep()
            return SweepStatus(True, sweep_id, n_points, n_completed)

        except Exception as e:
            self.logger.exception(f"Sweep '{sweep_id}' failed")
            # Best-effort cleanup: even on failure try to exit sweep mode to avoid
            # leaving hardware in a special state.
            try:
                self.adapter.end_sweep()
            except Exception as cleanup_err:
                self.logger.error(f"Failed to end sweep during cleanup: {cleanup_err}")
            return SweepStatus(False, sweep_id, n_points, n_completed, str(e))

    # =========================================================================
    # INTERNAL SETUP METHODS
    # =========================================================================

    def _setup_generator(self, gen_cfg: dict, log: list | None = None) -> None:
        """Configure a single generator from a config dictionary.

        This method applies generator-level configuration in a stable order:
        - modulation (DDS, phase/gain/frequency),
        - wave selection and compilation artifacts,
        - FIFO programming (when drive/readout pulses are scheduled).

        :param gen_cfg: Generator configuration dictionary (single generator).
                        Must contain "gen_index" and at least one of "drive"/"readout".
        :type gen_cfg: dict
        :param log: Optional list used to append human-readable configuration actions.
        :type log: list | None
        """
        if "gen_index" not in gen_cfg:
            raise KeyError("Generator config missing required key 'gen_index'")
        gen_index = gen_cfg["gen_index"]

        if "drive" not in gen_cfg and "readout" not in gen_cfg:
            raise KeyError("Generator config requires at least one of 'drive' or 'readout'")

        self.logger.info(f"Setting up generator {gen_index}")

        # Drive Path
        drive = gen_cfg.get("drive")
        if drive:
            if "frequency_mhz" in drive:
                self.adapter.generator_modulation(
                    gen_index,
                    "drive",
                    {
                        "frequency_mhz": float(drive["frequency_mhz"]),
                        "phase": float(drive.get("phase", 0.0)),
                    },
                )
                if log is not None:
                    log.append(f"gen {gen_index} drive frequency: {drive['frequency_mhz']} MHz")

            if "nyquist_zone" in drive:
                self.adapter.set_nyquist_zone(gen_index, "drive", int(drive["nyquist_zone"]))

            if "channel" in drive:
                self.adapter.gen_trigger2listen(gen_index, {"ttype": "drive", "channel": int(drive["channel"])})

            # FIFO programming is separated from modulation to keep waveform scheduling
            # independent from RF parameter setup.
            if "fifo" in drive:
                self.adapter.program_drive_sequence(
                    gen_index=gen_index,
                    wave_id_list=drive["fifo"],
                    start_index=drive.get("fifo_start_index", 1),
                )
                if log is not None:
                    log.append(f"gen {gen_index} drive sequence programmed")

        # Readout Path
        readout = gen_cfg.get("readout")
        if readout:
            if "frequency_mhz" in readout:
                self.adapter.generator_modulation(
                    gen_index,
                    "readout",
                    {
                        "frequency_mhz": float(readout["frequency_mhz"]),
                        "phase": float(readout.get("phase", 0.0)),
                    },
                )

            if "nyquist_zone" in readout:
                self.adapter.set_nyquist_zone(gen_index, "readout", int(readout["nyquist_zone"]))

            if "channel" in readout:
                self.adapter.gen_trigger2listen(gen_index, {"ttype": "readout", "channel": int(readout["channel"])})

            if "wave" in readout:
                self.adapter.upload_readout_wave(gen_index=gen_index, wave=readout["wave"], replace=True)
                if log is not None:
                    log.append(f"gen {gen_index} readout wave uploaded")

    def _setup_acquisition(self, acq_cfg: dict, log: list | None = None) -> None:
        """Configure a single acquisition block from a config dictionary.

        Acquisition is treated as independent from readout: an acquisition IP may be used for
        loopback tests or standalone capture. The config is expected to fully specify its routing
        (channel) and capture window.

        :param acq_cfg: Acquisition configuration dictionary (single acquisition).
        :type acq_cfg: dict
        :param log: Optional list used to append human-readable configuration actions.
        :type log: list | None
        """
        if "acq_index" not in acq_cfg:
            raise KeyError("Acquisition config missing required key 'acq_index'")
        acq_index = acq_cfg["acq_index"]

        # 1. Setup modulation (DDS and automatic Nyquist zone)
        if "frequency_mhz" in acq_cfg:
            self.adapter.acquisition_modulation(
                acq_index,
                {
                    "frequency_mhz": float(acq_cfg["frequency_mhz"]),
                    "phase": float(acq_cfg.get("phase", 0.0)),
                },
            )

        # 2. Setup Trigger Channel (Fixed: now passing a dict instead of int)
        if "channel" in acq_cfg:
            self.adapter.acq_trigger2listen(acq_index, {"ttype": "acquisition", "channel": int(acq_cfg["channel"])})
            if log is not None:
                log.append(f"acq {acq_index} listening to trigger channel {acq_cfg['channel']}")

        # 3. Setup Timing (ToF and integration duration)
        if "duration" in acq_cfg:
            tof = int(acq_cfg.get("tof", 0))
            self.adapter.acquisition_timing(acq_index, tof=tof, duration=int(acq_cfg["duration"]))
            if log is not None:
                log.append(f"acq {acq_index} timing set: tof={tof}")

    def _disable_unused_acquisitions(self, config: dict, log: list | None = None) -> None:
        """Disable trigger listening on acquisition IPs not used by the config.

        This avoids stale trigger routing when switching between experiments that
        use different acquisition sets.

        :param config: Experiment configuration with acquisition list.
        :type config: dict
        :param log: Optional list used to append human-readable actions.
        :type log: list | None
        :return: None
        :rtype: None
        """
        total = self.status_h.num_acquisitions
        if total <= 0:
            return
        used = {int(acq.get("acq_index", i)) for i, acq in enumerate(config.get("acquisitions", []))}
        for acq_index in range(total):
            if acq_index not in used:
                self.adapter.acq_trigger2listen(acq_index, {"ttype": "acquisition", "channel": 0})
                if log is not None:
                    log.append(f"acq {acq_index} disabled (trigger channel 0)")

    def _disable_all_acquisitions(self, log: list | None = None) -> None:
        """Disable trigger listening on all acquisition IPs.

        :param log: Optional list used to append human-readable actions.
        :type log: list | None
        :return: None
        :rtype: None
        """
        total = self.status_h.num_acquisitions
        if total <= 0:
            return
        for acq_index in range(total):
            self.adapter.acq_trigger2listen(acq_index, {"ttype": "acquisition", "channel": 0})
            if log is not None:
                log.append(f"acq {acq_index} disabled (trigger channel 0)")

    def _setup_generator_selective(self, gen_cfg: dict, variable_paths: set[str], log: list | None = None) -> None:
        """Reconfigure generator settings for sweep fast-path.

        Only the fields affected by ``variable_paths`` are re-applied. This reduces overhead
        compared to a full generator setup at every sweep point.

        Limitations
        -----------
        This method is intentionally conservative: if a sweep starts modifying structural fields
        (e.g., routing/channeling/Nyquist/topology), the fast-path should be extended or disabled
        in favor of full setup.

        :param gen_cfg: Generator configuration dictionary for the current point.
                        Must contain "gen_index". "drive" and/or "readout" are
                        required only if referenced by ``variable_paths``.
        :type gen_cfg: dict
        :param variable_paths: Set of dot-paths indicating which fields are variable-driven.
        :type variable_paths: set[str]
        :param log: Optional list used to append human-readable configuration actions.
        :type log: list | None
        """
        if "gen_index" not in gen_cfg:
            raise KeyError("Generator config missing required key 'gen_index'")
        gen_index = gen_cfg["gen_index"]

        needs_drive = any(
            p.endswith(".drive.frequency_mhz")
            or p.endswith(".drive.phase")
            or p.endswith(".drive.channel")
            or p.endswith(".drive.nyquist_zone")
            or p.endswith(".drive.fifo_start_index")
            or ".drive.fifo" in p
            for p in variable_paths
        )
        if needs_drive and "drive" not in gen_cfg:
            raise KeyError("Generator config missing required key 'drive'")

        needs_readout = any(
            p.endswith(".readout.frequency_mhz")
            or p.endswith(".readout.phase")
            or p.endswith(".readout.channel")
            or p.endswith(".readout.nyquist_zone")
            or ".readout.wave" in p
            for p in variable_paths
        )
        if needs_readout and "readout" not in gen_cfg:
            raise KeyError("Generator config missing required key 'readout'")

        drive = gen_cfg.get("drive")

        # Selective setup trades generality for speed: modify only subsystems
        # proven variable-driven by path analysis.
        if drive:
            if any(p.endswith(".drive.frequency_mhz") or p.endswith(".drive.phase") for p in variable_paths):
                self.adapter.generator_modulation(
                    gen_index,
                    "drive",
                    {
                        "frequency_mhz": float(drive["frequency_mhz"]),
                        "phase": float(drive.get("phase", 0.0)),
                    },
                )
            if any(p.endswith(".drive.nyquist_zone") for p in variable_paths) and "nyquist_zone" in drive:
                self.adapter.set_nyquist_zone(gen_index, "drive", int(drive["nyquist_zone"]))
            if any(p.endswith(".drive.channel") for p in variable_paths) and "channel" in drive:
                self.adapter.gen_trigger2listen(gen_index, {"ttype": "drive", "channel": int(drive["channel"])})
            if (
                any(".drive.fifo" in p or p.endswith(".drive.fifo_start_index") for p in variable_paths)
                and "fifo" in drive
            ):
                self.adapter.program_drive_sequence(
                    gen_index=gen_index,
                    wave_id_list=drive["fifo"],
                    start_index=drive.get("fifo_start_index", 1),
                )

        readout = gen_cfg.get("readout")
        if readout:
            if any(p.endswith(".readout.frequency_mhz") or p.endswith(".readout.phase") for p in variable_paths):
                self.adapter.generator_modulation(
                    gen_index,
                    "readout",
                    {
                        "frequency_mhz": float(readout["frequency_mhz"]),
                        "phase": float(readout.get("phase", 0.0)),
                    },
                )
            if any(p.endswith(".readout.nyquist_zone") for p in variable_paths) and "nyquist_zone" in readout:
                self.adapter.set_nyquist_zone(gen_index, "readout", int(readout["nyquist_zone"]))
            if any(p.endswith(".readout.channel") for p in variable_paths) and "channel" in readout:
                self.adapter.gen_trigger2listen(gen_index, {"ttype": "readout", "channel": int(readout["channel"])})
            if any(".readout.wave" in p for p in variable_paths) and "wave" in readout:
                self.adapter.upload_readout_wave(gen_index=gen_index, wave=readout["wave"], replace=True)

    def _setup_acquisition_selective(self, acq_cfg: dict, variable_paths: set[str], log: list | None = None) -> None:
        """Reconfigure acquisition settings for sweep fast-path.

        Re-apply only acquisition parameters that are variable-driven (e.g., delay/duration/shots),
        assuming routing/topology remains unchanged across points.

        :param acq_cfg: Acquisition configuration dictionary for the current point.
                        Must contain "acq_index". Other keys are required only if
                        referenced by ``variable_paths``.
        :type acq_cfg: dict
        :param variable_paths: Set of dot-paths indicating which acquisition fields
                               are variable-driven.
        :type variable_paths: set[str]
        :param log: Optional list used to append human-readable configuration actions.
        :type log: list | None
        """
        # PERFORMANCE/SAFETY TRADEOFF:
        # Selective setup is intentionally conservative: only subsystems proven
        # variable-driven are modified.
        # If new sweepable fields are added in the future, they must be explicitly handled here,
        # otherwise the fast-path may not reflect intended parameter changes.
        # Assumption: acquisition routing is stable across points. Changing
        # channel/routing mid-sweep can invalidate the preconfigured pipeline;
        # such changes should trigger a full reconfiguration.
        if "acq_index" not in acq_cfg:
            raise KeyError("Acquisition config missing required key 'acq_index'")
        acq_index = acq_cfg["acq_index"]

        needs_mod = any(p.endswith(".frequency_mhz") or p.endswith(".phase") for p in variable_paths)
        if needs_mod and "frequency_mhz" not in acq_cfg:
            raise KeyError("Acquisition config missing required key 'frequency_mhz'")
        if needs_mod:
            phase = float(acq_cfg.get("phase", 0.0))
            self.adapter.acquisition_modulation(
                acq_index,
                {"frequency_mhz": float(acq_cfg["frequency_mhz"]), "phase": phase},
            )
        if any(p.endswith(".channel") for p in variable_paths) and "channel" in acq_cfg:
            self.adapter.acq_trigger2listen(acq_index, {"ttype": "acquisition", "channel": int(acq_cfg["channel"])})
        needs_duration = any(p.endswith(".duration") for p in variable_paths)
        needs_tof = any(p.endswith(".tof") for p in variable_paths)
        if needs_duration and "duration" not in acq_cfg:
            raise KeyError("Acquisition config missing required key 'duration'")
        if needs_tof and "tof" not in acq_cfg:
            raise KeyError("Acquisition config missing required key 'tof'")
        if needs_duration or needs_tof:
            tof = int(acq_cfg.get("tof", 0))
            self.adapter.acquisition_timing(acq_index, tof=tof, duration=int(acq_cfg["duration"]))

    def _setup_trigger(self, trigger_cfg: dict, log: list | None = None) -> None:
        """Configure trigger routing and timing.

        Trigger configuration is performed after generators and acquisitions so that all
        involved endpoints (channels, indices) are already known and validated.

        :param trigger_cfg: Trigger configuration dictionary. "shots" is optional; when
            provided, value must be >= 1.
        :type trigger_cfg: dict
        :param log: Optional list used to append human-readable configuration actions.
        :type log: list | None
        """
        if not trigger_cfg:
            return

        shots = trigger_cfg.get("shots")
        if shots is not None and shots < 1:
            raise ValueError("The 'shots' key must be at least one.")

        if "shot_duration" in trigger_cfg:
            self.adapter.tg_set_duration(int(trigger_cfg["shot_duration"]))

        if "drive" in trigger_cfg or "readout" in trigger_cfg:
            self.adapter.tg_program_delays(
                drive=trigger_cfg.get("drive"),
                readout=trigger_cfg.get("readout"),
                drive_start_index=trigger_cfg.get("drive_start_index", 1),
            )
            if log is not None:
                if shots is None:
                    log.append("trigger delays programmed")
                else:
                    log.append(f"trigger delays programmed for {shots} shots")

    def _reset_dma_before_run(self) -> None:
        """Perform a minimal DMA reset before a new run/sweep point.

        :return: None
        :rtype: None
        """
        try:
            dma_engine = getattr(self.adapter, "dma_engine", None)
            if dma_engine is not None and hasattr(dma_engine, "abort"):
                self.logger.info("Resetting DMA before acquisition run.")
                dma_engine.abort()
        except Exception as e:
            self.logger.warning(f"DMA reset before run failed: {e}")

    def _setup_trigger_selective(self, trigger_cfg: dict, variable_paths: set[str], log: list | None = None) -> None:
        """Reconfigure trigger settings for sweep fast-path.

        Only re-apply trigger fields that are variable-driven to avoid reprogramming
        drive FIFOs when not needed.
        """
        if not trigger_cfg:
            return

        known_prefixes = (
            "trigger.shots",
            "trigger.shot_duration",
            "trigger.drive",
            "trigger.readout",
            "trigger.drive_start_index",
        )
        if any(p.startswith("trigger.") and not any(p.startswith(k) for k in known_prefixes) for p in variable_paths):
            # Conservative fallback: unknown trigger fields -> full setup.
            self._setup_trigger(trigger_cfg, log)
            return

        shots = trigger_cfg.get("shots")
        if shots is not None and shots < 1:
            raise ValueError("The 'shots' key must be at least one.")

        if any(p.endswith(".shot_duration") for p in variable_paths):
            if "shot_duration" not in trigger_cfg:
                raise KeyError("Trigger config missing required key 'shot_duration'")
            self.adapter.tg_set_duration(int(trigger_cfg["shot_duration"]))

        needs_drive = any(p.startswith("trigger.drive") for p in variable_paths) or any(
            p.endswith(".drive_start_index") for p in variable_paths
        )
        needs_readout = any(p.startswith("trigger.readout") for p in variable_paths)

        if needs_drive and "drive" not in trigger_cfg:
            raise KeyError("Trigger config missing required key 'drive'")
        if needs_readout and "readout" not in trigger_cfg:
            raise KeyError("Trigger config missing required key 'readout'")

        if needs_drive or needs_readout:
            self.adapter.tg_program_delays(
                drive=trigger_cfg.get("drive") if needs_drive else None,
                readout=trigger_cfg.get("readout") if needs_readout else None,
                drive_start_index=trigger_cfg.get("drive_start_index", 1),
            )
            if log is not None:
                log.append("trigger delays programmed (selective)")

    def _run_acquisition(self, config: dict, log: list | None = None) -> dict[int, np.ndarray]:
        """Run the acquisition sequence and return captured samples.

        This is the "data plane" step: it is expected to produce large numerical buffers and
        the only step that returns bulk data. All previous steps are control-plane.

        :param config: Full or partial experiment config for the current run/point.
        :type config: dict
        :param log: Optional list used to append human-readable actions.
        :type log: list | None
        :return: Map ``adc_index -> numpy array`` of acquired samples.
        :rtype: dict[int, numpy.ndarray]
        """
        acquisitions = config.get("acquisitions", [])
        if not acquisitions:
            if log is not None:
                log.append("No acquisitions configured; skipping acquisition run")
            return {}

        trigger_cfg = config.get("trigger", {})

        # Acquisition mode/ADC selection is derived from config.
        # Apply mapping from acq_index to DMA adc_index (may be inverted for AXI switch firmware).
        adc_indices = [
            self._map_acq_to_adc(acq.get("acq_index", acq.get("acquisition", i))) for i, acq in enumerate(acquisitions)
        ]
        if not adc_indices:
            adc_indices = [0]

        first_acq = acquisitions[0] if acquisitions else {}
        shots = trigger_cfg.get("shots", 1)
        if shots < 1:
            raise ValueError("The 'shots' key must be at least one.")
        results = self.adapter.run_multi_acquisition(
            adc_indices=adc_indices,
            mode=first_acq.get("output_type", "decimated"),
            shots=shots,
            samp_per_shot=int(first_acq.get("duration", 256)),
            timeout=config.get("timeout", 10.0),
        )
        if log is not None:
            log.append(f"Acquisition complete on ADCs: {adc_indices}")
        return results

    def _get_adc_indices(self, config: dict) -> list[int]:
        """Extract the ADC indices involved in the current experiment.

        This helper is used by sweep preparation to pre-configure the acquisition
        pipeline once (fast-path), avoiding repeated validation/initialization.

        :param config: Experiment configuration. Must contain non-empty "acquisitions"
            list.
        :type config: dict
        :return: List of ADC indices to be captured.
        :rtype: list[int]
        """
        if "acquisitions" not in config:
            raise KeyError("Experiment config missing required key 'acquisitions'")
        acquisitions = config["acquisitions"]
        if not acquisitions:
            raise ValueError("Experiment config requires at least one acquisition")
        return [self._map_acq_to_adc(acq.get("acq_index", i)) for i, acq in enumerate(acquisitions)]

    def _get_acq_mode(self, config: dict) -> str:
        """Determine the acquisition mode requested by the experiment.

        The mode is forwarded to the adapter so it can select the proper hardware
        capture path (e.g., standard vs sweep-optimized acquisition).

        :param config: Experiment configuration.
        :type config: dict
        :return: Acquisition mode identifier understood by the adapter.
        :rtype: str
        """
        acquisitions = config.get("acquisitions", [])
        if not acquisitions:
            return "decimated"
        return acquisitions[0].get("output_type", "decimated")
