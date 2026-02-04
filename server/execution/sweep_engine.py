# file: fireq-utils/server/sweep_engine.py
"""Sweep infrastructure for FIREQ experiments.

This module provides path-based navigation for sweep variable discovery
and substitution, enabling the sweep engine to:

1. Find all ``$variable`` placeholders in a nested dict/list config
2. Track their locations using simple tuple paths
3. Replace placeholder values with actual sweep point values

The ``SweepEngine`` class encapsulates sweep planning and fast-path task generation
for optimized multi-point experiments.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import product

import numpy as np

# ====================================================
#        TYPE ALIASES
# ====================================================

SimplePath = tuple[str | int, ...]
"""Simple path through a nested config: e.g., ("generators", 0, "drive", "frequency_mhz")"""


# ====================================================
#        VALUE CHANGE TRACKING
# ====================================================


class ValueTracker:
    """Tracks last-applied values for sweep fast-path optimization.

    Used to skip redundant hardware calls when sweep variable values
    are unchanged between consecutive sweep points.

    :ivar _cache: Internal cache mapping keys to their last-applied values.
    :vartype _cache: dict[tuple, object]
    """

    __slots__ = ("_cache",)

    def __init__(self) -> None:
        """Initialize an empty value tracker."""
        self._cache: dict[tuple, object] = {}

    def changed(self, key: tuple, new_value: object) -> bool:
        """Check if value changed and update cache.

        :param key: Unique identifier for the tracked value (e.g., ("gen", 0, "drive_mod")).
        :type key: tuple
        :param new_value: Current value to compare against cached value.
        :type new_value: object
        :return: True if value changed (or first call for this key), False otherwise.
        :rtype: bool
        """
        if key not in self._cache or self._cache[key] != new_value:
            self._cache[key] = new_value
            return True
        return False


def extract_mod_value(cfg: dict) -> tuple[float, float]:
    """Extract modulation comparison value from config.

    :param cfg: Configuration dict containing frequency_mhz and optional phase.
    :type cfg: dict
    :return: Tuple of (frequency_mhz, phase) for comparison.
    :rtype: tuple[float, float]
    """
    return (float(cfg["frequency_mhz"]), float(cfg.get("phase", 0.0)))


def make_hashable(obj: object) -> object:
    """Convert a nested structure to a hashable representation for comparison.

    :param obj: Any object (dict, list, or scalar).
    :type obj: object
    :return: Hashable representation (tuples for containers, scalars unchanged).
    :rtype: object
    """
    if isinstance(obj, dict):
        return tuple(sorted((k, make_hashable(v)) for k, v in obj.items()))
    if isinstance(obj, list):
        return tuple(make_hashable(item) for item in obj)
    return obj


# ====================================================
#        DECLARATIVE FLAG RULES
# ====================================================

# Maps (root_key, subsection, field_pattern) -> flag_name
# subsection=None means direct child of root
# field_pattern can be a string or tuple of strings
FLAG_RULES: dict[tuple, str] = {
    # Generator drive flags
    ("generators", "drive", ("frequency_mhz", "phase")): "drive_mod",
    ("generators", "drive", "nyquist_zone"): "drive_nyquist",
    ("generators", "drive", "channel"): "drive_channel",
    ("generators", "drive", ("fifo", "fifo_start_index")): "drive_fifo",
    # Generator readout flags
    ("generators", "readout", ("frequency_mhz", "phase")): "readout_mod",
    ("generators", "readout", "nyquist_zone"): "readout_nyquist",
    ("generators", "readout", "channel"): "readout_channel",
    ("generators", "readout", ("wave", "gain", "duration", "envelope", "switch_iq", "keep_last")): "readout_wave",
    # Waves section flags (top-level wave definitions)
    ("waves", None, ("gain", "duration", "envelope", "switch_iq", "keep_last", "wave_id", "kind")): "waves_compile",
    # Acquisition flags
    ("acquisitions", None, ("frequency_mhz", "phase")): "acq_mod",
    ("acquisitions", None, "channel"): "acq_channel",
    ("acquisitions", None, "duration"): "acq_duration",
    ("acquisitions", None, "tof"): "acq_tof",
    # Trigger flags
    ("trigger", None, "shots"): "trig_shots",
    ("trigger", None, "shot_duration"): "trig_shot_duration",
    ("trigger", "drive", "delay"): "trig_drive",
    ("trigger", "readout", "delay"): "trig_readout",
    ("trigger", None, "drive_start_index"): "trig_drive",
}


def _detect_flags_from_path(path: SimplePath) -> set[str]:
    """Determine which flags are affected by a single variable path.

    :param path: Simple tuple path like ("generators", 0, "drive", "frequency_mhz").
    :type path: SimplePath
    :return: Set of flag names like {"drive_mod"}.
    :rtype: set[str]
    """
    if len(path) < 2:
        return set()

    root = path[0]
    flags: set[str] = set()

    # Extract string keys from path (ignoring indices)
    str_keys = [p for p in path if isinstance(p, str)]
    if len(str_keys) < 2:
        return set()

    field = str_keys[-1]  # Last string key is the field name

    for pattern, flag in FLAG_RULES.items():
        pattern_root, subsection, field_pattern = pattern

        if pattern_root != root:
            continue

        # Check subsection match
        if subsection is not None:
            if subsection not in str_keys:
                continue

        # Check field match
        if isinstance(field_pattern, tuple):
            if field in field_pattern:
                flags.add(flag)
        elif field == field_pattern:
            flags.add(flag)

    return flags


# SweepFlags is a simple dict structure:
# {
#     "generators": {0: {"drive_mod", ...}, 1: {...}},  # gen_list_index -> flag set
#     "acquisitions": {0: {"acq_mod", ...}},            # acq_list_index -> flag set
#     "trigger": {"trig_shots", ...},                   # trigger flag set
#     "waves": {"waves_compile", ...},                  # waves section flag set
# }
SweepFlagsDict = dict[str, dict[int, set[str]] | set[str]]


def compute_sweep_flags(variable_paths: set[SimplePath]) -> SweepFlagsDict:
    """Compute all sweep flags from discovered variable paths in a single pass.

    :param variable_paths: Set of paths where sweep variables appear.
    :type variable_paths: set[SimplePath]
    :return: Dict with "generators", "acquisitions", "trigger", "waves" keys.
    :rtype: SweepFlagsDict
    """
    gen_flags: dict[int, set[str]] = {}
    acq_flags: dict[int, set[str]] = {}
    trig_flags: set[str] = set()
    waves_flags: set[str] = set()

    for path in variable_paths:
        if len(path) < 2:
            continue

        flags = _detect_flags_from_path(path)
        root = path[0]

        if root == "generators" and len(path) >= 2 and isinstance(path[1], int):
            idx = path[1]
            if idx not in gen_flags:
                gen_flags[idx] = set()
            gen_flags[idx].update(flags)

        elif root == "acquisitions" and len(path) >= 2 and isinstance(path[1], int):
            idx = path[1]
            if idx not in acq_flags:
                acq_flags[idx] = set()
            acq_flags[idx].update(flags)

        elif root == "trigger":
            trig_flags.update(flags)

        elif root == "waves":
            waves_flags.update(flags)

    return {
        "generators": gen_flags,
        "acquisitions": acq_flags,
        "trigger": trig_flags,
        "waves": waves_flags,
    }


# ====================================================
#        SWEEP PATH DISCOVERY
# ====================================================


def find_variable_paths(obj: object, var_names: set[str], path: SimplePath = ()) -> dict[str, set[SimplePath]]:
    """Discover where sweep variables are used inside a nested config structure.

    A "variable use" is detected when a string equals ``"$<name>"`` where ``name`` is
    one of ``var_names``. The function returns a mapping:

        var_name -> set(paths)

    where each path is a simple tuple of keys and indices.

    :param obj: Arbitrary nested structure (dict/list/scalars) representing the base config.
    :type obj: object
    :param var_names: Variable names without the ``$`` prefix.
    :type var_names: set[str]
    :param path: Internal recursion state (do not set manually).
    :type path: SimplePath
    :return: Map from variable name to the set of config paths where it appears.
    :rtype: dict[str, set[SimplePath]]
    """
    out: dict[str, set[SimplePath]] = {v: set() for v in var_names}

    if isinstance(obj, dict):
        for key, value in obj.items():
            sub = find_variable_paths(value, var_names, (*path, key))
            for v in var_names:
                out[v].update(sub[v])

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            sub = find_variable_paths(item, var_names, (*path, i))
            for v in var_names:
                out[v].update(sub[v])

    elif isinstance(obj, str):
        if obj.startswith("$"):
            clean_val = obj[1:]
            if clean_val in var_names:
                out[clean_val].add(path)

    return out


def _set_by_path(root: object, path: SimplePath, value: object) -> None:
    """Set a nested value by path (non-recursive).

    :param root: Root object (dict or list) to mutate.
    :type root: object
    :param path: Simple path tuple.
    :type path: SimplePath
    :param value: New value to set.
    :type value: object
    """
    if not path:
        raise ValueError("Empty path for _set_by_path")
    cur = root
    for token in path[:-1]:
        if isinstance(cur, list):
            cur = cur[int(token)]
        else:
            cur = cur[token]
    last = path[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def _extract_var_values(variables: list[dict]) -> tuple[list[str], list[list[object]]]:
    """Extract variable names and values from variable specifications.

    :param variables: List of variable specifications.
    :type variables: list[dict]
    :return: Tuple of (var_names, var_values) lists.
    :rtype: tuple[list[str], list[list[object]]]
    """
    var_names: list[str] = []
    var_values: list[list[object]] = []

    for v in variables:
        var_names.append(v["name"])

        if "values" in v:
            var_values.append(list(v["values"]))
        else:
            start = v["start"]
            stop = v["stop"]
            num = int(v["num"])
            space = v.get("space", "lin")

            if space != "lin":
                raise ValueError(f"Unknown/unsupported space='{space}'")

            axis = np.linspace(start, stop, num)
            var_values.append(axis.tolist())

    return var_names, var_values


def compute_sweep_size(variables: list[dict], mode: str = "cartesian") -> int:
    """Compute total sweep points without materializing them.

    :param variables: List of variable specifications.
    :type variables: list[dict]
    :param mode: "cartesian" or "zipped".
    :type mode: str
    :return: Total number of sweep points.
    :rtype: int
    :raises ValueError: If zipped lengths are inconsistent.
    """
    if not variables:
        return 1

    lengths = []
    for v in variables:
        if "values" in v:
            lengths.append(len(v["values"]))
        else:
            lengths.append(int(v["num"]))

    if mode == "cartesian":
        result = 1
        for n in lengths:
            result *= n
        return result
    if mode == "zipped":
        unique_lengths = set(lengths)
        if len(unique_lengths) > 1:
            raise ValueError("Zipped sweep requires equal-length variable lists")
        return lengths[0] if lengths else 0
    raise ValueError(f"Unknown sweep mode: {mode}")


def iter_sweep_points(
    variables: list[dict],
    mode: str = "cartesian",
) -> Iterator[dict]:
    """Generate sweep points lazily (iterator, not list).

    :param variables: List of variable specifications.
    :type variables: list[dict]
    :param mode: "cartesian" or "zipped".
    :type mode: str
    :yields: dict mapping variable names to values for each point.
    :raises ValueError: If ``mode`` is unknown or zipped lengths are inconsistent.
    """
    if not variables:
        yield {}
        return

    var_names, var_values = _extract_var_values(variables)

    if mode == "cartesian":
        for combo in product(*var_values):
            yield dict(zip(var_names, combo, strict=False))
    elif mode == "zipped":
        lengths = {len(vals) for vals in var_values}
        if len(lengths) > 1:
            raise ValueError("Zipped sweep requires equal-length variable lists")
        for values in zip(*var_values, strict=False):
            yield dict(zip(var_names, values, strict=False))
    else:
        raise ValueError(f"Unknown sweep mode: {mode}")


def generate_sweep_points(
    variables: list[dict],
    mode: str = "cartesian",
) -> list[dict[str, object]]:
    """Generate the list of sweep points from variable specifications.

    Supported variable formats
    --------------------------
    Each variable spec supports either:
    - explicit list: ``{"name": "X", "values": [...]}`` , or
    - linspace spec: ``{"name": "X", "start": a, "stop": b, "num": n}``.

    :param variables: List of variable specifications.
    :type variables: list[dict]
    :param mode: "cartesian" or "zipped".
    :type mode: str
    :return: List of points; each point is ``{var_name: value}``.
    :rtype: list[dict[str, object]]
    :raises ValueError: If ``mode`` is unknown or zipped lengths are inconsistent.

    .. note::
        For large sweeps, consider using :func:`iter_sweep_points` to avoid
        materializing all points in memory at once.
    """
    return list(iter_sweep_points(variables, mode))


# ====================================================
#        SWEEP PLAN DATA STRUCTURE
# ====================================================


@dataclass
class SweepPlan:
    """Sweep execution plan with computed properties.

    Contains all precomputed information needed to execute a sweep:
    - The variable specifications and sweep mode (for lazy point generation)
    - The precomputed total number of points
    - The paths where each variable is used in the config
    - Pre-computed flags indicating which hardware to reconfigure
    - Cached variable paths for efficient property lookups
    """

    variables: list[dict]
    sweep_mode: str
    n_points: int
    var_paths_by_name: dict[str, tuple[SimplePath, ...]]
    flags: SweepFlagsDict
    _variable_paths: frozenset[SimplePath]

    def iter_points(self) -> Iterator[dict]:
        """Lazy iterator over sweep points."""
        return iter_sweep_points(self.variables, self.sweep_mode)

    @property
    def has_envelope_vars(self) -> bool:
        """True if any sweep variable is in the 'envelopes' section."""
        return any(p[0] == "envelopes" for p in self._variable_paths)

    @property
    def has_timeout_var(self) -> bool:
        """True if any sweep variable path ends with 'timeout'."""
        return any(len(p) > 0 and p[-1] == "timeout" for p in self._variable_paths)

    @property
    def has_shots_var(self) -> bool:
        """True if any sweep variable is a 'shots' field in 'trigger'."""
        return any(len(p) > 0 and p[-1] == "shots" and p[0] == "trigger" for p in self._variable_paths)

    @property
    def has_duration_var(self) -> bool:
        """True if any sweep variable is a 'duration' field in 'acquisitions'."""
        return any(len(p) > 0 and p[-1] == "duration" and p[0] == "acquisitions" for p in self._variable_paths)

    @property
    def has_waves_section_vars(self) -> bool:
        """True if any sweep variable is directly in the 'waves' section."""
        return any(p[0] == "waves" for p in self._variable_paths)

    @property
    def has_waves_changes(self) -> bool:
        """True if waves section has variables."""
        return self.has_waves_section_vars


# ====================================================
#        FAST-PATH HELPERS
# ====================================================


def apply_gen_type(adapter: object, gi: int, cfg: dict, flags: set, ttype: str, tracker: ValueTracker) -> None:
    """Apply generator updates for a single type (drive or readout).

    Only calls adapter methods when values have actually changed from
    the previous sweep point, using the tracker for comparison.

    :param adapter: Hardware adapter.
    :type adapter: object
    :param gi: Generator index.
    :type gi: int
    :param cfg: Configuration dict for this type (drive or readout section).
    :type cfg: dict
    :param flags: Set of flags for this type.
    :type flags: set
    :param ttype: Type string ("drive" or "readout").
    :type ttype: str
    :param tracker: Value tracker for change detection.
    :type tracker: ValueTracker
    """
    prefix = f"{ttype}_"

    if f"{prefix}mod" in flags and "frequency_mhz" in cfg:
        val = extract_mod_value(cfg)
        if tracker.changed(("gen", gi, f"{prefix}mod"), val):
            adapter.generator.set_modulation(gi, ttype, {"frequency_mhz": val[0], "phase": val[1]})

    if f"{prefix}nyquist" in flags and "nyquist_zone" in cfg:
        val = int(cfg["nyquist_zone"])
        if tracker.changed(("gen", gi, f"{prefix}nyquist"), val):
            adapter.generator.set_nyquist_zone(gi, ttype, val)

    if f"{prefix}channel" in flags and "channel" in cfg:
        val = int(cfg["channel"])
        if tracker.changed(("gen", gi, f"{prefix}channel"), val):
            adapter.generator.set_trigger_listener(gi, {"ttype": ttype, "channel": val})

    # Type-specific final action
    if ttype == "drive" and "drive_fifo" in flags and "fifo" in cfg:
        val = (tuple(cfg["fifo"]), cfg.get("fifo_start_index", 1))
        if tracker.changed(("gen", gi, "drive_fifo"), val):
            adapter.generator.program_drive_sequence(gen_index=gi, wave_id_list=cfg["fifo"], start_index=val[1])

    elif ttype == "readout" and "readout_wave" in flags and "wave" in cfg:
        # Deep conversion to capture nested values (config is mutated in-place by apply_point)
        wave_cfg = cfg["wave"]
        val = make_hashable(wave_cfg)
        if tracker.changed(("gen", gi, "readout_wave"), val):
            adapter.generator.upload_readout_wave(gen_index=gi, wave=wave_cfg, replace=True)


# ====================================================
#        SWEEP PLANNING FUNCTIONS
# ====================================================


def plan_sweep(
    base_config: dict,
    variables: list[dict],
    sweep_mode: str,
) -> SweepPlan:
    """Create a sweep execution plan from base configuration and variables.

    :param base_config: Base experiment configuration with ``$var`` placeholders.
    :type base_config: dict
    :param variables: List of sweep variable specifications.
    :type variables: list[dict]
    :param sweep_mode: Sweep mode ("cartesian" or "zipped").
    :type sweep_mode: str
    :return: Sweep plan ready for execution.
    :rtype: SweepPlan
    :raises ValueError: If variables are invalid or not referenced in config.
    """
    if not variables:
        raise ValueError("Sweep requires at least one variable. Use run() for single experiments.")

    for var in variables:
        if not isinstance(var, dict):
            raise ValueError("Each sweep variable must be a dict")
        if "name" not in var:
            raise ValueError("Each sweep variable must include 'name'")

    var_names = {v["name"] for v in variables}
    var_to_paths = find_variable_paths(base_config, var_names)

    # Collect all variable paths
    variable_paths: set[SimplePath] = set()
    for ps in var_to_paths.values():
        variable_paths.update(ps)

    # Check all variables are referenced
    for name, paths in var_to_paths.items():
        if not paths:
            raise ValueError(f"Sweep variable '{name}' is not referenced in base config")

    # Compute flags in single pass using declarative rules
    flags = compute_sweep_flags(variable_paths)

    # Sort paths for deterministic order
    var_paths_by_name = {name: tuple(sorted(paths)) for name, paths in var_to_paths.items()}

    n_points = compute_sweep_size(variables, sweep_mode)

    return SweepPlan(
        variables=variables,
        sweep_mode=sweep_mode,
        n_points=n_points,
        var_paths_by_name=var_paths_by_name,
        flags=flags,
        _variable_paths=frozenset(variable_paths),
    )


def apply_sweep_point(
    config: dict,
    var_paths_by_name: dict[str, tuple[SimplePath, ...]],
    point: dict,
) -> None:
    """Apply sweep variable values to config at their discovered paths.

    :param config: Mutable experiment configuration.
    :type config: dict
    :param var_paths_by_name: Map from variable name to paths where it appears.
    :type var_paths_by_name: dict[str, tuple[SimplePath, ...]]
    :param point: Current sweep point values.
    :type point: dict
    """
    for name, paths in var_paths_by_name.items():
        value = point[name]
        for path in paths:
            _set_by_path(config, path, value)


__all__ = [
    # Type aliases
    "SimplePath",
    "SweepFlagsDict",
    # Flag computation
    "FLAG_RULES",
    "compute_sweep_flags",
    # Path utilities
    "find_variable_paths",
    # Sweep point generation
    "generate_sweep_points",
    "iter_sweep_points",
    "compute_sweep_size",
    # Sweep planning
    "SweepPlan",
    "plan_sweep",
    "apply_sweep_point",
    # Fast-path helpers
    "ValueTracker",
    "apply_gen_type",
    "extract_mod_value",
    "make_hashable",
]
