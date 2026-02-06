"""Generator operations orchestrator.

This module coordinates wave management, FIFO programming, and modulation control.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .fifo_ops import FIFOOps
from .modulation_ops import ModulationOps
from .trigger_ops import TriggerOps
from .wave_envelope_ops import WaveEnvelopeOps

if TYPE_CHECKING:
    from ..cache import AdapterContext
    from ..overlay_adapter_types import WaveEntry


class GeneratorOps:
    """High-level generator operations combining wave, FIFO, modulation, and triggering.

    This class orchestrates four specialized operation classes:
    - WaveEnvelopeOps: Wave definition, compilation, and envelope management
    - FIFOOps: Drive sequence FIFO programming
    - ModulationOps: DDS modulation configuration
    - TriggerOps: Generator trigger configuration
    """

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize GeneratorOps with all submodules.

        :param ctx: Shared adapter context with all dependencies.
        """
        self._ctx = ctx
        self._waves = WaveEnvelopeOps(ctx)
        self._fifo = FIFOOps(ctx)
        self._modulation = ModulationOps(ctx)
        self._trigger = TriggerOps(ctx)

    # ========== Wave Management Delegation ==========

    def get_wave_cache(self, gen_index: int) -> dict:
        """Retrieve the High-Level wave cache for a specific generator."""
        return self._waves.get_wave_cache(gen_index)

    def get_envelope_names(self, gen_index: int) -> list[str]:
        """Retrieve the list of envelope names currently stored in the generator's memory."""
        return self._waves.get_envelope_names(gen_index)

    def upload_envelopes(self, *, gen_index: int, envelopes: list, auto_pad_noninterp: bool = True) -> dict:
        """Upload multiple envelopes into generator envelope memory."""
        return self._waves.upload_envelopes(
            gen_index=gen_index,
            envelopes=envelopes,
            auto_pad_noninterp=auto_pad_noninterp,
        )

    def compile_waves(self, *, gen_index: int, waves: list[dict], replace: bool) -> dict:
        """Compile high-level wave definitions into hardware Wave Definition Words (WDW)."""
        return self._waves.compile_waves(gen_index=gen_index, waves=waves, replace=replace)

    def upload_readout_wave(self, *, gen_index: int, wave: dict, replace: bool = False) -> dict:
        """Compile and upload a specific wave configuration for readout operations."""
        return self._waves.upload_readout_wave(gen_index=gen_index, wave=wave, replace=replace)

    def get_readout_wave_cache(self, gen_index: int) -> WaveEntry | None:
        """Return the WaveEntry currently configured for readout, if any."""
        return self._waves.get_readout_wave_cache(gen_index)

    def reset_wave_memory(
        self,
        *,
        gen_index: int,
        preserve_wave_specs: bool = True,
        clear_last_fifo: bool = True,
    ) -> dict:
        """Reset the generator wave memory and synchronize the High-Level cache."""
        return self._waves.reset_wave_memory(
            gen_index=gen_index,
            preserve_wave_specs=preserve_wave_specs,
            clear_last_fifo=clear_last_fifo,
        )

    def reset_envelopes(
        self,
        *,
        gen_index: int,
        preserve_wave_specs: bool = True,
        clear_last_fifo: bool = True,
    ) -> dict:
        """Reset the generator envelope memory and synchronize the High-Level wave cache."""
        return self._waves.reset_envelopes(
            gen_index=gen_index,
            preserve_wave_specs=preserve_wave_specs,
            clear_last_fifo=clear_last_fifo,
        )

    # ========== FIFO Operations Delegation ==========

    def program_drive_sequence(
        self,
        *,
        gen_index: int,
        wave_id_list: list[str],
        start_index: int = 1,
    ) -> dict:
        """Program the generator FIFO with a sequence of wave_ids."""
        return self._fifo.program_drive_sequence(
            gen_index=gen_index,
            wave_id_list=wave_id_list,
            start_index=start_index,
        )

    def set_drive_source(self, *, gen_index: int, source: str, seed: int | None = None) -> dict:
        """Select the source for the drive wave sequence."""
        return self._fifo.set_drive_source(gen_index=gen_index, source=source, seed=seed)

    # ========== Modulation Delegation ==========

    def set_modulation(self, gen_index: int, label: str, mod: dict) -> dict:
        """Configure the Direct Digital Synthesis (DDS) modulation parameters."""
        return self._modulation.set_modulation(gen_index=gen_index, label=label, mod=mod)

    def set_nyquist_zone(self, gen_index: int, label: str, zone: int) -> dict:
        """Set the Nyquist zone for a generator's modulation."""
        return self._modulation.set_nyquist_zone(gen_index=gen_index, label=label, zone=zone)

    # ========== Trigger Listener Delegation ==========

    def set_trigger_listener(self, gen_index: int, trig: dict) -> dict:
        """Configure which trigger channel the generator should listen to."""
        return self._trigger.set_trigger_listener(gen_index=gen_index, trig=trig)


__all__ = ["GeneratorOps"]
