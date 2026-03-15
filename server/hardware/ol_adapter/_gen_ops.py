"""Generator operations for OverlayAdapter.

This module provides the ``GeneratorOps`` class that handles all
generator-related hardware operations in a single linear class:

- Wave definition and compilation (WDW)
- Envelope upload and processing
- Readout wave configuration
- Wave memory reset and synchronization
- Drive sequence FIFO programming
- DDS modulation and Nyquist zone configuration
- Generator trigger channel assignment

The class owns its own cache state (``_wave_store``, ``_last_fifo``,
``_readout_wave_store``).
"""

from __future__ import annotations

import logging
from typing import Literal

from ...models.config_types import Modulation, TriggerCommand
from ...models.exceptions import ConfigurationError
from ._errors import check_driver_result
from .overlay_adapter_types import (
    EnvelopeSpec,
    ReadoutWaveSpec,
    WaveEntry,
    process_envelope_samples,
    validate_envelope_spec,
    validate_envelope_symmetry,
    validate_fifo_capacity,
    validate_wave_ids_in_cache,
)


class GeneratorOps:
    """Generator operations: waves, envelopes, FIFO, modulation, trigger.

    :param fireq_soc: The FIREQ_SoC hardware driver instance.
    :type fireq_soc: FIREQ_SoC-compatible
    :param logger: Logger instance for debug/error reporting.
    :type logger: logging.Logger
    """

    _DRIVER_NAME = "GeneratorDriver"

    def __init__(self, fireq_soc: object, logger: logging.Logger) -> None:
        self._fireq_soc = fireq_soc
        self._logger = logger

        self._wave_store: dict[int, dict[str, WaveEntry]] = {}
        self._last_fifo: dict[int, list[str]] = {}
        self._readout_wave_store: dict[int, WaveEntry] = {}

    # ========================================================================
    # Private helpers
    # ========================================================================

    def _get_gen(self, gen_index: int) -> object:
        """Retrieve the low-level driver for a specific generator.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: The low-level generator driver instance.
        :rtype: object
        :raises ConfigurationError: If the index is out of bounds or invalid.
        """
        try:
            return self._fireq_soc.generators[int(gen_index)]
        except Exception as e:
            raise ConfigurationError(f"Invalid gen_index={gen_index}") from e

    def _check(self, result: object, *, operation: str, hint: str | None = None) -> object:
        """Check a driver return code and raise on error.

        :param result: Raw return value from the driver method.
        :type result: object
        :param operation: Name of the driver operation.
        :type operation: str
        :param hint: Explicit diagnostic hint.
        :type hint: str | None
        :return: The original result on success.
        :rtype: object
        :raises ConfigurationError: If the result is a negative integer.
        """
        return check_driver_result(
            result,
            operation=operation,
            driver_name=self._DRIVER_NAME,
            logger=self._logger,
            hint=hint,
        )

    def _compile_wdw(self, gen: object, entry: WaveEntry) -> int:
        """Compile a WaveEntry into a Wave Definition Word (WDW).

        :param gen: Generator device object.
        :param entry: WaveEntry to compile.
        :return: Integer WDW value.
        """
        if entry.kind == "env":
            return int(
                self._check(
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
        return int(
            self._check(
                gen.create_vz_gate_definition_word(entry.vz_phase_rad),
                operation="create_vz_gate_definition_word",
            )
        )

    def _store_wdw_in_hardware(self, gen: object, wdw: int, wave_id: str, replace: bool) -> None:
        """Store or replace a WDW in generator hardware memory.

        :param gen: Generator device object.
        :param wdw: Wave Definition Word value to store.
        :param wave_id: Wave identifier.
        :param replace: If True, replace existing wave; else add new.
        """
        if replace:
            self._check(
                gen.replace_wave_in_wave_memory(wdw, wave_id, wave_id),
                operation="replace_wave_in_wave_memory",
            )
        else:
            self._check(
                gen.add_wave_in_wave_memory(wdw, wave_id),
                operation="add_wave_in_wave_memory",
            )

    def _sync_cache_after_reset(self, gen_index: int, clear_last_fifo: bool) -> int:
        """Clear HL caches after a hardware reset.

        Used by both ``reset_wave_memory`` and ``reset_envelopes``.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param clear_last_fifo: If True, also clears the FIFO cache.
        :type clear_last_fifo: bool
        :return: Number of wave entries that were in cache before clearing.
        :rtype: int
        """
        cache = self.get_wave_cache(gen_index)
        n_before = len(cache)
        cache.clear()

        if clear_last_fifo:
            self._last_fifo.pop(int(gen_index), None)

        self._readout_wave_store.pop(gen_index, None)

        return n_before

    def _reset_memory(
        self,
        *,
        gen_index: int,
        driver_method: str,
        clear_last_fifo: bool,
    ) -> dict:
        """Shared logic for ``reset_wave_memory`` and ``reset_envelopes``.

        Calls the specified LL driver method, then clears the HL caches.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param driver_method: Name of the driver method to call
            (``"reset_wave_memory_dict"`` or ``"reset_envelope_dict"``).
        :type driver_method: str
        :param clear_last_fifo: If True, also clears the FIFO cache.
        :type clear_last_fifo: bool
        :return: Summary of the cache state after reset.
        :rtype: dict
        """
        self._logger.debug(
            "%s: gen=%d clear_last_fifo=%s",
            driver_method,
            gen_index,
            clear_last_fifo,
        )

        gen = self._get_gen(gen_index)
        self._check(getattr(gen, driver_method)(), operation=driver_method)

        n_before = self._sync_cache_after_reset(gen_index, clear_last_fifo)

        self._logger.debug(
            "%s: done gen=%d cleared=%d entries",
            driver_method,
            gen_index,
            n_before,
        )

        return {
            "gen_index": int(gen_index),
            "reset_action": "cleared_cache",
            "hl_wave_count_before": n_before,
            "hl_wave_count_after": 0,
            "cleared_last_fifo": bool(clear_last_fifo),
        }

    def _configure_dac_mix_mode(self, gen_index: int, label: str, freq_mhz: float) -> dict | None:
        """Configure the DAC Mix-Mode (Nyquist zone) for a generator.

        Silently returns ``None`` if the driver does not support mix-mode
        configuration (e.g. on mock overlays or older bitstreams).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param label: Modulation context ('drive' or 'readout').
        :type label: str
        :param freq_mhz: Target frequency in MHz.
        :type freq_mhz: float
        :return: Mix-mode info dictionary from the driver, or ``None``.
        :rtype: dict | None
        """
        try:
            mix_info = self._fireq_soc.configure_dac_mix_mode(gen_index, label, freq_mhz)
            if mix_info.get("changed"):
                self._logger.debug(
                    "DAC Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                    mix_info["nyquist_zone"],
                    mix_info["amd_zone"],
                    mix_info["tile"],
                    mix_info["block"],
                )
            return mix_info
        except ValueError as e:
            self._logger.warning("DAC Mix-mode config skipped: %s", e)
            return None

    def _update_fifo_cache(
        self,
        gen_index: int,
        wave_id_list: list[str],
        start_index: int,
    ) -> list[str]:
        """Update the FIFO cache and return the complete sequence.

        :param gen_index: Generator index.
        :param wave_id_list: Wave IDs being programmed.
        :param start_index: FIFO write start index.
        :return: Updated complete FIFO sequence.
        :raises ConfigurationError: If patching with insufficient cache.
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

    # ========================================================================
    # Wave compilation
    # ========================================================================

    def get_wave_cache(self, gen_index: int) -> dict[str, WaveEntry]:
        """Retrieve the HL wave cache for a generator (lazy-initialized).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: Dictionary mapping wave IDs to WaveEntry objects.
        :rtype: dict[str, WaveEntry]
        """
        return self._wave_store.setdefault(gen_index, {})

    def compile_waves(self, *, gen_index: int, waves: list[dict], replace: bool) -> dict:
        """Compile wave definitions into hardware Wave Definition Words (WDW).

        Handles 'env' (Envelope) and 'vz' (Virtual-Z) wave types.
        Identical specifications are skipped via cache.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param waves: List of wave specification dictionaries.
        :type waves: list[dict]
        :param replace: If True, allows overwriting existing wave definitions.
        :type replace: bool
        :return: Summary with compiled, replaced, skipped, and failed waves.
        :rtype: dict
        """
        self._logger.debug("compile_waves: gen=%d n=%d", gen_index, len(waves))

        gen = self._get_gen(gen_index)
        out, replaced, skipped, failed = [], [], [], []
        cache: dict[str, WaveEntry] = self.get_wave_cache(gen_index)

        for wave_spec in waves:
            try:
                wave_id = str(wave_spec["wave_id"])
                new_entry = WaveEntry.from_spec(wave_spec)
                old_entry = cache.get(wave_id)
                in_hw = wave_id in gen.wave_memory_dict

                # Skip recompilation if the spec hasn't changed
                if old_entry is not None and old_entry.same_spec(new_entry) and in_hw and old_entry.wdw is not None:
                    skipped.append(wave_id)
                    new_entry.wdw = old_entry.wdw
                    cache[wave_id] = new_entry
                    out.append({"wave_id": wave_id, "WDW": hex(new_entry.wdw)})
                    self._logger.debug("compile_waves: '%s' same spec -> skipped", wave_id)
                    continue

                if old_entry is not None and not old_entry.same_spec(new_entry) and not replace:
                    raise ConfigurationError(
                        f"wave_id '{wave_id}' already exists but spec differs. "
                        f"OLD={old_entry} NEW={new_entry}. "
                        f"Hint: set replace=True or use a different wave_id."
                    )

                if old_entry is None and in_hw and not replace:
                    raise ConfigurationError(
                        f"wave_id '{wave_id}' exists in HW but not in HL cache. "
                        f"Hint: set replace=True to re-sync or rebuild HL cache."
                    )

                # --- compile and store ---
                wdw = self._compile_wdw(gen, new_entry)
                new_entry.wdw = wdw
                self._store_wdw_in_hardware(gen, wdw, wave_id, in_hw)

                if in_hw:
                    replaced.append(wave_id)

                cache[wave_id] = new_entry
                out.append({"wave_id": wave_id, "WDW": hex(wdw)})
                self._logger.debug("compile_waves: '%s' -> WDW=0x%X", wave_id, wdw)

            except Exception as ex:
                self._logger.exception("compile_waves: failed wave=%s", wave_spec)
                failed.append({"wave_id": wave_spec.get("wave_id"), "error": str(ex)})

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

    # ========================================================================
    # Readout wave
    # ========================================================================

    def upload_readout_wave(
        self,
        *,
        gen_index: int,
        wave: ReadoutWaveSpec,
        replace: bool = False,
    ) -> dict:
        """Compile and upload a readout wave configuration.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param wave: Readout wave specification.
        :type wave: ReadoutWaveSpec
        :param replace: If True, allows overwriting an existing configuration.
        :type replace: bool
        :return: Summary of the upload status and compiled WDW.
        :rtype: dict
        """
        self._logger.debug("upload_readout_wave: gen=%d replace=%s", gen_index, replace)

        gen = self._get_gen(gen_index)
        new_entry = WaveEntry.from_readout_spec(wave)
        old_entry = self._readout_wave_store.get(gen_index)

        # Skip recompilation if the spec hasn't changed
        if old_entry is not None and old_entry.same_spec(new_entry) and old_entry.wdw is not None:
            new_entry.wdw = old_entry.wdw
            self._readout_wave_store[gen_index] = new_entry
            self._logger.debug("upload_readout_wave: skipped gen=%d (same spec)", gen_index)
            return new_entry.to_readout_result(gen_index, "skipped")

        if old_entry is not None and not old_entry.same_spec(new_entry) and not replace:
            raise ConfigurationError(
                f"Readout wave for gen_index={gen_index} already exists but "
                f"spec differs. OLD={old_entry} NEW={new_entry}. "
                f"Hint: set replace=True to overwrite."
            )

        # --- compile and upload ---
        wdw = self._compile_wdw(gen, new_entry)
        new_entry.wdw = wdw

        self._check(gen.write_readout_wave(wdw), operation="write_readout_wave")

        self._readout_wave_store[gen_index] = new_entry
        status = "replaced" if old_entry is not None else "compiled"
        self._logger.debug("upload_readout_wave: %s gen=%d WDW=0x%X", status, gen_index, wdw)

        return new_entry.to_readout_result(gen_index, status)

    def get_readout_wave_cache(self, gen_index: int) -> WaveEntry | None:
        """Return the WaveEntry currently configured for readout, if any.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: The current WaveEntry or None if not configured.
        :rtype: WaveEntry | None
        """
        return self._readout_wave_store.get(gen_index)

    # ========================================================================
    # Envelope upload
    # ========================================================================

    def get_envelope_names(self, gen_index: int) -> list[str]:
        """Retrieve envelope names currently stored in the generator's memory.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: List of envelope names in the hardware driver.
        :rtype: list[str]
        """
        gen = self._get_gen(gen_index)
        return list(getattr(gen, "envelope_memory_dict", {}).keys())

    def upload_envelopes(
        self,
        *,
        gen_index: int,
        envelopes: list[EnvelopeSpec],
        auto_pad_noninterp: bool = True,
    ) -> dict:
        """Upload multiple envelopes into generator envelope memory.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param envelopes: List of envelope specifications.
        :type envelopes: list[EnvelopeSpec]
        :param auto_pad_noninterp: If True, zero-pads non-interpolated envelopes.
        :type auto_pad_noninterp: bool
        :return: Summary with loaded, skipped, and failed names.
        :rtype: dict
        """
        self._logger.debug(
            "upload_envelopes: gen=%d n=%d auto_pad=%s",
            gen_index,
            len(envelopes),
            auto_pad_noninterp,
        )

        gen = self._get_gen(gen_index)
        loaded: list[str] = []
        skipped: list[str] = []
        failed: list[dict] = []

        env_cache = getattr(gen, "envelope_memory_dict", {})

        for env_spec in envelopes:
            name = str(env_spec.get("name", ""))
            try:
                # Validate and check for duplicates
                validate_envelope_spec(name)
                if name in env_cache:
                    self._logger.debug("upload_envelopes: skip '%s' (already loaded)", name)
                    skipped.append(name)
                    continue

                # Extract spec fields
                for_interp = bool(env_spec["for_interpolation"])
                is_sym = bool(env_spec["is_symmetric"])
                i_even = bool(env_spec["i_even"])
                q_even = bool(env_spec["q_even"])
                samples_iq = env_spec["samples_iq"]

                # Convert float IQ samples to hardware format and auto-pad
                env, original_size = process_envelope_samples(
                    samples_iq, for_interp, int(gen.sample_size), int(gen.number_of_channels), auto_pad_noninterp
                )
                if auto_pad_noninterp and not for_interp and int(env.size) > original_size:
                    self._logger.debug(
                        "upload_envelopes: padded '%s' from %d to %d",
                        name,
                        original_size,
                        int(env.size),
                    )

                i_even, q_even = validate_envelope_symmetry(is_sym, i_even, q_even, for_interp)

                # Upload to hardware
                self._check(
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

    # ========================================================================
    # Memory reset
    # ========================================================================

    def reset_wave_memory(self, *, gen_index: int, clear_last_fifo: bool = True) -> dict:
        """Reset generator wave memory and clear the HL cache.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param clear_last_fifo: If True, also clears the FIFO cache.
        :type clear_last_fifo: bool
        :return: Summary of the cache state after reset.
        :rtype: dict
        """
        return self._reset_memory(
            gen_index=gen_index,
            driver_method="reset_wave_memory_dict",
            clear_last_fifo=clear_last_fifo,
        )

    def reset_envelopes(self, *, gen_index: int, clear_last_fifo: bool = True) -> dict:
        """Reset generator envelope memory and clear the HL wave cache.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param clear_last_fifo: If True, also clears the FIFO cache.
        :type clear_last_fifo: bool
        :return: Summary of the cache state after reset.
        :rtype: dict
        """
        return self._reset_memory(
            gen_index=gen_index,
            driver_method="reset_envelope_dict",
            clear_last_fifo=clear_last_fifo,
        )

    # ========================================================================
    # FIFO programming
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
        :return: Dictionary containing the updated FIFO sequence.
        :rtype: dict
        """
        self._logger.debug("program_drive_sequence: gen=%d n=%d", gen_index, len(wave_id_list))

        gen = self._get_gen(gen_index)
        cache = self.get_wave_cache(gen_index)
        start_index = int(start_index)

        if start_index < 1:
            raise ConfigurationError(f"program_drive_sequence: start_index must be >= 1, got {start_index}")

        validate_fifo_capacity(int(gen.memory_mapped_fifo_segment_depth), start_index, len(wave_id_list))
        validate_wave_ids_in_cache(cache, wave_id_list, gen.wave_memory_dict)

        self.set_drive_source(gen_index=gen_index, source="fifo")

        for i, wave_id in enumerate(wave_id_list, start=start_index):
            self._logger.debug(
                "program_drive_sequence: FIFO[%d] = '%s'",
                i,
                wave_id,
            )
            self._check(
                gen.add_wave_to_drive_wave_sequence(i, wave_id),
                operation="add_wave_to_drive_wave_sequence",
            )

        new_fifo = self._update_fifo_cache(gen_index, wave_id_list, start_index)

        self._logger.debug("program_drive_sequence: done gen=%d fifo_len=%d", gen_index, len(new_fifo))
        return {"gen_index": int(gen_index), "fifo": new_fifo}

    def set_drive_source(
        self,
        *,
        gen_index: int,
        source: Literal["fifo", "lfsr"],
        seed: int | None = None,
    ) -> dict:
        """Select the source for the drive wave sequence.

        :param gen_index: Index of the generator.
        :type gen_index: int
        :param source: ``"fifo"`` (programmed sequence) or ``"lfsr"``.
        :type source: str
        :param seed: Optional LFSR seed value (only used when source="lfsr").
        :type seed: int | None
        :return: Selected source status (and seed, if applied).
        :rtype: dict
        """
        self._logger.debug("set_drive_source: gen=%d source=%s seed=%s", gen_index, source, seed)

        gen = self._get_gen(gen_index)

        source_lower = str(source).lower()
        if source_lower == "fifo":
            source_val = 0
        elif source_lower == "lfsr":
            source_val = 1
            if seed is not None:
                self._check(gen.set_lfsr_seed(int(seed)), operation="set_lfsr_seed")
        else:
            raise ConfigurationError(f"set_drive_source: invalid source='{source}'. Use 'fifo' or 'lfsr'.")

        self._check(gen.set_drive_order_source(source_val), operation="set_drive_order_source")

        out = {"gen_index": int(gen_index), "source": source_lower}
        if source_lower == "lfsr" and seed is not None:
            out["seed"] = int(seed)
        return out

    # ========================================================================
    # DDS modulation
    # ========================================================================

    def set_modulation(self, gen_index: int, label: str, mod: Modulation) -> dict:
        """Configure DDS modulation parameters for drive or readout.

        Handles both digital frequency synthesis and analog Mix-Mode settings.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param label: ``'drive'`` or ``'readout'``.
        :type label: str
        :param mod: Modulation parameters (frequency_mhz, phase).
        :type mod: Modulation
        :return: Summary of the applied configuration.
        :rtype: dict
        :raises ConfigurationError: If label is not 'drive' or 'readout'.
        """
        freq_mhz = mod["frequency_mhz"]
        phase = mod["phase"]

        self._logger.debug(
            "set_modulation: gen=%d label=%s freq=%f phase=%s",
            gen_index,
            label,
            freq_mhz,
            phase,
        )
        gen = self._get_gen(gen_index)
        dac_sr_mhz = float(self._fireq_soc.hw_specs["summary"]["dac_sr_hz"]) / 1e6

        self._configure_dac_mix_mode(gen_index, label, freq_mhz)

        if label == "drive":
            self._check(
                gen.set_drive_dds_parameters(frequency=freq_mhz, dac_samplerate=dac_sr_mhz),
                operation="set_drive_dds_parameters",
            )
        elif label == "readout":
            self._check(
                gen.set_readout_dds_parameters(frequency=freq_mhz, phase=phase, dac_samplerate=dac_sr_mhz),
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
        """Set the Nyquist zone for a generator's modulation path.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param label: ``'drive'`` or ``'readout'``.
        :type label: str
        :param zone: Target Nyquist zone (typically 1 or 2).
        :type zone: int
        :return: Summary of applied zone configuration.
        :rtype: dict
        """
        self._logger.debug("set_nyquist_zone: gen=%d label=%s zone=%d", gen_index, label, zone)

        specs = getattr(self._fireq_soc, "hw_specs", {})
        dac_nyquist_hz = specs.get("summary", {}).get("dac_nyquist_hz", 2.0e9)

        # Convert zone number to a representative frequency for DAC mix-mode
        freq_mhz = dac_nyquist_hz / 1e6 * (0.5 if zone == 1 else zone - 0.5)
        mix_info = self._configure_dac_mix_mode(gen_index, label, freq_mhz)

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

    # ========================================================================
    # Trigger listener
    # ========================================================================

    def set_trigger_listener(self, gen_index: int, trig: TriggerCommand) -> dict:
        """Configure which trigger channel the generator listens to.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param trig: Trigger type and source channel.
        :type trig: TriggerCommand
        :return: The applied trigger configuration.
        :rtype: dict
        """
        channel = trig["channel"]
        ttype = trig["ttype"]

        self._logger.debug("set_trigger_listener: gen=%d ttype=%s channel=%s", gen_index, ttype, channel)
        gen = self._get_gen(gen_index)

        self._check(
            gen.set_trigger_channel(channel=channel, ttype=ttype),
            operation="set_trigger_channel",
        )

        if channel == 0:
            self._logger.debug("Generator %d is deaf to any trigger!", gen_index)
        else:
            self._logger.debug("Generator %d listens to %s_trigger_word channel %d", gen_index, ttype, channel)

        return {
            "gen_index": gen_index,
            "ttype": ttype,
            "channel": channel,
        }


__all__ = ["GeneratorOps"]
