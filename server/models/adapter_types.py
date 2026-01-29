# file: fireq-utils/server/models/adapter_types.py
"""Adapter-specific types for server.hardware.ol_adapter.

This module contains data structures used by the OverlayAdapter for:
- Wave definition and caching (WaveEntry)
- Envelope specifications (EnvelopeSpec)
- Type aliases for wave kinds (WaveKind)

These types are separated from config_types.py as they are internal
to the hardware adapter layer rather than user-facing configuration.
"""

from dataclasses import dataclass
from typing import Literal, TypedDict


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


# WAVE TYPES: regular waves vs virtual Z gates

WaveKind = Literal["env", "vz"]  # env = X/Y/readout, vz = Virtual-Z


@dataclass
class WaveEntry:
    """A unified high-level representation of a waveform or virtual gate command.

    This data structure serves as the primary cache entry for the generator, acting as a
    discriminated union between standard envelope-based waves and Virtual-Z (VZ) gates.
    It stores both the high-level specification and the compiled hardware instruction
    (WDW).

    :param kind: Discriminator tag. Use 'env' for standard pulses or 'vz' for phase
        updates.
    :type kind: WaveKind
    :param envelope: The name of the envelope shape stored in hardware memory (used only
        if kind='env').
    :type envelope: str
    :param duration: The duration of the pulse in FPGA clock cycles (used only if
        kind='env').
    :type duration: int
    :param gain: The digital gain scaling factor in range [-1.0, 1.0] (used only if
        kind='env').
    :type gain: float
    :param switch_iq: If True, swaps the I and Q signal paths (used only if kind='env').
    :type switch_iq: bool
    :param keep_last: If True, holds the last sample value after duration ends (used
        only if kind='env').
    :type keep_last: bool
    :param vz_phase_rad: The phase increment in radians (used only if kind='vz').
    :type vz_phase_rad: float
    :param wdw: The compiled 128-bit Wave Definition Word. If None, the entry requires
        compilation.
    :type wdw: Optional[int]
    """

    kind: WaveKind = "env"
    # --- env waves(X/Y/readout)
    envelope: str = ""
    duration: int = 1
    gain: float = 0.0
    switch_iq: bool = False
    keep_last: bool = False

    # --- vz waves ---
    vz_phase_rad: float = 0.0

    # --- compiled outcome ---
    wdw: int | None = None


def same_spec(a: WaveEntry, b: WaveEntry) -> bool:
    """Compare two WaveEntry objects for functional hardware equivalence.

    Equality here denotes that two instances produce the same Wave Definition Word (WDW)
    and FPGA behavior, allowing for safe compilation skipping.

    :param a: The first wave entry.
    :type a: WaveEntry
    :param b: The second wave entry.
    :type b: WaveEntry
    :return: True if the entries are functionally equivalent, False otherwise.
    :rtype: bool
    """
    if a.kind != b.kind:
        return False
    # X/Y/Readout  "envelope-centric"
    if a.kind == "env":
        return (
            a.envelope == b.envelope
            and a.duration == b.duration
            and a.gain == b.gain
            and a.switch_iq == b.switch_iq
            and a.keep_last == b.keep_last
        )

    # VZ: envelope/duration/gain : "phase-centric"
    return float(a.vz_phase_rad) == float(b.vz_phase_rad)


__all__ = [
    "EnvelopeSpec",
    "WaveKind",
    "WaveEntry",
    "same_spec",
]
