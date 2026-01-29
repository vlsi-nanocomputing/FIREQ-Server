# file: fireq-utils/server/hardware/adapter/modulation.py
"""Modulation configuration mixin for OverlayAdapter.

This module provides the ModulationMixin class that handles:
- Generator DDS modulation (drive/readout)
- Acquisition DDS modulation
- Trigger channel configuration for generators and acquisitions
- Acquisition timing parameters
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from ...models.config_types import Modulation, TriggerCommand
from ...models.exceptions import ConfigurationError

if TYPE_CHECKING:
    import logging


class ModulationMixin:
    """Mixin class providing modulation and trigger configuration methods.

    This mixin expects the following attributes on self:
    - ol: The low-level overlay driver
    - logger: A logging.Logger instance
    - _call: Method for driver call error handling
    - _get_gen: Method to get generator driver by index
    - _get_acq: Method to get acquisition driver by index
    - _dac_sr_mhz: Method returning DAC sample rate in MHz
    - _adc_sr_mhz: Method returning ADC sample rate in MHz
    - _acq_trigger_channel: Dict tracking acquisition trigger channels
    """

    # Type hints for attributes expected from the main class
    ol: object
    logger: logging.Logger
    _acq_trigger_channel: dict[int, int]

    def _configure_modulation(
        self,
        unit_type: Literal["generator", "acquisition"],
        unit_index: int,
        label: str | None,
        mod: Modulation,
    ) -> dict:
        """Unified modulation configuration for both generators and acquisitions.

        :param unit_type: Either "generator" or "acquisition".
        :param unit_index: Index of the unit.
        :param label: For generators: 'drive' or 'readout'. For acquisitions: None.
        :param mod: Modulation parameters (frequency_mhz, phase).
        :return: Applied configuration summary.
        """
        freq_mhz = mod["frequency_mhz"]
        phase = mod["phase"]

        if unit_type == "generator":
            self.logger.debug(
                "generator_modulation: gen=%d label=%s frequency=%f phase (if readout)=%s",
                unit_index,
                label,
                freq_mhz,
                phase,
            )
            unit = self._get_gen(unit_index)

            # Configure Mix-Mode via overlay
            try:
                mix_info = self.ol.configure_dac_mix_mode(unit_index, label, freq_mhz)
                if mix_info.get("changed"):
                    self.logger.debug(
                        "Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                        mix_info["nyquist_zone"],
                        mix_info["amd_zone"],
                        mix_info["tile"],
                        mix_info["block"],
                    )
            except ValueError as e:
                self.logger.debug(f"Mix-mode config skipped: {e}")

            if label == "drive":
                self._call(
                    unit.set_drive_dds_parameters(
                        frequency=freq_mhz,
                        dac_samplerate=self._dac_sr_mhz(),
                    ),
                    operation="set_drive_dds_parameters",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )
            elif label == "readout":
                self._call(
                    unit.set_readout_dds_parameters(
                        frequency=freq_mhz,
                        phase=phase,
                        dac_samplerate=self._dac_sr_mhz(),
                    ),
                    operation="set_readout_dds_parameters",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )
            else:
                raise ConfigurationError("Invalid mode selection!\nHint: select label = 'drive' or 'readout'")

            return {
                "gen_index": unit_index,
                "label": label,
                "frequency_mhz": freq_mhz,
                "phase": phase,
            }

        else:  # acquisition
            self.logger.debug(
                "acquisition_modulation: acq=%d frequency=%s phase=%s",
                unit_index,
                freq_mhz,
                phase,
            )
            unit = self._get_acq(unit_index)

            # Configure Mix-Mode via overlay
            try:
                mix_info = self.ol.configure_adc_mix_mode(acq_index=unit_index, freq_mhz=freq_mhz)
                if mix_info.get("changed"):
                    self.logger.debug(
                        "ADC Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                        mix_info["nyquist_zone"],
                        mix_info["amd_zone"],
                        mix_info["tile"],
                        mix_info["block"],
                    )
            except ValueError as e:
                self.logger.warning(f"ADC Mix-mode config skipped: {e}")

            self._call(
                unit.set_acquisition_dds_parameters(
                    frequency=freq_mhz,
                    phase=phase,
                    adc_samplerate=self._adc_sr_mhz(),
                ),
                operation="set_acquisition_dds_parameters",
                driver_name="AcquisitionDriver",
                config_error=True,
            )
            self.logger.debug("acquisition_parameters: done acq=%d", unit_index)

            return {
                "acq_index": unit_index,
                "frequency_mhz": freq_mhz,
                "phase": phase,
            }

    def _configure_trigger_listener(
        self,
        unit_type: Literal["generator", "acquisition"],
        unit_index: int,
        trig: TriggerCommand,
    ) -> dict:
        """Unified trigger channel configuration for generators and acquisitions.

        :param unit_type: Either "generator" or "acquisition".
        :param unit_index: Index of the unit.
        :param trig: Trigger command with type and channel.
        :return: Applied configuration summary.
        """
        channel = trig["channel"]
        ttype = trig["ttype"]

        if unit_type == "generator":
            self.logger.debug(
                "gen_trigger2listen: gen=%d ttype=%s channel=%s",
                unit_index,
                ttype,
                channel,
            )
            unit = self._get_gen(unit_index)

            self._call(
                unit.set_trigger_channel(channel=channel, ttype=ttype),
                operation="set_trigger_channel",
                driver_name="GeneratorDriver",
                config_error=True,
            )

            if channel == 0:
                self.logger.debug("Generator %d is deaf to any trigger!", unit_index)
            else:
                self.logger.debug(
                    "Generator %d listens to %s_trigger_word channel %d",
                    unit_index,
                    ttype,
                    channel,
                )

            return {
                "gen_index": unit_index,
                "ttype": ttype,
                "channel": channel,
            }

        else:  # acquisition
            self.logger.debug("acq_trigger2listen: acq=%d channel=%s", unit_index, channel)
            unit = self._get_acq(unit_index)

            self._call(
                unit.set_trigger_channel(channel=channel),
                operation="set_trigger_channel",
                driver_name="AcquisitionDriver",
                config_error=True,
            )

            if channel == 0:
                self.logger.debug("Acquisition %d is deaf to any trigger!", unit_index)
            else:
                self.logger.debug(
                    "Acquisition %d listens to %s_trigger_word channel %d",
                    unit_index,
                    ttype,
                    channel,
                )
            self._acq_trigger_channel[int(unit_index)] = int(channel)

            return {
                "acq_index": unit_index,
                "channel": channel,
            }

    # --- Public methods (delegate to unified helpers) ---

    def generator_modulation(self, gen_index: int, label: str, gen_mod: Modulation) -> dict:
        """Configure the Direct Digital Synthesis (DDS) modulation parameters.

        This method handles both the digital frequency synthesis configuration and the
        analog-domain Mix-Mode settings (Nyquist zone selection) based on the target frequency.

        :param gen_index: The index of the target generator.
        :type gen_index: int
        :param label: The modulation context, must be either 'drive' (control) or 'readout' (measurement).
        :type label: str
        :param gen_mod: A dictionary containing the modulation parameters (frequency in MHz, phase in degrees).
        :type gen_mod: Modulation
        :return: A summary of the applied modulation configuration.
        :rtype: dict
        :raises ConfigurationError: If the ``label`` is not 'drive' or 'readout'.
        """
        return self._configure_modulation("generator", gen_index, label, gen_mod)

    def acquisition_modulation(self, acq_index: int, acq_mod: Modulation) -> dict:
        """Configure the DDS modulation parameters for an acquisition unit.

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param acq_mod: Dictionary containing frequency and phase parameters.
        :type acq_mod: Modulation
        :return: The applied configuration.
        :rtype: dict
        """
        return self._configure_modulation("acquisition", acq_index, None, acq_mod)

    def gen_trigger2listen(self, gen_index: int, trig: TriggerCommand) -> dict:
        """Configure which trigger channel the generator should listen to.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param trig: Dictionary defining the trigger type and source channel.
        :type trig: TriggerCommand
        :return: The applied trigger configuration.
        :rtype: dict
        """
        return self._configure_trigger_listener("generator", gen_index, trig)

    def acq_trigger2listen(self, acq_index: int, trig: TriggerCommand) -> dict:
        """Configure which trigger channel the acquisition should listen to.

        :param acq_index: Index of the target acquisition unit.
        :type acq_index: int
        :param trig: Dictionary defining the trigger type and source channel.
        :type trig: TriggerCommand
        :return: The applied trigger configuration.
        :rtype: dict
        """
        return self._configure_trigger_listener("acquisition", acq_index, trig)

    def acquisition_timing(self, acq_index: int, tof: int, duration: int) -> dict:
        """Configure the timing parameters (Time of Flight and Duration).

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param tof: Time of Flight delay in clock cycles.
        :type tof: int
        :param duration: Acquisition duration in clock cycles.
        :type duration: int
        :return: The applied timing configuration.
        :rtype: dict
        """
        self.logger.debug("acquisition_timing: acq_index=%d tof=%d duration=%d", acq_index, tof, duration)
        acq = self._get_acq(acq_index)

        self._call(
            acq.set_acquisition_duration(duration),
            operation="set_acquisition_duration",
            driver_name="AcquisitionDriver",
            config_error=True,
        )

        self._call(
            acq.set_time_of_flight(tof),
            operation="set_time_of_flight",
            driver_name="AcquisitionDriver",
            config_error=True,
        )
        return {
            "acq_index": acq_index,
            "tof": tof,
            "duration": duration,
        }


__all__ = ["ModulationMixin"]
