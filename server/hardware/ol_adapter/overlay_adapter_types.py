"""Adapter-specific types and utilities for hardware operation classes.

This module is the single source of truth for the OverlayAdapter's domain
types, validation logic, and signal-processing helpers:

- **Types**: EnvelopeSpec, ReadoutWaveSpec, WaveEntry, WaveKind
- **Protocol**: parse_bool_flag
- **Validation**: envelope spec/symmetry, FIFO capacity, wave-ID checks
- **Signal processing**: IQ quantization (float → cint16), envelope padding
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

import numpy as np

from ...models.exceptions import ConfigurationError

# ============================================================================
# Protocol helpers
# ============================================================================


def parse_bool_flag(value: object) -> bool:
    """Parse a string boolean flag from protocol messages.

    Returns ``True`` only when *value* is exactly the string ``"True"``.
    Every other input (``None``, ``"False"``, ``""``, ``"true"``, etc.)
    maps to ``False``.

    .. note::
       The comparison is intentionally case-sensitive to preserve
       backward-compatible behavior.

    :param value: The raw value received from the protocol layer.
    :type value: object
    :return: ``True`` only if *value* is exactly ``"True"``.
    :rtype: bool
    """
    return value is not None and value == "True"


# ============================================================================
# Typed specifications (input dicts)
# ============================================================================


class EnvelopeSpec(TypedDict):
    """Specification for a waveform envelope to be uploaded.

    :param name: Unique identifier for the envelope.
    :type name: str
    :param for_interpolation: Indicates if the envelope is designed for hardware
        interpolation.
    :type for_interpolation: bool
    :param is_symmetric: Indicates if the envelope has symmetry optimization.
    :type is_symmetric: bool
    :param i_even: Symmetry flag for the In-phase component.
    :type i_even: bool
    :param q_even: Symmetry flag for the Quadrature component.
    :type q_even: bool
    :param samples_iq: List of [I, Q] floating-point sample pairs.
    :type samples_iq: List[List[float]]
    """

    name: str
    for_interpolation: bool
    is_symmetric: bool
    i_even: bool
    q_even: bool
    samples_iq: list[list[float]]


class ReadoutWaveSpec(TypedDict):
    """Specification for a readout wave to be compiled and uploaded.

    :param envelope: Name of the envelope stored in hardware memory.
    :type envelope: str
    :param duration: Pulse duration in FPGA clock cycles.
    :type duration: int
    :param gain: Digital gain scaling factor in range [-1.0, 1.0].
    :type gain: float
    :param switch_iq: If ``"True"``, swaps I and Q signal paths.
    :type switch_iq: str | None
    :param keep_last: If ``"True"``, holds the last sample value after duration.
    :type keep_last: str | None
    """

    envelope: str
    duration: int
    gain: float
    switch_iq: str | None
    keep_last: str | None


# ============================================================================
# Wave types
# ============================================================================

WaveKind = Literal["env", "vz"]  # env = X/Y/readout, vz = Virtual-Z


@dataclass
class WaveEntry:
    """A unified cache entry for waveform or virtual gate commands.

    Acts as a discriminated union between standard envelope-based waves
    (``kind='env'``) and Virtual-Z gates (``kind='vz'``).  Stores both
    the high-level specification and the compiled hardware instruction (WDW).

    :param kind: Discriminator tag: ``'env'`` for standard pulses,
        ``'vz'`` for phase updates.
    :type kind: WaveKind
    :param envelope: Envelope shape name in hardware memory (env only).
    :type envelope: str
    :param duration: Pulse duration in FPGA clock cycles (env only).
    :type duration: int
    :param gain: Digital gain in [-1.0, 1.0] (env only).
    :type gain: float
    :param switch_iq: If True, swaps I/Q paths (env only).
    :type switch_iq: bool
    :param keep_last: If True, holds last sample after duration (env only).
    :type keep_last: bool
    :param vz_phase_rad: Phase increment in radians (vz only).
    :type vz_phase_rad: float
    :param wdw: Compiled Wave Definition Word, or None if not yet compiled.
    :type wdw: int | None
    """

    kind: WaveKind = "env"
    # --- env waves (X/Y/readout) ---
    envelope: str = ""
    duration: int = 0
    gain: float = 0.0
    switch_iq: bool = False
    keep_last: bool = False
    # --- vz waves ---
    vz_phase_rad: float = 0.0
    # --- compiled outcome ---
    wdw: int | None = None

    # --- construction ---

    @classmethod
    def from_spec(cls, wave_spec: dict) -> WaveEntry:
        """Build a WaveEntry from a raw wave specification dictionary.

        :param wave_spec: Dictionary with ``'kind'``, ``'wave_id'``,
            and kind-specific fields.
        :type wave_spec: dict
        :return: Constructed WaveEntry (``wdw=None``).
        :rtype: WaveEntry
        :raises ConfigurationError: If the specification is invalid.
        """
        kind = str(wave_spec.get("kind", "env")).lower()
        if kind not in ("env", "vz"):
            raise ConfigurationError(f"Unknown wave kind '{kind}' (use 'env' or 'vz').")

        if kind == "env":
            return cls(
                envelope=str(wave_spec["envelope"]),
                duration=int(wave_spec["duration"]),
                gain=float(wave_spec["gain"]),
                switch_iq=parse_bool_flag(wave_spec.get("switch_iq")),
                keep_last=parse_bool_flag(wave_spec.get("keep_last")),
                wdw=None,
            )

        if "vz_phase_rad" not in wave_spec:
            raise ConfigurationError(
                f"VZ wave '{wave_spec.get('wave_id')}' missing vz_phase_rad. " "Hint: provide vz_phase_rad (radians)."
            )
        return cls(
            kind="vz",
            envelope="",
            duration=0,
            gain=0.0,
            switch_iq=False,
            keep_last=False,
            vz_phase_rad=float(wave_spec["vz_phase_rad"]),
            wdw=None,
        )

    @classmethod
    def from_readout_spec(cls, spec: ReadoutWaveSpec) -> WaveEntry:
        """Build an env WaveEntry from a readout wave specification.

        :param spec: Readout wave specification.
        :type spec: ReadoutWaveSpec
        :return: Constructed WaveEntry (``wdw=None``, ``kind='env'``).
        :rtype: WaveEntry
        """
        return cls(
            envelope=str(spec["envelope"]),
            duration=int(spec["duration"]),
            gain=float(spec["gain"]),
            switch_iq=parse_bool_flag(spec.get("switch_iq")),
            keep_last=parse_bool_flag(spec.get("keep_last")),
            wdw=None,
        )

    # --- comparison ---

    def same_spec(self, other: WaveEntry) -> bool:
        """Check functional hardware equivalence with another WaveEntry.

        Two entries are equivalent when they produce the same WDW and
        FPGA behavior, allowing safe compilation skipping.

        :param other: The other wave entry.
        :type other: WaveEntry
        :return: True if functionally equivalent.
        :rtype: bool
        """
        if self.kind != other.kind:
            return False
        if self.kind == "env":
            return (
                self.envelope == other.envelope
                and self.duration == other.duration
                and self.gain == other.gain
                and self.switch_iq == other.switch_iq
                and self.keep_last == other.keep_last
            )
        return float(self.vz_phase_rad) == float(other.vz_phase_rad)

    # --- serialization ---

    def to_readout_result(self, gen_index: int, status: str) -> dict:
        """Build the result dictionary for readout wave operations.

        :param gen_index: Generator index.
        :type gen_index: int
        :param status: Status string (``'compiled'``, ``'replaced'``, ``'skipped'``).
        :type status: str
        :return: Readout wave result dictionary.
        :rtype: dict
        """
        return {
            "gen_index": gen_index,
            "status": status,
            "envelope": self.envelope,
            "duration": self.duration,
            "gain": self.gain,
            "switch_iq": self.switch_iq,
            "keep_last": self.keep_last,
            "WDW": hex(self.wdw),
        }


# ============================================================================
# Envelope validation
# ============================================================================


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


# ============================================================================
# IQ signal conversion
# ============================================================================


def iq_float_to_cint16(
    samples_iq: list[list[float]] | np.ndarray,
    sample_bits: int,
) -> np.ndarray:
    """Convert floating-point IQ samples into fixed-point complex int16 format.

    Performs quantization for the FPGA via:
    1. **Scaling**: Maps [-1.0, 1.0] to the full dynamic range of ``sample_bits``.
    2. **Symmetric Rounding**: Standard rounding to minimize quantization noise.
    3. **Hard Clipping**: Enforces saturation limits to prevent overflow.

    :param samples_iq: Either List[List[float]] or np.ndarray with shape (N, 2).
    :type samples_iq: list[list[float]] | np.ndarray
    :param sample_bits: Resolution (bit depth) of the target DAC (e.g. 16).
    :type sample_bits: int
    :return: Array of complex16 numbers ready for hardware upload.
    :rtype: np.ndarray
    """
    vmax = (1 << (sample_bits - 1)) - 1

    if isinstance(samples_iq, np.ndarray) and samples_iq.dtype == np.float32:
        i = np.clip(np.rint(samples_iq[:, 0] * vmax), -vmax - 1, vmax).astype(np.int16)
        q = np.clip(np.rint(samples_iq[:, 1] * vmax), -vmax - 1, vmax).astype(np.int16)
    else:
        a = np.asarray(samples_iq, dtype=np.float64)
        i = np.clip(np.rint(a[:, 0] * vmax), -vmax - 1, vmax).astype(np.int16)
        q = np.clip(np.rint(a[:, 1] * vmax), -vmax - 1, vmax).astype(np.int16)

    return i + 1j * q


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


# ============================================================================
# FIFO validation
# ============================================================================


def validate_fifo_capacity(
    gen_fifo_depth: int,
    start_index: int,
    sequence_length: int,
) -> None:
    """Validate FIFO capacity and raise if overflow would occur.

    :param gen_fifo_depth: FIFO segment depth (from hardware).
    :type gen_fifo_depth: int
    :param start_index: FIFO write start index.
    :type start_index: int
    :param sequence_length: Length of sequence to write.
    :type sequence_length: int
    :raises ConfigurationError: If FIFO would overflow.
    """
    max_entries = int(gen_fifo_depth // 4)
    end_index = start_index + sequence_length - 1
    if end_index > max_entries:
        raise ConfigurationError(
            f"program_drive_sequence: overflow: end_index={end_index} > " f"max_entries={max_entries}"
        )


def validate_wave_ids_in_cache(
    cache: dict[str, WaveEntry],
    wave_id_list: list[str],
    gen_wave_memory: dict,
) -> None:
    """Validate all wave_ids exist in HL cache and LL memory.

    :param cache: High-level wave cache.
    :type cache: dict[str, WaveEntry]
    :param wave_id_list: List of wave IDs to check.
    :type wave_id_list: list[str]
    :param gen_wave_memory: Generator wave memory dictionary (from HW).
    :type gen_wave_memory: dict
    :raises ConfigurationError: If any wave_id is missing.
    """
    missing_wave_id_hl = [wid for wid in wave_id_list if (wid not in cache) or (cache[wid].wdw) is None]
    missing_wave_id_ll = [wid for wid in wave_id_list if wid not in gen_wave_memory]

    if missing_wave_id_hl:
        raise ConfigurationError(f"program_drive_sequence: wave_id not in HL cache: {missing_wave_id_hl}")
    if missing_wave_id_ll:
        raise ConfigurationError(f"program_drive_sequence: wave_id was never compiled (LL): " f"{missing_wave_id_ll}")


__all__ = [
    "EnvelopeSpec",
    "ReadoutWaveSpec",
    "WaveKind",
    "WaveEntry",
    "iq_float_to_cint16",
    "parse_bool_flag",
    "process_envelope_samples",
    "validate_envelope_spec",
    "validate_envelope_symmetry",
    "validate_fifo_capacity",
    "validate_wave_ids_in_cache",
]
