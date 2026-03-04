"""Flat generator operations for OverlayAdapter.

This module provides the GeneratorOps class that handles all generator-related
hardware operations in a single flat class:

- Wave definition and compilation (WDW)
- Envelope upload and processing
- Readout wave configuration
- Wave memory reset and synchronization
- Drive sequence FIFO programming
- DDS modulation and Nyquist zone configuration
- Generator trigger channel assignment

The class owns its own cache state (wave_store, last_fifo, readout_wave_store)
and requires only a LowLevelAccess instance and a logger.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from ...models.config_types import Modulation, TriggerCommand
from ...models.exceptions import ConfigurationError
from .generator_utils._wave_utils import (
    build_wave_entry,
    check_readout_wave_cache,
    check_wave_replacement_policy,
    process_envelope_samples,
    validate_envelope_spec,
    validate_envelope_symmetry,
    validate_fifo_capacity,
    validate_wave_ids_in_cache,
)
from .overlay_adapter_types import WaveEntry

if TYPE_CHECKING:
    from ._low_level_access import LowLevelAccess


class GeneratorOps:
    """Generator operations: waves, envelopes, FIFO, modulation, trigger.

    This class manages all generator-related hardware operations and owns
    its own cache state for wave definitions, FIFO sequences, and readout waves.

    :param ll: Low-level access helper for driver calls and error handling.
    :type ll: LowLevelAccess
    :param logger: Logger instance for debug/error reporting.
    :type logger: logging.Logger
    """

    def __init__(self, ll: LowLevelAccess, logger: logging.Logger) -> None:
        """Initialize GeneratorOps with direct dependencies.

        :param ll: Low-level access helper for driver calls and error handling.
        :type ll: LowLevelAccess
        :param logger: Logger instance for debug/error reporting.
        :type logger: logging.Logger
        """
        self._ll = ll
        self._logger = logger

        # Generator cache state (previously in CacheContainers)
        self._wave_store: dict[int, dict[str, WaveEntry]] = {}
        self._last_fifo: dict[int, list[str]] = {}
        self._readout_wave_store: dict[int, WaveEntry] = {}

    # ========================================================================
    # PUBLIC METHODS — Wave
    # ========================================================================

    def get_wave_cache(self, gen_index: int) -> dict[str, WaveEntry]:
        """Retrieve the High-Level wave cache for a specific generator.

        This method employs lazy initialization: if the cache for the requested
        generator does not exist, an empty dictionary is created, stored, and returned.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: A dictionary mapping wave IDs to their corresponding WaveEntry objects.
        :rtype: dict[str, WaveEntry]
        """
        wave_cache = self._wave_store.get(gen_index)
        if wave_cache is None:
            wave_cache = {}
            self._wave_store[gen_index] = wave_cache
        return wave_cache

    def compile_waves(self, *, gen_index: int, waves: list[dict], replace: bool) -> dict:
        """Compile high-level wave definitions into hardware Wave Definition Words (WDW).

        Handles 'env' (Envelope) and 'vz' (Virtual-Z) wave types. Supports caching to
        skip re-compilation of identical specifications.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param waves: List of dictionaries defining the waves.
        :type waves: list[dict]
        :param replace: If True, allows overwriting existing wave definitions.
        :type replace: bool
        :return: A summary dictionary detailing compiled, replaced, skipped, and failed waves.
        :rtype: dict
        """
        self._logger.debug("compile_waves: gen=%d n=%d", gen_index, len(waves))

        gen = self._ll.get_gen(gen_index)
        out, replaced, skipped, failed = [], [], [], []
        cache: dict[str, WaveEntry] = self.get_wave_cache(gen_index)

        for w in waves:
            try:
                wave_id = str(w["wave_id"])
                new_entry = build_wave_entry(w)
                old_entry = cache.get(wave_id)
                in_hw = wave_id in gen.wave_memory_dict

                action = check_wave_replacement_policy(wave_id, old_entry, new_entry, in_hw, replace)

                if action == "skip":
                    skipped.append(wave_id)
                    new_entry.wdw = old_entry.wdw
                    cache[wave_id] = new_entry
                    out.append({"wave_id": wave_id, "WDW": hex(new_entry.wdw)})
                    self._logger.debug(
                        "compile_waves: wave_id '%s' already present (same spec) -> skipped",
                        wave_id,
                    )
                    continue

                wdw = self._compile_wdw(gen, new_entry)
                self._logger.debug("compile_waves: wave_id '%s' -> WDW=0x%X", wave_id, wdw)
                new_entry.wdw = wdw
                self._store_wdw_in_hardware(gen, wdw, wave_id, action == "replace")

                if action == "replace":
                    replaced.append(wave_id)

                cache[wave_id] = new_entry
                out.append({"wave_id": wave_id, "WDW": hex(wdw)})

            except Exception as ex:
                self._logger.exception("compile_waves: failed wave=%s", w)
                failed.append({"wave_id": w.get("wave_id"), "error": str(ex)})

        self._logger.debug(
            "compile_waves: done gen=%d compiled=%d replaced=%d skipped=%d failed=%d",
            gen_index,
            len(out),
            len(replaced),
            len(skipped),
            len(failed),
        )
        return {
            "gen_index": int(gen_index),
            "waves": out,
            "replaced": replaced,
            "skipped": skipped,
            "failed": failed,
        }

    def upload_readout_wave(self, *, gen_index: int, wave: dict, replace: bool = False) -> dict:
        """Compile and upload a specific wave configuration for readout operations.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param wave: Dictionary containing the wave specification.
        :type wave: dict
        :param replace: If True, allows overwriting an existing readout configuration.
        :type replace: bool
        :return: A dictionary summarizing the upload status and compiled WDW.
        :rtype: dict
        """
        self._logger.debug("upload_readout_wave: gen=%d replace=%s", gen_index, replace)

        gen = self._ll.get_gen(gen_index)

        # parsing switch iq and keeplast
        switch_iq = wave.get("switch_iq", None)
        keep_last = wave.get("keep_last", None)
        # default to false, which removes some possibility for errors
        # TODO: make this convertion strict
        switch_iq = True if (switch_iq and switch_iq == "True") else False
        keep_last = True if (keep_last and keep_last == "True") else False

        new_entry = WaveEntry(
            envelope=str(wave["envelope"]),
            duration=int(wave["duration"]),
            gain=float(wave["gain"]),
            switch_iq=switch_iq,
            keep_last=keep_last,
            wdw=None,
        )

        old_entry, action = check_readout_wave_cache(gen_index, new_entry, self._readout_wave_store, replace)

        if action == "skip":
            new_entry.wdw = old_entry.wdw
            self._readout_wave_store[gen_index] = new_entry
            self._logger.debug(
                "upload_readout_wave: skipped gen=%d (same spec, WDW=0x%X)",
                gen_index,
                new_entry.wdw,
            )
            return {
                "gen_index": gen_index,
                "status": "skipped",
                "envelope": new_entry.envelope,
                "duration": new_entry.duration,
                "gain": new_entry.gain,
                "switch_iq": new_entry.switch_iq,
                "keep_last": new_entry.keep_last,
                "WDW": hex(new_entry.wdw),
            }

        wdw = self._compile_wdw(gen, new_entry)
        new_entry.wdw = wdw

        self._ll.check_result(
            gen.write_readout_wave(wdw),
            operation="write_readout_wave",
        )

        self._readout_wave_store[gen_index] = new_entry
        status = "replaced" if action == "replace" else "compiled"
        self._logger.debug("upload_readout_wave: %s gen=%d WDW=0x%X", status, gen_index, wdw)

        return {
            "gen_index": gen_index,
            "status": status,
            "envelope": new_entry.envelope,
            "duration": new_entry.duration,
            "gain": new_entry.gain,
            "switch_iq": new_entry.switch_iq,
            "keep_last": new_entry.keep_last,
            "WDW": hex(wdw),
        }

    def get_readout_wave_cache(self, gen_index: int) -> WaveEntry | None:
        """Return the WaveEntry currently configured for readout, if any.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: The current WaveEntry or None if not configured.
        :rtype: WaveEntry | None
        """
        return self._readout_wave_store.get(gen_index)

    def reset_wave_memory(
        self,
        *,
        gen_index: int,
        preserve_wave_specs: bool = True,
        clear_last_fifo: bool = True,
    ) -> dict:
        """Reset the generator wave memory and synchronize the High-Level cache.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param preserve_wave_specs: If True, keeps WaveEntry objects but invalidates WDWs.
        :type preserve_wave_specs: bool
        :param clear_last_fifo: If True, clears the record of the last programmed FIFO.
        :type clear_last_fifo: bool
        :return: A summary of the cache state after reset.
        :rtype: dict
        """
        self._logger.debug(
            "reset_wave_memory: gen=%d preserve_wave_specs=%s clear_last_fifo=%s",
            gen_index,
            preserve_wave_specs,
            clear_last_fifo,
        )

        gen = self._ll.get_gen(gen_index)

        self._ll.check_result(
            gen.reset_wave_memory_dict(),
            operation="reset_wave_memory_dict",
        )

        cache = self.get_wave_cache(gen_index)
        n_before = len(cache)

        if preserve_wave_specs:
            for entry in cache.values():
                entry.wdw = None
            hl_action = "invalidated_wdw"
        else:
            cache.clear()
            hl_action = "cleared_cache"

        if clear_last_fifo:
            self._last_fifo.pop(int(gen_index), None)

        # Invalidate readout wave cache (wave memory is shared)
        readout_entry = self._readout_wave_store.get(gen_index)
        if readout_entry is not None:
            if preserve_wave_specs:
                readout_entry.wdw = None
            else:
                self._readout_wave_store.pop(gen_index, None)

        self._logger.debug(
            "reset_wave_memory: done gen=%d hl_action=%s n_before=%d n_after=%d",
            gen_index,
            hl_action,
            n_before,
            len(cache),
        )

        return {
            "gen_index": int(gen_index),
            "hl_action": hl_action,
            "hl_wave_count_before": n_before,
            "hl_wave_count_after": len(cache),
            "cleared_last_fifo": bool(clear_last_fifo),
        }

    # ========================================================================
    # PUBLIC METHODS — Envelope
    # ========================================================================

    def get_envelope_names(self, gen_index: int) -> list[str]:
        """Retrieve the list of envelope names currently stored in the generator's memory.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: A list of envelope names available in the hardware driver.
        :rtype: list[str]
        """
        gen = self._ll.get_gen(gen_index)
        return list(getattr(gen, "envelope_memory_dict", {}).keys())

    def upload_envelopes(
        self,
        *,
        gen_index: int,
        envelopes: list,
        auto_pad_noninterp: bool = True,
    ) -> dict:
        """Upload multiple envelopes into generator envelope memory.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param envelopes: List of envelope specifications to upload.
        :type envelopes: list
        :param auto_pad_noninterp: If True, automatically zero-pads non-interpolated envelopes.
        :type auto_pad_noninterp: bool
        :return: A summary dictionary containing lists of loaded, skipped, and failed names.
        :rtype: dict
        """
        self._logger.debug(
            "upload_envelopes: gen=%d, n=%d, auto_pad_noninterp=%s",
            gen_index,
            len(envelopes),
            auto_pad_noninterp,
        )

        gen = self._ll.get_gen(gen_index)
        loaded: list[str] = []
        skipped: list[str] = []
        failed: list[dict] = []

        env_cache = getattr(gen, "EnvelopeMemoryDict", {})

        for e in envelopes:
            name = str(e.get("name", ""))
            try:
                validate_envelope_spec(name)

                if name in env_cache:
                    self._logger.debug(
                        "upload_envelopes: skip '%s' (already in EnvelopeMemoryDict)",
                        name,
                    )
                    skipped.append(name)
                    continue

                for_interp = bool(e["for_interpolation"])
                is_sym = bool(e["is_symmetric"])
                i_even = bool(e["i_even"])
                q_even = bool(e["q_even"])
                samples_iq = e["samples_iq"]

                env, original_size = process_envelope_samples(
                    samples_iq, for_interp, int(gen.sample_size), int(gen.number_of_channels), auto_pad_noninterp
                )
                if auto_pad_noninterp and not for_interp and int(env.size) > original_size:
                    self._logger.debug(
                        "upload_envelopes: padded from %d to %d (par=%d)",
                        original_size,
                        int(env.size),
                        int(gen.number_of_channels),
                    )

                i_even, q_even = validate_envelope_symmetry(is_sym, i_even, q_even, for_interp)

                self._ll.check_result(
                    gen.add_envelope_to_envelope_memory(env, for_interp, is_sym, i_even, q_even, name),
                    operation="add_envelope_to_envelope_memory",
                )
                loaded.append(name)

            except Exception as ex:
                self._logger.exception("upload_envelopes: failed '%s'", name)
                failed.append({"name": name, "error": str(ex)})

        self._logger.debug(
            "upload_envelopes: done gen=%d loaded=%d skipped=%d failed=%d",
            gen_index,
            len(loaded),
            len(skipped),
            len(failed),
        )
        return {
            "gen_index": int(gen_index),
            "loaded": loaded,
            "skipped": skipped,
            "failed": failed,
        }

    def reset_envelopes(
        self,
        *,
        gen_index: int,
        preserve_wave_specs: bool = True,
        clear_last_fifo: bool = True,
    ) -> dict:
        """Reset the generator envelope memory and synchronize the High-Level wave cache.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param preserve_wave_specs: If True, retains WaveEntry specs but invalidates WDWs.
        :type preserve_wave_specs: bool
        :param clear_last_fifo: If True, clears the record of the last programmed sequence.
        :type clear_last_fifo: bool
        :return: A summary of the actions taken on the cache.
        :rtype: dict
        """
        self._logger.debug(
            "reset_envelopes: gen=%d preserve_wave_specs=%s clear_last_fifo=%s",
            gen_index,
            preserve_wave_specs,
            clear_last_fifo,
        )

        gen = self._ll.get_gen(gen_index)

        self._ll.check_result(
            gen.reset_envelope_dict(),
            operation="reset_envelope_dict",
        )

        cache = self.get_wave_cache(gen_index)
        n_before = len(cache)

        if preserve_wave_specs:
            for entry in cache.values():
                entry.wdw = None
            hl_action = "invalidated_wdw"
        else:
            cache.clear()
            hl_action = "cleared_cache"

        if clear_last_fifo:
            self._last_fifo.pop(int(gen_index), None)

        # FIX C8: Invalidate readout_wave_store (was missing in original code).
        # Envelopes are shared with readout waves, so resetting envelopes must
        # also invalidate any readout wave that depends on them.
        readout_entry = self._readout_wave_store.get(gen_index)
        if readout_entry is not None:
            if preserve_wave_specs:
                readout_entry.wdw = None
            else:
                self._readout_wave_store.pop(gen_index, None)

        self._logger.debug(
            "reset_envelopes: done gen=%d hl_action=%s n_before=%d n_after=%d",
            gen_index,
            hl_action,
            n_before,
            len(cache),
        )

        return {
            "gen_index": int(gen_index),
            "hl_action": hl_action,
            "hl_wave_count_before": n_before,
            "hl_wave_count_after": len(cache),
            "cleared_last_fifo": bool(clear_last_fifo),
        }

    # ========================================================================
    # PUBLIC METHODS — FIFO
    # ========================================================================

    def program_drive_sequence(
        self,
        *,
        gen_index: int,
        wave_id_list: list[str],
        start_index: int = 1,
    ) -> dict:
        """Program the generator FIFO with a sequence of wave_ids.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param wave_id_list: Ordered list of wave IDs to execute.
        :type wave_id_list: list[str]
        :param start_index: FIFO index to start writing at (default 1).
        :type start_index: int
        :return: A dictionary containing the updated FIFO sequence.
        :rtype: dict
        """
        self._logger.debug("program_drive_sequence: gen=%d n=%d", gen_index, len(wave_id_list))

        gen = self._ll.get_gen(gen_index)
        cache = self.get_wave_cache(gen_index)
        start_index = int(start_index)

        if start_index < 1:
            raise ConfigurationError(f"program_drive_sequence: start_index must be >= 1, got {start_index}")

        validate_fifo_capacity(int(gen.memory_mapped_fifo_segment_depth), start_index, len(wave_id_list))
        validate_wave_ids_in_cache(cache, wave_id_list, gen.wave_memory_dict)

        self.set_drive_source(gen_index=gen_index, source="fifo")
        self._logger.debug("program_drive_sequence: set_drive_source(gen=%d, source='fifo')", gen_index)

        for i, wave_id in enumerate(wave_id_list, start=start_index):
            wave_addr = gen.wave_memory_dict.get(wave_id, "UNKNOWN")
            self._logger.debug(
                "program_drive_sequence: FIFO[%d] = wave_id='%s' addr=%s",
                i,
                wave_id,
                wave_addr,
            )
            self._ll.check_result(
                gen.add_wave_to_drive_wave_sequence(i, wave_id),
                operation="add_wave_to_drive_wave_sequence",
            )

        new_fifo = self._update_fifo_cache(gen_index, wave_id_list, start_index)

        self._logger.debug(
            "program_drive_sequence: done gen=%d fifo_len=%d",
            gen_index,
            len(wave_id_list),
        )
        return {"gen_index": int(gen_index), "fifo": new_fifo}

    def set_drive_source(
        self,
        *,
        gen_index: int,
        source: Literal["fifo", "lfsr"],
        seed: int | None = None,
    ) -> dict:
        """Select the source for the drive wave sequence.

        If source="lfsr" and seed is provided, the LFSR seed is programmed before
        enabling LFSR. If source="fifo", the seed parameter is ignored.

        :param gen_index: Index of the generator.
        :type gen_index: int
        :param source: Selection between "fifo" (programmed sequence) or "lfsr".
        :type source: str
        :param seed: Optional LFSR seed value. Used only when source="lfsr".
        :type seed: int | None
        :return: Selected source status (and seed, if applied).
        :rtype: dict
        """
        self._logger.debug("set_drive_source: gen=%d source=%s seed=%s", gen_index, source, seed)

        gen = self._ll.get_gen(gen_index)

        source_lower = str(source).lower()
        if source_lower == "fifo":
            source_val = 0

        elif source_lower == "lfsr":
            source_val = 1

            if seed is not None:
                self._ll.check_result(
                    gen.set_lfsr_seed(int(seed)),
                    operation="set_lfsr_seed",
                )
        else:
            raise ConfigurationError(f"set_drive_source: invalid source='{source}'. Use 'fifo' or 'lfsr'.")

        self._ll.check_result(
            gen.set_drive_order_source(source_val),
            operation="set_drive_order_source",
        )

        self._logger.debug("set_drive_source: done gen=%d source=%s", gen_index, source_lower)

        out = {"gen_index": int(gen_index), "source": source_lower}
        if source_lower == "lfsr" and seed is not None:
            out["seed"] = int(seed)
        return out

    # ========================================================================
    # PUBLIC METHODS — Modulation
    # ========================================================================

    def set_modulation(self, gen_index: int, label: str, mod: Modulation) -> dict:
        """Configure the Direct Digital Synthesis (DDS) modulation parameters.

        This method handles both the digital frequency synthesis configuration and the
        analog-domain Mix-Mode settings (Nyquist zone selection) based on the target frequency.

        :param gen_index: The index of the target generator.
        :type gen_index: int
        :param label: The modulation context, must be either 'drive' or 'readout'.
        :type label: str
        :param mod: A dictionary containing the modulation parameters (frequency in MHz, phase in degrees).
        :type mod: Modulation
        :return: A summary of the applied modulation configuration.
        :rtype: dict
        :raises ConfigurationError: If the ``label`` is not 'drive' or 'readout'.
        """
        freq_mhz = mod["frequency_mhz"]
        phase = mod["phase"]

        self._logger.debug(
            "set_modulation: gen=%d label=%s frequency=%f phase=%s",
            gen_index,
            label,
            freq_mhz,
            phase,
        )
        unit = self._ll.get_gen(gen_index)

        self._ll.configure_dac_mix_mode(gen_index, label, freq_mhz)

        if label == "drive":
            self._ll.check_result(
                unit.set_drive_dds_parameters(
                    frequency=freq_mhz,
                    dac_samplerate=self._ll.dac_sr_mhz(),
                ),
                operation="set_drive_dds_parameters",
            )
        elif label == "readout":
            self._ll.check_result(
                unit.set_readout_dds_parameters(
                    frequency=freq_mhz,
                    phase=phase,
                    dac_samplerate=self._ll.dac_sr_mhz(),
                ),
                operation="set_readout_dds_parameters",
            )
        else:
            raise ConfigurationError("Invalid mode selection!\nHint: select label = 'drive' or 'readout'")

        return {
            "gen_index": gen_index,
            "label": label,
            "frequency_mhz": freq_mhz,
            "phase": phase,
        }

    def set_nyquist_zone(self, gen_index: int, label: str, zone: int) -> dict:
        """Set the Nyquist zone for a generator's modulation.

        This method explicitly sets the Mix-Mode Nyquist zone for a generator's
        drive or readout path. The zone determines which Nyquist band is used for
        the analog Mix-Mode configuration in the RF frontend.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param label: Modulation context ('drive' or 'readout').
        :type label: str
        :param zone: Target Nyquist zone (typically 1 or 2).
        :type zone: int
        :return: Summary of applied zone configuration.
        :rtype: dict
        """
        self._logger.debug(
            "set_nyquist_zone: gen=%d label=%s zone=%d",
            gen_index,
            label,
            zone,
        )

        try:
            try:
                dac_nyquist_hz = self._ll.hw_specs["summary"]["dac_nyquist_hz"]
            except (KeyError, TypeError, AttributeError):
                dac_nyquist_hz = 2.0e9

            freq_mhz = dac_nyquist_hz / 1e6 * (0.5 if zone == 1 else zone - 0.5)
            mix_info = self._ll.configure_dac_mix_mode(gen_index, label, freq_mhz)

            if mix_info is not None:
                return {
                    "gen_index": gen_index,
                    "label": label,
                    "nyquist_zone": mix_info.get("nyquist_zone", zone),
                    "amd_zone": mix_info.get("amd_zone"),
                }
            return {
                "gen_index": gen_index,
                "label": label,
                "nyquist_zone": zone,
                "status": "mocked",
            }
        except (ValueError, KeyError, AttributeError) as e:
            self._logger.error(f"Failed to set Nyquist zone: {e}")
            raise

    # ========================================================================
    # PUBLIC METHODS — Trigger Listener
    # ========================================================================

    def set_trigger_listener(self, gen_index: int, trig: TriggerCommand) -> dict:
        """Configure which trigger channel the generator should listen to.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param trig: Dictionary defining the trigger type and source channel.
        :type trig: TriggerCommand
        :return: The applied trigger configuration.
        :rtype: dict
        """
        channel = trig["channel"]
        ttype = trig["ttype"]

        self._logger.debug(
            "set_trigger_listener: gen=%d ttype=%s channel=%s",
            gen_index,
            ttype,
            channel,
        )
        unit = self._ll.get_gen(gen_index)

        self._ll.check_result(
            unit.set_trigger_channel(channel=channel, ttype=ttype),
            operation="set_trigger_channel",
        )

        if channel == 0:
            self._logger.debug("Generator %d is deaf to any trigger!", gen_index)
        else:
            self._logger.debug(
                "Generator %d listens to %s_trigger_word channel %d",
                gen_index,
                ttype,
                channel,
            )

        return {
            "gen_index": gen_index,
            "ttype": ttype,
            "channel": channel,
        }

    # ========================================================================
    # INTERNAL HELPERS — Wave
    # ========================================================================

    def _compile_wdw(self, gen: object, entry: WaveEntry) -> int:
        """Compile WaveEntry to Wave Definition Word (WDW).

        :param gen: Generator device object.
        :param entry: WaveEntry to compile.
        :return: Integer WDW value.
        """
        if entry.kind == "env":
            return int(
                self._ll.check_result(
                    gen.create_wave_definition_word(
                        entry.envelope,
                        entry.duration,
                        entry.gain,
                        entry.switch_iq,
                        entry.keep_last,
                    ),
                    operation="create_wave_definition_word",
                )
            )
        else:
            return int(
                self._ll.check_result(
                    gen.create_vz_gate_definition_word(entry.vz_phase_rad),
                    operation="create_vz_gate_definition_word",
                )
            )

    def _store_wdw_in_hardware(self, gen: object, wdw: int, wave_id: str, replace: bool) -> None:
        """Store or replace WDW in generator hardware memory.

        :param gen: Generator device object.
        :param wdw: Wave Definition Word value to store.
        :param wave_id: Wave identifier.
        :param replace: If True, replace existing wave; else add new.
        """
        if replace:
            self._ll.check_result(
                gen.replace_wave_in_wave_memory(wdw, wave_id, wave_id),
                operation="replace_wave_in_wave_memory",
            )
        else:
            self._ll.check_result(
                gen.add_wave_in_wave_memory(wdw, wave_id),
                operation="add_wave_in_wave_memory",
            )

    # ========================================================================
    # INTERNAL HELPERS — FIFO
    # ========================================================================

    def _update_fifo_cache(
        self,
        gen_index: int,
        wave_id_list: list[str],
        start_index: int,
    ) -> list[str]:
        """Update FIFO cache with new sequence and return complete FIFO.

        :param gen_index: Generator index.
        :param wave_id_list: Wave IDs being programmed.
        :param start_index: FIFO write start index.
        :return: Updated complete FIFO sequence.
        :raises ConfigurationError: If patching from non-1 start_index with insufficient cache.
        """
        end_index = start_index + len(wave_id_list) - 1
        prev = self._last_fifo.get(int(gen_index), [])

        if start_index == 1:
            new_fifo = list(wave_id_list)
        else:
            if len(prev) < (start_index - 1):
                raise ConfigurationError(
                    f"program_drive_sequence: cannot patch from start_index={start_index} "
                    f"because last_fifo has only {len(prev)} entries. "
                    f"Program from 1 first, then patch."
                )
            suffix = prev[end_index:] if len(prev) >= end_index else []
            new_fifo = prev[: start_index - 1] + list(wave_id_list) + suffix

        self._last_fifo[int(gen_index)] = new_fifo
        return new_fifo


__all__ = ["GeneratorOps"]
