"""Generator modulation configuration.

This module handles:
- DDS modulation parameter configuration
- Nyquist zone selection
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ....models.config_types import Modulation
from ....models.exceptions import ConfigurationError
from . import wave_utils as gu

if TYPE_CHECKING:
    from ..cache import AdapterContext


class ModulationOps:
    """Generator modulation operations."""

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize ModulationOps.

        :param ctx: Shared adapter context with all dependencies.
        """
        self._ctx = ctx

    def _configure_modulation(
        self,
        label: str | None,
        gen_index: int,
        mod: Modulation,
    ) -> dict:
        """Unified modulation configuration for generators.

        :param label: For generators: 'drive' or 'readout'.
        :param gen_index: Index of the generator.
        :param mod: Modulation parameters (frequency_mhz, phase).
        :return: Applied configuration summary.
        """
        freq_mhz = mod["frequency_mhz"]
        phase = mod["phase"]

        self._ctx.logger.debug(
            "set_modulation: gen=%d label=%s frequency=%f phase=%s",
            gen_index,
            label,
            freq_mhz,
            phase,
        )
        unit = self._ctx.ll.get_gen(gen_index)

        # Configure Mix-Mode via overlay
        try:
            mix_info = self._ctx.ll.overlay_driver.configure_dac_mix_mode(gen_index, label, freq_mhz)
            if mix_info.get("changed"):
                self._ctx.logger.debug(
                    "Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                    mix_info["nyquist_zone"],
                    mix_info["amd_zone"],
                    mix_info["tile"],
                    mix_info["block"],
                )
        except ValueError as e:
            self._ctx.logger.debug(f"Mix-mode config skipped: {e}")

        if label == "drive":
            self._ctx.ll.call(
                unit.set_drive_dds_parameters(
                    frequency=freq_mhz,
                    dac_samplerate=self._ctx.ll.dac_sr_mhz(),
                ),
                operation="set_drive_dds_parameters",
                driver_name="GeneratorDriver",
                config_error=True,
            )
        elif label == "readout":
            self._ctx.ll.call(
                unit.set_readout_dds_parameters(
                    frequency=freq_mhz,
                    phase=phase,
                    dac_samplerate=self._ctx.ll.dac_sr_mhz(),
                ),
                operation="set_readout_dds_parameters",
                driver_name="GeneratorDriver",
                config_error=True,
            )
        else:
            raise ConfigurationError("Invalid mode selection!\nHint: select label = 'drive' or 'readout'")

        return {
            "gen_index": gen_index,
            "label": label,
            "frequency_mhz": freq_mhz,
            "phase": phase,
        }

    def set_modulation(self, gen_index: int, label: str, mod: Modulation) -> dict:
        """Configure the Direct Digital Synthesis (DDS) modulation parameters.

        This method handles both the digital frequency synthesis configuration and the
        analog-domain Mix-Mode settings (Nyquist zone selection) based on the target frequency.

        :param gen_index: The index of the target generator.
        :param label: The modulation context, must be either 'drive' or 'readout'.
        :param mod: A dictionary containing the modulation parameters (frequency in MHz, phase in degrees).
        :return: A summary of the applied modulation configuration.
        :raises ConfigurationError: If the ``label`` is not 'drive' or 'readout'.
        """
        return self._configure_modulation(label, gen_index, mod)

    def set_nyquist_zone(self, gen_index: int, label: str, zone: int) -> dict:
        """Set the Nyquist zone for a generator's modulation.

        This method explicitly sets the Mix-Mode Nyquist zone for a generator's
        drive or readout path. The zone determines which Nyquist band is used for
        the analog Mix-Mode configuration in the RF frontend.

        Note: The zone is set by configuring an appropriate frequency. For even zones,
        a mixing mode frequency is used; for odd zones, the frequency is in the baseband.

        :param gen_index: Index of the target generator.
        :param label: Modulation context ('drive' or 'readout').
        :param zone: Target Nyquist zone (typically 1 or 2).
        :return: Summary of applied zone configuration.
        """
        self._ctx.logger.debug(
            "set_nyquist_zone: gen=%d label=%s zone=%d",
            gen_index,
            label,
            zone,
        )

        try:
            try:
                dac_nyquist_hz = self._ctx.ll.overlay_driver.hw_specs["summary"]["dac_nyquist_hz"]
            except (KeyError, TypeError, AttributeError):
                dac_nyquist_hz = 2.0e9  # Default 2 GHz Nyquist

            freq_mhz = gu.compute_frequency_for_zone(zone, dac_nyquist_hz)

            try:
                mix_info = self._ctx.ll.overlay_driver.configure_dac_mix_mode(gen_index, label, freq_mhz)
                result = {
                    "gen_index": gen_index,
                    "label": label,
                    "nyquist_zone": mix_info.get("nyquist_zone", zone),
                    "amd_zone": mix_info.get("amd_zone"),
                }
                if mix_info.get("nyquist_zone"):
                    self._ctx.logger.debug(
                        "Nyquist zone configured: Zone %d (AMD=%d) on tile=%d block=%d",
                        mix_info["nyquist_zone"],
                        mix_info.get("amd_zone"),
                        mix_info.get("tile"),
                        mix_info.get("block"),
                    )
                return result
            except (AttributeError, TypeError, KeyError):
                return {
                    "gen_index": gen_index,
                    "label": label,
                    "nyquist_zone": zone,
                    "status": "mocked",
                }
        except (ValueError, KeyError, AttributeError) as e:
            self._ctx.logger.error(f"Failed to set Nyquist zone: {e}")
            raise


__all__ = ["ModulationOps"]
