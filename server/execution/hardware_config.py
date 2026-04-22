# file: fireq-utils/server/execution/hardware_config.py
"""Hardware configuration orchestration for FIREQ experiments.

Encapsulates the responsibility of applying a full experiment configuration
to hardware: envelope uploads, wave compilation, generator/acquisition setup,
and trigger programming.
"""

import logging

from .handlers import EnvelopeHandler, StatusHandler, WaveHandler


class HardwareConfigurator:
    """Applies full experiment configurations to hardware subsystems.

    Owns the sequencing of envelope upload, wave compilation, generator
    configuration, acquisition setup, and trigger programming.  Delegates
    low-level operations to the adapter and to the specialized handlers
    (``EnvelopeHandler``, ``WaveHandler``).

    :param adapter: Hardware adapter implementing the FIREQ control surface.
    :type adapter: object
    :param status_handler: Provides hardware topology (e.g. acquisition count).
    :type status_handler: StatusHandler
    :param envelope_handler: Handles envelope uploads.
    :type envelope_handler: EnvelopeHandler
    :param wave_handler: Handles wave compilation.
    :type wave_handler: WaveHandler
    :param logger: Optional logger for consistent tracing.
    :type logger: logging.Logger | None
    """

    def __init__(
        self,
        adapter: object,
        status_handler: StatusHandler,
        envelope_handler: EnvelopeHandler,
        wave_handler: WaveHandler,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize with explicit dependencies.

        :param adapter: Hardware adapter implementing the FIREQ control surface.
        :type adapter: object
        :param status_handler: Provides hardware topology (e.g. acquisition count).
        :type status_handler: StatusHandler
        :param envelope_handler: Handles envelope uploads.
        :type envelope_handler: EnvelopeHandler
        :param wave_handler: Handles wave compilation.
        :type wave_handler: WaveHandler
        :param logger: Optional logger for consistent tracing.
        :type logger: logging.Logger | None
        """
        self._adapter = adapter
        self._status_h = status_handler
        self._env_h = envelope_handler
        self._wave_h = wave_handler
        self._logger = logger or logging.getLogger(__name__)

    # =========================================================================
    #                             PUBLIC API
    # =========================================================================

    def apply_full_config(self, config: dict, log: list | None = None) -> None:
        """Apply a full experiment configuration to hardware.

        Uploads envelopes, compiles waves, configures generators, disables
        then re-enables acquisitions, and programs the trigger.

        :param config: Experiment configuration dictionary.
        :type config: dict
        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        """
        if "envelopes" in config:
            self._env_h.upload(config)

        if "waves" in config:
            self._wave_h.compile(config)

        for gen_cfg in config.get("generators", []):
            self._configure_generator(gen_cfg, log)

        self.disable_acquisitions(log=log)
        for acquisition_config in config.get("acquisitions", []):
            self._configure_acquisition(acquisition_config, log)

        self._configure_trigger(config.get("trigger", {}), log)

    def disable_acquisitions(self, log: list | None = None) -> None:
        """Disable trigger listening on all acquisitions.

        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        """
        total = self._status_h.num_acquisitions
        if total <= 0:
            return
        for acquisition_index in range(total):
            self._adapter.acquisition.set_trigger_listener(acquisition_index, {"channel": 0})
            self._log(log, f"acq {acquisition_index} disabled (trigger channel 0)")

    # =========================================================================
    #                          PRIVATE HELPERS
    # =========================================================================

    def _configure_generator(self, gen_cfg: dict, log: list | None = None) -> None:
        """Configure a generator. Applies all settings present in the config dict.

        :param gen_cfg: Generator configuration dictionary.
        :type gen_cfg: dict
        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        """
        gen_index = gen_cfg["gen_index"]
        self._logger.debug(f"Configuring generator {gen_index}")

        if drive := gen_cfg.get("drive"):
            if "frequency_mhz" in drive:
                self._adapter.generator.set_modulation(
                    gen_index,
                    "drive",
                    {"frequency_mhz": float(drive["frequency_mhz"]), "phase": float(drive.get("phase", 0.0))},
                )
                self._log(log, f"gen {gen_index} drive frequency: {drive['frequency_mhz']} MHz")
            if "nyquist_zone" in drive:
                self._adapter.generator.set_nyquist_zone(gen_index, "drive", int(drive["nyquist_zone"]))
            if "channel" in drive:
                self._adapter.generator.set_trigger_listener(
                    gen_index, {"ttype": "drive", "channel": int(drive["channel"])}
                )
            if "fifo" in drive:
                self._adapter.generator.program_drive_sequence(
                    gen_index=gen_index, wave_id_list=drive["fifo"], start_index=drive.get("fifo_start_index", 1)
                )
                self._log(log, f"gen {gen_index} drive sequence programmed")
            legacy_drive_keys = {"source", "lfsr_seed", "lsfr_seed"} & set(drive)
            if legacy_drive_keys:
                raise ValueError(
                    f"drive fields {sorted(legacy_drive_keys)} are no longer supported; "
                    "use 'random' and 'random_seed'."
                )
            if "random" in drive:
                seed = drive.get("random_seed")
                self._adapter.generator.set_drive_source(
                    gen_index=gen_index,
                    source=str(drive["random"]),
                    seed=(int(seed) if seed is not None else None),
                )
                source_lower = str(drive["random"]).lower()
                if source_lower == "lfsr" and seed is not None:
                    self._log(log, f"gen {gen_index} drive source set to lfsr (seed={int(seed)})")
                else:
                    self._log(log, f"gen {gen_index} drive source set to {source_lower}")

        if readout := gen_cfg.get("readout"):
            if "frequency_mhz" in readout:
                self._adapter.generator.set_modulation(
                    gen_index,
                    "readout",
                    {"frequency_mhz": float(readout["frequency_mhz"]), "phase": float(readout.get("phase", 0.0))},
                )
            if "nyquist_zone" in readout:
                self._adapter.generator.set_nyquist_zone(gen_index, "readout", int(readout["nyquist_zone"]))
            if "channel" in readout:
                self._adapter.generator.set_trigger_listener(
                    gen_index, {"ttype": "readout", "channel": int(readout["channel"])}
                )
            if "wave" in readout:
                self._adapter.generator.upload_readout_wave(gen_index=gen_index, wave=readout["wave"], replace=True)
                self._log(log, f"gen {gen_index} readout wave uploaded")

    def _configure_acquisition(self, acquisition_config: dict, log: list | None = None) -> None:
        """Configure an acquisition. Applies all settings present in the config dict.

        :param acquisition_config: Acquisition configuration dictionary.
        :type acquisition_config: dict
        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        """
        acquisition_index = acquisition_config["acq_index"]

        if "frequency_mhz" in acquisition_config:
            self._adapter.acquisition.set_modulation(
                acquisition_index,
                {
                    "frequency_mhz": float(acquisition_config["frequency_mhz"]),
                    "phase": float(acquisition_config.get("phase", 0.0)),
                },
            )
        if "channel" in acquisition_config:
            self._adapter.acquisition.set_trigger_listener(
                acquisition_index, {"channel": int(acquisition_config["channel"])}
            )
            self._log(log, f"acq {acquisition_index} listening to trigger channel {acquisition_config['channel']}")
        if "duration" in acquisition_config:
            tof = int(acquisition_config.get("tof", 0))
            self._adapter.acquisition.set_timing(
                acquisition_index,
                tof=tof,
                duration=int(acquisition_config["duration"]),
            )
            self._log(log, f"acq {acquisition_index} timing set: tof={tof}")

    def _configure_trigger(self, trigger_cfg: dict, log: list | None = None) -> None:
        """Configure trigger routing and timing. Applies all settings present in the config dict.

        :param trigger_cfg: Trigger configuration dictionary.
        :type trigger_cfg: dict
        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        """
        if not trigger_cfg:
            return

        if "shot_duration" in trigger_cfg:
            self._adapter.trigger.set_duration(int(trigger_cfg["shot_duration"]))

        has_drive = "drive" in trigger_cfg
        has_readout = "readout" in trigger_cfg
        if has_drive or has_readout:
            self._adapter.trigger.program_delays(
                drive=trigger_cfg.get("drive") if has_drive else None,
                readout=trigger_cfg.get("readout") if has_readout else None,
                drive_start_index=trigger_cfg.get("drive_start_index", 1),
            )
            shots = trigger_cfg.get("shots")
            msg = "trigger delays programmed" if shots is None else f"trigger delays programmed for {shots} shots"
            self._log(log, msg)

    @staticmethod
    def _log(log: list | None, msg: str) -> None:
        """Append a message to the config log if provided.

        :param log: Optional list for user-visible configuration actions.
        :type log: list | None
        :param msg: Message to append.
        :type msg: str
        """
        if log is not None:
            log.append(msg)


__all__ = ["HardwareConfigurator"]
