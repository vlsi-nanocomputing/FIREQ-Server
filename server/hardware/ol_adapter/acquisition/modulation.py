"""DDS modulation and trigger listener configuration for acquisition.

This module provides the ModulationOps class that handles:
- DDS modulation parameter configuration
- Acquisition trigger channel selection
- Mix-mode configuration for ADC units
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ....models.config_types import Modulation, TriggerCommand

if TYPE_CHECKING:
    from .cache import AdapterContext


class ModulationOps:
    """Operation class for acquisition modulation configuration.

    Handles DDS and trigger configuration including:
    - DDS frequency and phase setting
    - Mix-mode configuration for Nyquist zones
    - Trigger channel assignment for acquisitions

    Attributes:
    -----------
    _ctx : AdapterContext
        Shared context containing ll, cache, logger, and other dependencies.
    """

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize ModulationOps.

        :param ctx: Shared adapter context with all dependencies.
        :type ctx: AdapterContext
        """
        self._ctx = ctx

    def set_modulation(self, acq_index: int, mod: Modulation) -> dict:
        """Configure the DDS modulation parameters for an acquisition unit.

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param mod: Dictionary containing frequency and phase parameters.
        :type mod: Modulation
        :return: The applied configuration.
        :rtype: dict
        """
        freq_mhz = mod["frequency_mhz"]
        phase = mod["phase"]

        self._ctx.logger.debug(
            "set_modulation: acq=%d frequency=%s phase=%s",
            acq_index,
            freq_mhz,
            phase,
        )
        unit = self._ctx.ll.get_acq(acq_index)

        # Configure Mix-Mode via overlay
        try:
            mix_info = self._ctx.ll.overlay_driver.configure_adc_mix_mode(acq_index=acq_index, freq_mhz=freq_mhz)
            if mix_info.get("changed"):
                self._ctx.logger.debug(
                    "ADC Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                    mix_info["nyquist_zone"],
                    mix_info["amd_zone"],
                    mix_info["tile"],
                    mix_info["block"],
                )
        except ValueError as e:
            self._ctx.logger.warning(f"ADC Mix-mode config skipped: {e}")

        self._ctx.ll.call(
            unit.set_acquisition_dds_parameters(
                frequency=freq_mhz,
                phase=phase,
                adc_samplerate=self._ctx.ll.adc_sr_mhz(),
            ),
            operation="set_acquisition_dds_parameters",
            driver_name="AcquisitionDriver",
            config_error=True,
        )
        self._ctx.logger.debug("set_modulation: done acq=%d", acq_index)

        return {
            "acq_index": acq_index,
            "frequency_mhz": freq_mhz,
            "phase": phase,
        }

    def set_trigger_listener(self, acq_index: int, trig: TriggerCommand) -> dict:
        """Configure which trigger channel the acquisition should listen to.

        :param acq_index: Index of the target acquisition unit.
        :type acq_index: int
        :param trig: Dictionary defining the trigger type and source channel.
        :type trig: TriggerCommand
        :return: The applied trigger configuration.
        :rtype: dict
        """
        channel = trig["channel"]

        self._ctx.logger.debug("set_trigger_listener: acq=%d channel=%s", acq_index, channel)
        unit = self._ctx.ll.get_acq(acq_index)

        self._ctx.ll.call(
            unit.set_trigger_channel(channel=channel),
            operation="set_trigger_channel",
            driver_name="AcquisitionDriver",
            config_error=True,
        )

        if channel == 0:
            self._ctx.logger.debug("Acquisition %d is deaf to any trigger!", acq_index)
        else:
            self._ctx.logger.debug(
                "Acquisition %d listens to trigger_word channel %d",
                acq_index,
                channel,
            )
        self._ctx.cache.acq_trigger_channel[int(acq_index)] = int(channel)

        return {
            "acq_index": acq_index,
            "channel": channel,
        }


__all__ = ["ModulationOps"]
