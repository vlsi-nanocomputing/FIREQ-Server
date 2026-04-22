# file: fireq-utils/server/execution/sweep_updates.py
"""Sweep fast-path update helpers for FIREQ experiments.

Provides value change tracking and per-subsystem update functions used
during sweep execution to skip redundant hardware calls.
"""

from __future__ import annotations


class ValueTracker:
    """Tracks last-applied values to skip redundant hardware calls.

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


def make_hashable(value: object) -> object:
    """Convert nested structure to hashable representation.

    :param value: Any object (dict, list, or scalar).
    :type value: object
    :return: Hashable representation (tuples for containers, scalars unchanged).
    :rtype: object
    """
    if isinstance(value, dict):
        return tuple(sorted((key, make_hashable(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(make_hashable(item) for item in value)
    return value


def extract_modulation_value(signal_config: dict) -> tuple[float, float]:
    """Extract (frequency_mhz, phase) tuple for comparison.

    :param signal_config: Configuration dict containing frequency_mhz and optional phase.
    :type signal_config: dict
    :return: Tuple of (frequency_mhz, phase).
    :rtype: tuple[float, float]
    """
    return (float(signal_config["frequency_mhz"]), float(signal_config.get("phase", 0.0)))


def apply_generator_signal_updates(
    adapter: object,
    gen_index: int,
    signal_config: dict,
    flags: set,
    signal_kind: str,
    tracker: ValueTracker,
) -> None:
    """Apply generator updates for drive or readout, skipping unchanged values.

    :param adapter: Hardware adapter.
    :type adapter: object
    :param gen_index: Generator index.
    :type gen_index: int
    :param signal_config: Configuration dict for this type (drive or readout section).
    :type signal_config: dict
    :param flags: Set of flags for this type.
    :type flags: set
    :param signal_kind: Type string (``"drive"`` or ``"readout"``).
    :type signal_kind: str
    :param tracker: Value tracker for change detection.
    :type tracker: ValueTracker
    """
    prefix = f"{signal_kind}_"

    if f"{prefix}mod" in flags and "frequency_mhz" in signal_config:
        modulation_value = extract_modulation_value(signal_config)
        if tracker.changed(("gen", gen_index, f"{prefix}mod"), modulation_value):
            adapter.generator.set_modulation(
                gen_index,
                signal_kind,
                {"frequency_mhz": modulation_value[0], "phase": modulation_value[1]},
            )

    if f"{prefix}nyquist" in flags and "nyquist_zone" in signal_config:
        nyquist_zone = int(signal_config["nyquist_zone"])
        if tracker.changed(("gen", gen_index, f"{prefix}nyquist"), nyquist_zone):
            adapter.generator.set_nyquist_zone(gen_index, signal_kind, nyquist_zone)

    if f"{prefix}channel" in flags and "channel" in signal_config:
        channel_value = int(signal_config["channel"])
        if tracker.changed(("gen", gen_index, f"{prefix}channel"), channel_value):
            adapter.generator.set_trigger_listener(gen_index, {"ttype": signal_kind, "channel": channel_value})

    # Type-specific final action
    if signal_kind == "drive" and "drive_fifo" in flags and "fifo" in signal_config:
        drive_fifo_value = (tuple(signal_config["fifo"]), signal_config.get("fifo_start_index", 1))
        if tracker.changed(("gen", gen_index, "drive_fifo"), drive_fifo_value):
            adapter.generator.program_drive_sequence(
                gen_index=gen_index,
                wave_id_list=signal_config["fifo"],
                start_index=drive_fifo_value[1],
            )

    elif signal_kind == "readout" and "readout_wave" in flags and "wave" in signal_config:
        # Deep conversion to capture nested values (config is mutated in-place by apply_point)
        wave_config = signal_config["wave"]
        wave_signature = make_hashable(wave_config)
        if tracker.changed(("gen", gen_index, "readout_wave"), wave_signature):
            adapter.generator.upload_readout_wave(gen_index=gen_index, wave=wave_config, replace=True)


class SweepUpdateApplier:
    """Applies per-point sweep delta updates to hardware subsystems.

    Dispatches to wave, generator, acquisition, and trigger update methods,
    using value change tracking to skip redundant hardware calls.

    :param adapter: Hardware adapter implementing the FIREQ control surface.
    :type adapter: object
    :param wave_handler: Wave compilation handler.
    :type wave_handler: object
    """

    def __init__(self, adapter: object, wave_handler: object) -> None:
        """Initialize with explicit dependencies.

        :param adapter: Hardware adapter implementing the FIREQ control surface.
        :type adapter: object
        :param wave_handler: Wave compilation handler.
        :type wave_handler: object
        """
        self._adapter = adapter
        self._wave_h = wave_handler

    def apply(
        self,
        config: dict,
        flags: dict,
        tracker: ValueTracker,
    ) -> None:
        """Apply sweep fast-path updates with value change detection.

        Called for each sweep point after apply_point() to update only
        hardware subsystems with changed values.

        :param config: Current experiment configuration (mutated by apply_point).
        :type config: dict
        :param flags: Sweep flags indicating which hardware to reconfigure.
        :type flags: dict
        :param tracker: Value tracker for change detection.
        :type tracker: ValueTracker
        """
        self._apply_wave_updates(config, flags.get("waves", set()), tracker)
        self._apply_generator_updates(config, flags.get("generators", {}), tracker)
        self._apply_acquisition_updates(config, flags.get("acquisitions", {}), tracker)
        self._apply_trigger_updates(config, flags.get("trigger", set()), tracker)

    def _apply_wave_updates(
        self,
        config: dict,
        wave_flags: set,
        tracker: ValueTracker,
    ) -> None:
        """Recompile waves if the wave section changed since last point.

        :param config: Current experiment configuration.
        :type config: dict
        :param wave_flags: Wave-related sweep flags.
        :type wave_flags: set
        :param tracker: Value tracker for change detection.
        :type tracker: ValueTracker
        """
        if "waves_compile" in wave_flags and "waves" in config:
            wave_signature = make_hashable(config["waves"])
            if tracker.changed(("waves", "compile"), wave_signature):
                self._wave_h.compile(config)

    def _apply_generator_updates(
        self,
        config: dict,
        generator_flags: dict,
        tracker: ValueTracker,
    ) -> None:
        """Apply per-generator drive/readout updates for swept fields.

        :param config: Current experiment configuration.
        :type config: dict
        :param generator_flags: Per-generator flag sets, keyed by list index.
        :type generator_flags: dict
        :param tracker: Value tracker for change detection.
        :type tracker: ValueTracker
        """
        for generator_list_index, generator_config in enumerate(config.get("generators", [])):
            generator_update_flags = generator_flags.get(generator_list_index, set())
            if not generator_update_flags:
                continue

            gen_index = int(generator_config["gen_index"])
            drive_config = generator_config.get("drive")
            readout_config = generator_config.get("readout")
            drive_flags = generator_update_flags & {"drive_mod", "drive_nyquist", "drive_channel", "drive_fifo"}
            readout_flags = generator_update_flags & {
                "readout_mod",
                "readout_nyquist",
                "readout_channel",
                "readout_wave",
            }

            if drive_config and drive_flags:
                apply_generator_signal_updates(
                    self._adapter,
                    gen_index,
                    drive_config,
                    drive_flags,
                    "drive",
                    tracker,
                )
            if readout_config and readout_flags:
                apply_generator_signal_updates(
                    self._adapter,
                    gen_index,
                    readout_config,
                    readout_flags,
                    "readout",
                    tracker,
                )

    def _apply_acquisition_updates(
        self,
        config: dict,
        acquisition_flags: dict,
        tracker: ValueTracker,
    ) -> None:
        """Apply per-acquisition modulation, channel, and timing updates.

        :param config: Current experiment configuration.
        :type config: dict
        :param acquisition_flags: Per-acquisition flag sets, keyed by list index.
        :type acquisition_flags: dict
        :param tracker: Value tracker for change detection.
        :type tracker: ValueTracker
        """
        for acquisition_list_index, acquisition_config in enumerate(config.get("acquisitions", [])):
            acquisition_update_flags = acquisition_flags.get(acquisition_list_index, set())
            if not acquisition_update_flags:
                continue

            acquisition_index = int(acquisition_config["acq_index"])

            if "acq_mod" in acquisition_update_flags and "frequency_mhz" in acquisition_config:
                modulation_value = extract_modulation_value(acquisition_config)
                if tracker.changed(("acq", acquisition_index, "acq_mod"), modulation_value):
                    self._adapter.acquisition.set_modulation(
                        acquisition_index,
                        {"frequency_mhz": modulation_value[0], "phase": modulation_value[1]},
                    )

            if "acq_channel" in acquisition_update_flags and "channel" in acquisition_config:
                channel_value = int(acquisition_config["channel"])
                if tracker.changed(("acq", acquisition_index, "acq_channel"), channel_value):
                    self._adapter.acquisition.set_trigger_listener(acquisition_index, {"channel": channel_value})

            if acquisition_update_flags & {"acq_duration", "acq_tof"} and "duration" in acquisition_config:
                timing_value = (int(acquisition_config.get("tof", 0)), int(acquisition_config["duration"]))
                if tracker.changed(("acq", acquisition_index, "acq_timing"), timing_value):
                    self._adapter.acquisition.set_timing(
                        acquisition_index,
                        tof=timing_value[0],
                        duration=timing_value[1],
                    )

    def _apply_trigger_updates(
        self,
        config: dict,
        trigger_flags: set,
        tracker: ValueTracker,
    ) -> None:
        """Apply trigger duration and delay updates for swept fields.

        :param config: Current experiment configuration.
        :type config: dict
        :param trigger_flags: Trigger-related sweep flags.
        :type trigger_flags: set
        :param tracker: Value tracker for change detection.
        :type tracker: ValueTracker
        """
        if not trigger_flags:
            return

        trigger_cfg = config.get("trigger", {})

        if "trig_shot_duration" in trigger_flags and "shot_duration" in trigger_cfg:
            shot_duration = int(trigger_cfg["shot_duration"])
            if tracker.changed(("trig", "shot_duration"), shot_duration):
                self._adapter.trigger.set_duration(shot_duration)

        if trigger_flags & {"trig_drive", "trig_readout"}:
            drive_config = trigger_cfg.get("drive") if "trig_drive" in trigger_flags else None
            readout_config = trigger_cfg.get("readout") if "trig_readout" in trigger_flags else None
            start_index = trigger_cfg.get("drive_start_index", 1)

            # Deep conversion to capture nested delay values
            drive_signature = make_hashable(drive_config) if drive_config else None
            readout_signature = make_hashable(readout_config) if readout_config else None
            delay_signature = (drive_signature, readout_signature, start_index)

            if tracker.changed(("trig", "delays"), delay_signature):
                self._adapter.trigger.program_delays(
                    drive=drive_config,
                    readout=readout_config,
                    drive_start_index=start_index,
                )


__all__ = [
    "ValueTracker",
    "SweepUpdateApplier",
    "make_hashable",
    "extract_modulation_value",
    "apply_generator_signal_updates",
]
