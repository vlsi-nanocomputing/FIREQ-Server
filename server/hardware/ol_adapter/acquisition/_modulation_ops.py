"""DDS modulation configuration for acquisition.

This module handles:
- DDS modulation parameter configuration (frequency, phase)
- ADC Mix-Mode configuration for Nyquist zone selection
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ....models.config_types import Modulation

if TYPE_CHECKING:
    from .._cache import AdapterContext


class ModulationOps:
    """Acquisition DDS modulation configuration."""

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize ModulationOps.

        :param ctx: Shared adapter context with all dependencies.
        :type ctx: AdapterContext
        """
        self._ctx = ctx

    # ========================================================================
    # PUBLIC METHODS
    # ========================================================================

    def set_modulation(self, acq_index: int, mod: Modulation) -> dict:
        """Configure the DDS modulation parameters for an acquisition unit.

        Handles both the digital frequency synthesis configuration and the
        analog-domain Mix-Mode settings (Nyquist zone) based on the target frequency.

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

        self._configure_adc_mix_mode(acq_index, freq_mhz)

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

    # ========================================================================
    # INTERNAL HELPERS
    # ========================================================================

    def _configure_adc_mix_mode(self, acq_index: int, freq_mhz: float) -> None:
        """Configure the ADC Mix-Mode (Nyquist zone) based on target frequency.

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param freq_mhz: Target frequency in MHz.
        :type freq_mhz: float
        """
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


__all__ = ["ModulationOps"]
