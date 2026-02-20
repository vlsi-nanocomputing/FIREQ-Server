# file: fireq-utils/server/models/config_types.py
"""Configuration type definitions for FIREQ experiments.

This module provides TypedDict definitions that document the expected
configuration structure and provide IDE autocomplete support. These types
do not enforce runtime validation (that's the TCP layer's responsibility).

All TypedDicts use ``total=False`` to make all fields optional, supporting
partial configurations and sweep variable placeholders.
"""

from typing import TypedDict


class GeneratorDriveConfig(TypedDict, total=False):
    """Drive channel configuration for a generator.

    :param frequency_mhz: Drive frequency in MHz.
    :type frequency_mhz: float
    :param phase: Phase offset in radians.
    :type phase: float
    :param nyquist_zone: Nyquist zone for frequency mapping.
    :type nyquist_zone: int
    :param channel: Trigger channel to listen on.
    :type channel: int
    :param fifo: List of wave IDs to sequence.
    :type fifo: list[str]
    :param fifo_start_index: Starting index in the FIFO.
    :type fifo_start_index: int
    """

    frequency_mhz: float
    phase: float
    nyquist_zone: int
    channel: int
    fifo: list[str]
    fifo_start_index: int


class GeneratorReadoutConfig(TypedDict, total=False):
    """Readout channel configuration for a generator.

    :param frequency_mhz: Readout frequency in MHz.
    :type frequency_mhz: float
    :param phase: Phase offset in radians.
    :type phase: float
    :param nyquist_zone: Nyquist zone for frequency mapping.
    :type nyquist_zone: int
    :param channel: Trigger channel to listen on.
    :type channel: int
    :param wave: Readout wave specification.
    :type wave: dict
    """

    frequency_mhz: float
    phase: float
    nyquist_zone: int
    channel: int
    wave: dict


class GeneratorConfig(TypedDict, total=False):
    """Full generator configuration.

    :param gen_index: Generator index.
    :type gen_index: int
    :param drive: Drive channel configuration.
    :type drive: GeneratorDriveConfig
    :param readout: Readout channel configuration.
    :type readout: GeneratorReadoutConfig
    """

    gen_index: int
    drive: GeneratorDriveConfig
    readout: GeneratorReadoutConfig


class AcquisitionConfig(TypedDict, total=False):
    """Acquisition channel configuration.

    :param acq_index: Acquisition index.
    :type acq_index: int
    :param frequency_mhz: Acquisition frequency in MHz.
    :type frequency_mhz: float
    :param phase: Phase offset in radians.
    :type phase: float
    :param channel: Trigger channel to listen on.
    :type channel: int
    :param duration: Number of samples to capture per shot.
    :type duration: int
    :param tof: Time of flight delay in samples.
    :type tof: int
    :param output_type: Acquisition mode (decimated, accumulated, raw).
    :type output_type: str
    """

    acq_index: int
    frequency_mhz: float
    phase: float
    channel: int
    duration: int
    tof: int
    output_type: str


class TriggerDelayConfig(TypedDict, total=False):
    """Trigger delay configuration for a channel group.

    The delay field can be:
    - An integer (single delay value)
    - A list of [delay, count] pairs for multi-pulse sequences
    - A string starting with ``$`` for sweep variables

    :param delay: Delay value(s) in clock cycles or sweep variable.
    :type delay: int | list | str
    """

    delay: int | list | str


class TriggerConfig(TypedDict, total=False):
    """Trigger configuration.

    The drive and readout fields are dicts keyed by channel ID strings,
    where each value contains delay configuration for that channel group.

    :param shots: Number of shots to execute.
    :type shots: int
    :param shot_duration: Duration of each shot in clock cycles.
    :type shot_duration: int
    :param drive: Drive trigger delays keyed by channel ID.
    :type drive: dict[str, TriggerDelayConfig]
    :param readout: Readout trigger delays keyed by channel ID.
    :type readout: dict[str, TriggerDelayConfig]
    :param drive_start_index: Starting index for drive triggers.
    :type drive_start_index: int
    """

    shots: int
    shot_duration: int
    drive: dict[str, TriggerDelayConfig]
    readout: dict[str, TriggerDelayConfig]
    drive_start_index: int


class SweepVariableSpec(TypedDict, total=False):
    """Sweep variable specification.

    Either ``values`` or (``start``, ``stop``, ``num``) must be provided.

    :param name: Variable name (referenced as ``$name`` in config).
    :type name: str
    :param values: Explicit list of values.
    :type values: list
    :param start: Start value for linspace.
    :type start: float
    :param stop: Stop value for linspace.
    :type stop: float
    :param num: Number of points for linspace.
    :type num: int
    :param space: Spacing mode (default: "lin").
    :type space: str
    """

    name: str
    values: list
    start: float
    stop: float
    num: int
    space: str


class ExperimentConfig(TypedDict, total=False):
    """Full experiment configuration.

    :param envelopes: Envelope specifications keyed by generator index.
    :type envelopes: dict[str, list[dict]]
    :param waves: Wave definitions keyed by generator index.
    :type waves: dict[str, list[dict]]
    :param generators: List of generator configurations.
    :type generators: list[GeneratorConfig]
    :param acquisitions: List of acquisition configurations.
    :type acquisitions: list[AcquisitionConfig]
    :param trigger: Trigger configuration.
    :type trigger: TriggerConfig
    :param timeout: Acquisition timeout in seconds.
    :type timeout: float
    """

    envelopes: dict[str, list[dict]]
    waves: dict[str, list[dict]]
    generators: list[GeneratorConfig]
    acquisitions: list[AcquisitionConfig]
    trigger: TriggerConfig
    timeout: float


class SweepMessage(TypedDict, total=False):
    """Sweep message format.

    :param sweep_id: Unique sweep identifier.
    :type sweep_id: str
    :param base: Base experiment configuration with ``$var`` placeholders.
    :type base: ExperimentConfig
    :param variables: List of sweep variable specifications.
    :type variables: list[SweepVariableSpec]
    :param sweep_mode: Sweep mode (cartesian or zipped).
    :type sweep_mode: str
    """

    sweep_id: str
    base: ExperimentConfig
    variables: list[SweepVariableSpec]
    sweep_mode: str


# --- Hardware adapter types (server.hardware) ---


class Modulation(TypedDict):
    """Specification for Local Oscillator (LO) modulation parameters.

    Used by server.hardware.ol_adapter for generator and acquisition modulation.

    :param frequency_mhz: The modulation frequency in MHz.
    :type frequency_mhz: float
    :param phase: The phase offset in degrees (optional, primarily for readout).
    :type phase: Optional[float]
    """

    frequency_mhz: float
    phase: float | None


class TriggerCommand(TypedDict):
    """Specification for a trigger configuration command.

    Used by server.hardware.ol_adapter for trigger listening configuration.

    :param ttype: The trigger type identifier (e.g., 'start', 'readout').
    :type ttype: str
    :param channel: The target channel index for the trigger.
    :type channel: int
    """

    ttype: str
    channel: int


__all__ = [
    "GeneratorDriveConfig",
    "GeneratorReadoutConfig",
    "GeneratorConfig",
    "AcquisitionConfig",
    "TriggerDelayConfig",
    "TriggerConfig",
    "SweepVariableSpec",
    "ExperimentConfig",
    "SweepMessage",
    # Hardware adapter types
    "Modulation",
    "TriggerCommand",
]
