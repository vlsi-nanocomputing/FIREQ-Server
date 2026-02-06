"""Utility functions for wave compilation and envelope processing.

This module provides pure functions used by WaveEnvelopeOps:

- Wave entry construction and validation
- Wave compilation policy determination
- Envelope processing and validation
- Readout wave caching
"""

import numpy as np

from ....models.exceptions import ConfigurationError
from ..adapter_types import WaveEntry, same_spec
from .iq_conversion import iq_float_to_cint16

# =============================================================================
# Wave Entry Construction
# =============================================================================


def build_wave_entry(wave_spec: dict) -> WaveEntry:
    """Build a WaveEntry from wave specification.

    :param wave_spec: Wave specification dictionary with 'kind', 'wave_id',
        and kind-specific fields.
    :return: Constructed WaveEntry with all parameters but no WDW.
    :raises ConfigurationError: If specification is invalid.
    """
    kind = str(wave_spec.get("kind", "env")).lower()
    if kind not in ("env", "vz"):
        raise ConfigurationError(f"Unknown wave kind '{kind}' (use 'env' or 'vz').")

    if kind == "env":
        return WaveEntry(
            envelope=str(wave_spec["envelope"]),
            duration=int(wave_spec["duration"]),
            gain=float(wave_spec["gain"]),
            switch_iq=bool(wave_spec.get("switch_iq", False)),
            keep_last=bool(wave_spec.get("keep_last", False)),
            wdw=None,
        )
    else:
        if "vz_phase_rad" not in wave_spec:
            raise ConfigurationError(
                f"VZ wave '{wave_spec.get('wave_id')}' missing vz_phase_rad. " "Hint: provide vz_phase_rad (radians)."
            )
        phase = float(wave_spec["vz_phase_rad"])
        return WaveEntry(
            kind="vz",
            envelope="",
            duration=0,
            gain=0.0,
            switch_iq=False,
            keep_last=False,
            vz_phase_rad=phase,
            wdw=None,
        )


# =============================================================================
# Wave Compilation Policy
# =============================================================================


def check_wave_replacement_policy(
    wave_id: str,
    old_entry: WaveEntry | None,
    new_entry: WaveEntry,
    in_hw: bool,
    replace: bool,
) -> str:
    """Determine replacement policy: 'skip', 'replace', or 'add'.

    :param wave_id: The wave identifier.
    :param old_entry: Existing HL cache entry, or None.
    :param new_entry: New wave entry being compiled.
    :param in_hw: Whether wave exists in LL hardware memory.
    :param replace: Allow replacement flag.
    :return: Action string: 'skip', 'replace', or 'add'.
    :raises ConfigurationError: If replacement is needed but not allowed,
        or if cache/HW are inconsistent.
    """
    # SKIP EARLY (same spec, already compiled)
    if old_entry is not None and same_spec(old_entry, new_entry) and in_hw and (old_entry.wdw is not None):
        return "skip"

    # Replacement check
    if old_entry is not None and not same_spec(old_entry, new_entry) and not replace:
        raise ConfigurationError(
            f"wave_id '{wave_id}' already exists but spec differs. "
            f"OLD={old_entry} NEW={new_entry}. "
            f"Hint: set replace=True or use a different wave_id."
        )

    # HL-LL desynchronization guard
    if old_entry is None and in_hw and not replace:
        raise ConfigurationError(
            f"wave_id '{wave_id}' exists in HW but not in HL cache. "
            f"Hint: set replace=True to re-sync or rebuild HL cache."
        )

    return "replace" if in_hw else "add"


# =============================================================================
# Envelope Validation
# =============================================================================


def validate_envelope_spec(name: str) -> None:
    """Validate envelope specification (name validation).

    :param name: Envelope name to validate.
    :raises ConfigurationError: If name is empty or forbidden.
    """
    if not name:
        raise ConfigurationError("Envelope name is empty")
    if name.startswith("_"):
        raise ConfigurationError("Envelope Name forbidden : '_' is for reserved name")


def validate_envelope_symmetry(
    is_sym: bool,
    i_even: bool,
    q_even: bool,
    for_interp: bool,
) -> tuple[bool, bool]:
    """Validate and adjust symmetry flags.

    :param is_sym: Symmetry flag.
    :param i_even: I-component even flag.
    :param q_even: Q-component even flag.
    :param for_interp: Whether envelope is for interpolation.
    :return: Adjusted (i_even, q_even) flags.
    :raises ConfigurationError: If symmetry is invalid.
    """
    if not is_sym:
        i_even = False
        q_even = False
    elif is_sym and not for_interp:
        raise ConfigurationError(
            "Invalid envelope: the 'is_sym' flag is only for interpolated " "envelope.\nHint: set for_interp = True"
        )
    return i_even, q_even


def process_envelope_samples(
    samples_iq: np.ndarray,
    for_interp: bool,
    gen_sample_size: int,
    gen_num_channels: int,
    auto_pad: bool,
) -> tuple[np.ndarray, int]:
    """Convert float I/Q samples to cint16 and apply auto-padding if needed.

    :param samples_iq: Input I/Q samples (numpy array).
    :param for_interp: Whether this envelope is for interpolation.
    :param gen_sample_size: Generator sample size (bits per sample).
    :param gen_num_channels: Number of parallel channels in hardware.
    :param auto_pad: If True, auto-pad non-interpolated envelopes.
    :return: Tuple of (processed envelope data, original size before padding).
    """
    env = iq_float_to_cint16(samples_iq, gen_sample_size)
    original_size = int(env.size)

    if auto_pad and not for_interp:
        par = gen_num_channels
        r = int(env.size) % par
        if r != 0:
            env = np.pad(env, (0, par - r), mode="constant")

    return env, original_size


# =============================================================================
# Readout Wave Caching
# =============================================================================


def check_readout_wave_cache(
    gen_index: int,
    new_entry: WaveEntry,
    readout_wave_store: dict[int, WaveEntry],
    replace: bool,
) -> tuple[WaveEntry | None, str]:
    """Check readout wave cache and determine action.

    :param gen_index: Generator index.
    :param new_entry: New wave entry being uploaded.
    :param readout_wave_store: Readout wave cache dictionary.
    :param replace: Allow replacement flag.
    :return: Tuple of (old_entry, action) where action is 'skip', 'replace',
        or 'compile'.
    :raises ConfigurationError: If replacement is needed but not allowed.
    """
    old_entry = readout_wave_store.get(gen_index)

    if old_entry is not None and same_spec(old_entry, new_entry) and (old_entry.wdw is not None):
        return old_entry, "skip"

    if old_entry is not None and not same_spec(old_entry, new_entry) and not replace:
        raise ConfigurationError(
            f"Readout wave for gen_index={gen_index} already exists but "
            f"spec differs. OLD={old_entry} NEW={new_entry}. "
            f"Hint: set replace=True to overwrite."
        )

    return old_entry, "replace" if old_entry is not None else "compile"


__all__ = [
    "build_wave_entry",
    "check_wave_replacement_policy",
    "validate_envelope_spec",
    "validate_envelope_symmetry",
    "process_envelope_samples",
    "check_readout_wave_cache",
]
