# file: fireq-utils/server/hardware/adapter/wave.py
"""Wave management mixin for OverlayAdapter.

This module provides the WaveMixin class that handles:
- Wave cache management
- Envelope upload and management
- Wave compilation (env and vz types)
- Readout wave configuration
- Drive sequence programming
- Memory reset operations
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import numpy as np

from ...models.adapter_types import EnvelopeSpec, WaveEntry, same_spec
from ...models.exceptions import ConfigurationError
from ..utils import iq_float_to_cint16

if TYPE_CHECKING:
    import logging


class WaveMixin:
    """Mixin class providing wave management methods.

    This mixin expects the following attributes on self:
    - ol: The low-level overlay driver
    - logger: A logging.Logger instance
    - _call: Method for driver call error handling
    - _get_gen: Method to get generator driver by index
    - _wave_store: Dict[int, Dict[str, WaveEntry]] for wave cache
    - _last_fifo: Dict[int, List[str]] for FIFO tracking
    - _readout_wave_store: Dict[int, WaveEntry] for readout waves
    """

    # Type hints for attributes expected from the main class
    ol: object
    logger: logging.Logger
    _wave_store: dict[int, dict[str, WaveEntry]]
    _last_fifo: dict[int, list[str]]
    _readout_wave_store: dict[int, WaveEntry]

    def _lookup_wave_in_wave_memory(self, gen_index: int, wdw_int: int) -> str:
        """Resolve a compiled Wave Definition Word (WDW) back to its unique wave_id.

        This method enforces strict consistency between the High-Level (HL) cache and
        the Low-Level (LL) generator memory.

        :param gen_index: Index of the target generator.
        :param wdw_int: The integer representation of the Wave Definition Word.
        :return: The unique wave_id associated with the WDW.
        :raises ConfigurationError: If the WDW is not found, ambiguous, or inconsistent.
        """
        gen = self._get_gen(gen_index)
        cache: dict[str, WaveEntry] = self.get_wave_cache(gen_index)

        # find wave_ids whose cached WDW matches
        matches: list[str] = [
            wave_id for wave_id, entry in cache.items() if entry.wdw is not None and (int(entry.wdw) == int(wdw_int))
        ]

        if len(matches) == 0:
            raise ConfigurationError(
                f"WDW {wdw_int} not found for gen_index={gen_index} (no WaveEntry with matching .wdw)"
            )

        if len(matches) > 1:
            raise ConfigurationError(
                f"Ambiguous WDW {wdw_int} for gen_index={gen_index}: multiple wave_id map to same WDW"
            )

        wave_id = matches[0]

        # LL consistency check
        gen = self._get_gen(gen_index)
        if wave_id not in gen.wave_memory_dict:
            raise ConfigurationError(
                f"Inconsistent state: wave_id='{wave_id}' has matching WDW in "
                "WaveEntry but not in driver WaveMemoryDict"
            )

        return wave_id

    def get_wave_cache(self, gen_index: int) -> dict[str, WaveEntry]:
        """Retrieve the High-Level wave cache for a specific generator.

        This method employs lazy initialization: if the cache for the requested
        generator does not exist, an empty dictionary is created, stored, and returned.

        :param gen_index: Index of the target generator.
        :return: A dictionary mapping wave IDs to their corresponding WaveEntry objects.
        """
        cache = self._wave_store.get(gen_index)
        if cache is None:
            cache = {}
            self._wave_store[gen_index] = cache
        return cache

    def get_envelope_names(self, gen_index: int) -> list[str]:
        """Retrieve the list of envelope names currently stored in the generator's memory.

        :param gen_index: Index of the target generator.
        :return: A list of envelope names available in the hardware driver.
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
        :param envelopes: List of envelope specifications to upload.
        :param auto_pad_noninterp: If True, automatically zero-pads non-interpolated
            envelopes to match hardware parallelism.
        :return: A summary dictionary containing lists of loaded, skipped, and failed names.
        """
        self.logger.debug(
            "upload_envelopes: gen=%d, n=%d, auto_pad_noninterp=%s",
            gen_index,
            len(envelopes),
            auto_pad_noninterp,
        )

        gen = self._get_gen(gen_index)
        loaded: list[str] = []
        skipped: list[str] = []
        failed: list[dict] = []

        env_cache = getattr(gen, "EnvelopeMemoryDict", {})

        for e in envelopes:
            name = str(e.get("name", ""))
            try:
                if not name:
                    raise ConfigurationError("Envelope name is empty")

                if name.startswith("_"):
                    raise ConfigurationError("Envelope Name forbidden : '_' is for reserved name")

                if name in env_cache:
                    self.logger.debug(
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

                env = iq_float_to_cint16(samples_iq, int(gen.sample_size))

                # Non-interp: automatic zero-padding
                if auto_pad_noninterp and not for_interp:
                    par = int(gen.number_of_channels)
                    r = int(env.size) % par
                    if r != 0:
                        old = int(env.size)
                        env = np.pad(env, (0, par - r), mode="constant")
                        self.logger.debug(
                            "upload_envelopes: padded '%s' from %d to %d (par=%d)",
                            name,
                            old,
                            int(env.size),
                            par,
                        )

                if not is_sym:
                    i_even = False
                    q_even = False
                if is_sym and not for_interp:
                    raise ConfigurationError(
                        "Invalid envelope: the 'is_sym' flag is only for interpolated "
                        "envelope.\nHint: set for_interp = True"
                    )

                self._call(
                    gen.add_envelope_to_envelope_memory(env, for_interp, is_sym, i_even, q_even, name),
                    operation="add_envelope_to_envelope_memory",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )
                loaded.append(name)

            except Exception as ex:
                self.logger.exception("upload_envelopes: failed '%s'", name)
                failed.append({"name": name, "error": str(ex)})

        self.logger.debug(
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

    def compile_waves(self, *, gen_index: int, waves: list[dict], replace: bool) -> dict:
        """Compile high-level wave definitions into hardware Wave Definition Words (WDW).

        Handles 'env' (Envelope) and 'vz' (Virtual-Z) wave types. Supports caching to
        skip re-compilation of identical specifications.

        :param gen_index: Index of the target generator.
        :param waves: List of dictionaries defining the waves.
        :param replace: If True, allows overwriting existing wave definitions.
        :return: A summary dictionary detailing compiled, replaced, skipped, and failed waves.
        """
        self.logger.debug("compile_waves: gen=%d n=%d", gen_index, len(waves))
        self.logger.debug("compile_waves: waves=%s", waves)

        gen = self._get_gen(gen_index)
        out, replaced, skipped, failed = [], [], [], []
        cache: dict[str, WaveEntry] = self.get_wave_cache(gen_index)

        for w in waves:
            try:
                kind = str(w.get("kind", "env")).lower()
                if kind not in ("env", "vz"):
                    raise ConfigurationError(f"Unknown wave kind '{kind}' (use 'env' or 'vz').")

                wave_id = str(w["wave_id"])

                # Build the new WaveEntry by type
                if kind == "env":
                    new_entry = WaveEntry(
                        envelope=str(w["envelope"]),
                        duration=int(w["duration"]),
                        gain=float(w["gain"]),
                        switch_iq=bool(w.get("switch_iq", False)),
                        keep_last=bool(w.get("keep_last", False)),
                        wdw=None,
                    )
                else:
                    if "vz_phase_rad" not in w:
                        raise ConfigurationError(
                            f"VZ wave '{wave_id}' missing vz_phase_rad. " f"Hint: provide vz_phase_rad (radians)."
                        )
                    phase = float(w["vz_phase_rad"])
                    new_entry = WaveEntry(
                        kind="vz",
                        envelope="",
                        duration=0,
                        gain=0.0,
                        switch_iq=False,
                        keep_last=False,
                        vz_phase_rad=phase,
                        wdw=None,
                    )

                old_entry = cache.get(wave_id)
                in_hw = wave_id in gen.wave_memory_dict

                # SKIP EARLY (same spec, already compiled)
                if old_entry is not None and same_spec(old_entry, new_entry) and in_hw and (old_entry.wdw is not None):
                    skipped.append(wave_id)
                    new_entry.wdw = old_entry.wdw
                    cache[wave_id] = new_entry
                    out.append({"wave_id": wave_id, "WDW": hex(new_entry.wdw)})
                    self.logger.debug(
                        "compile_waves: wave_id '%s' already present (same spec) -> skipped",
                        wave_id,
                    )
                    continue

                # Replacement check
                if old_entry is not None and not same_spec(old_entry, new_entry) and not replace:
                    raise ConfigurationError(
                        f"wave_id '{wave_id}' already exists but spec differs. "
                        f"OLD={old_entry} NEW={new_entry}. "
                        f"Hint: set replace=True or use a different wave_id."
                    )

                # HL-LL desynchronization guard
                if old_entry is None and in_hw and not replace:
                    raise ConfigurationError(
                        f"wave_id '{wave_id}' exists in HW but not in HL cache. "
                        f"Hint: set replace=True to re-sync or rebuild HL cache."
                    )

                # WDW generation by kind
                if new_entry.kind == "env":
                    wdw = self._call(
                        gen.create_wave_definition_word(
                            new_entry.envelope,
                            new_entry.duration,
                            new_entry.gain,
                            new_entry.switch_iq,
                            new_entry.keep_last,
                        ),
                        operation="create_wave_definition_word",
                        driver_name="GeneratorDriver",
                        config_error=True,
                    )
                else:
                    wdw = self._call(
                        gen.create_vz_gate_definition_word(new_entry.vz_phase_rad),
                        operation="create_vz_gate_definition_word",
                        driver_name="GeneratorDriver",
                        config_error=True,
                    )

                wdw = int(wdw)
                new_entry.wdw = wdw

                if in_hw:
                    self._call(
                        gen.replace_wave_in_wave_memory(wdw, wave_id, wave_id),
                        operation="replace_wave_in_wave_memory",
                        driver_name="GeneratorDriver",
                        config_error=True,
                    )
                    replaced.append(wave_id)
                else:
                    self._call(
                        gen.add_wave_in_wave_memory(wdw, wave_id),
                        operation="add_wave_in_wave_memory",
                        driver_name="GeneratorDriver",
                        config_error=True,
                    )

                cache[wave_id] = new_entry
                out.append({"wave_id": wave_id, "WDW": hex(wdw)})

            except Exception as ex:
                self.logger.exception("compile_waves: failed wave=%s", w)
                failed.append({"wave_id": w.get("wave_id"), "error": str(ex)})

        self.logger.debug(
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
        :param wave: Dictionary containing the wave specification.
        :param replace: If True, allows overwriting an existing readout configuration.
        :return: A dictionary summarizing the upload status and compiled WDW.
        """
        self.logger.debug("upload_readout_wave: gen=%d replace=%s", gen_index, replace)
        self.logger.debug("upload_readout_wave: wave=%s", wave)

        gen = self._get_gen(gen_index)

        new_entry = WaveEntry(
            envelope=str(wave["envelope"]),
            duration=int(wave["duration"]),
            gain=float(wave["gain"]),
            switch_iq=bool(wave.get("switch_iq", False)),
            keep_last=bool(wave.get("keep_last", False)),
            wdw=None,
        )

        old_entry = self._readout_wave_store.get(gen_index)

        # SKIP EARLY (same spec, already compiled)
        if old_entry is not None and same_spec(old_entry, new_entry) and (old_entry.wdw is not None):
            new_entry.wdw = old_entry.wdw
            self._readout_wave_store[gen_index] = new_entry

            self.logger.debug(
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

        # Replacement check
        if old_entry is not None and not same_spec(old_entry, new_entry) and not replace:
            raise ConfigurationError(
                f"Readout wave for gen_index={gen_index} already exists but spec differs. "
                f"OLD={old_entry} NEW={new_entry}. "
                f"Hint: set replace=True to overwrite."
            )

        # Compile WDW
        wdw = self._call(
            gen.create_wave_definition_word(
                new_entry.envelope,
                new_entry.duration,
                new_entry.gain,
                new_entry.switch_iq,
                new_entry.keep_last,
            ),
            operation="create_wave_definition_word",
            driver_name="GeneratorDriver",
            config_error=True,
        )
        wdw = int(wdw)
        new_entry.wdw = wdw

        # Write to HW
        self._call(
            gen.write_readout_wave(wdw),
            operation="write_readout_wave",
            driver_name="GeneratorDriver",
            config_error=True,
        )

        was_replaced = old_entry is not None
        self._readout_wave_store[gen_index] = new_entry

        status = "replaced" if was_replaced else "compiled"
        self.logger.debug("upload_readout_wave: %s gen=%d WDW=0x%X", status, gen_index, wdw)

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
        :return: The current WaveEntry or None if not configured.
        """
        return self._readout_wave_store.get(gen_index)

    def program_drive_sequence(
        self,
        *,
        gen_index: int,
        wave_id_list: list[str],
        start_index: int = 1,
    ) -> dict:
        """Program the generator FIFO with a sequence of wave_ids.

        :param gen_index: Index of the target generator.
        :param wave_id_list: Ordered list of wave IDs to execute.
        :param start_index: FIFO index to start writing at (default 1).
        :return: A dictionary containing the updated FIFO sequence.
        """
        self.logger.debug("program_drive_sequence: gen=%d n=%d", gen_index, len(wave_id_list))
        self.logger.debug("program_drive_sequence: wave_id_list=%s", wave_id_list)

        gen = self._get_gen(gen_index)
        cache = self.get_wave_cache(gen_index)
        start_index = int(start_index)
        if start_index < 1:
            raise ConfigurationError(f"program_drive_sequence: start_index must be >= 1, got {start_index}")

        # capacity check
        max_entries = int(gen.memory_mapped_fifo_segment_depth // 4)
        end_index = start_index + len(wave_id_list) - 1
        if end_index > max_entries:
            raise ConfigurationError(
                f"program_drive_sequence: overflow: end_index={end_index} > max_entries={max_entries}"
            )

        # Pre-check
        missing_wave_id_HL = [wid for wid in wave_id_list if (wid not in cache) or (cache[wid].wdw) is None]
        missing_wave_id_LL = [wid for wid in wave_id_list if wid not in gen.wave_memory_dict]

        if missing_wave_id_HL:
            raise ConfigurationError(f"program_drive_sequence: wave_id not in HL cache: {missing_wave_id_HL}")
        if missing_wave_id_LL:
            raise ConfigurationError(f"program_drive_sequence: wave_id was never compiled (LL): {missing_wave_id_LL}")

        # set the driver source as FIFO
        self.set_drive_source(gen_index=gen_index, source="fifo")
        self.logger.debug("program_drive_sequence: set_drive_source(gen=%d, source='fifo')", gen_index)

        # Program FIFO
        for i, wave_id in enumerate(wave_id_list, start=start_index):
            wave_addr = gen.wave_memory_dict.get(wave_id, "UNKNOWN")
            self.logger.debug(
                "program_drive_sequence: FIFO[%d] = wave_id='%s' addr=%s",
                i,
                wave_id,
                wave_addr,
            )
            self._call(
                gen.add_wave_to_drive_wave_sequence(i, wave_id),
                operation="add_wave_to_drive_wave_sequence",
                driver_name="GeneratorDriver",
                config_error=True,
            )

        # Update last FIFO cache
        prev = self._last_fifo.get(int(gen_index), [])
        if start_index == 1:
            new_fifo = list(wave_id_list)
        else:
            if len(prev) < (start_index - 1):
                raise ConfigurationError(
                    f"program_drive_sequence: cannot patch from start_index={start_index} "
                    f"because _last_fifo has only {len(prev)} entries. "
                    f"Program from 1 first, then patch."
                )
            suffix = prev[end_index:] if len(prev) >= end_index else []
            new_fifo = prev[: start_index - 1] + list(wave_id_list) + suffix

        self._last_fifo[int(gen_index)] = new_fifo

        self.logger.debug(
            "program_drive_sequence: done gen=%d fifo_len=%d",
            gen_index,
            len(wave_id_list),
        )
        self.logger.debug(
            "program_drive_sequence: final _last_fifo[%d] = %s",
            gen_index,
            new_fifo,
        )
        return {"gen_index": int(gen_index), "fifo": self._last_fifo[int(gen_index)]}

    def reset_wave_memory(
        self,
        *,
        gen_index: int,
        preserve_specs: bool = True,
        clear_last_fifo: bool = True,
    ) -> dict:
        """Reset the generator wave memory and synchronize the High-Level cache.

        :param gen_index: Index of the target generator.
        :param preserve_specs: If True, keeps WaveEntry objects but invalidates WDWs.
        :param clear_last_fifo: If True, clears the record of the last programmed FIFO.
        :return: A summary of the cache state after reset.
        """
        self.logger.debug(
            "reset_wave_memory: gen=%d preserve_specs=%s clear_last_fifo=%s",
            gen_index,
            preserve_specs,
            clear_last_fifo,
        )

        gen = self._get_gen(gen_index)

        self._call(
            gen.reset_wave_memory_dict(),
            operation="reset_wave_memory_dict",
            driver_name="GeneratorDriver",
            config_error=True,
        )

        cache = self.get_wave_cache(gen_index)
        n_before = len(cache)

        if preserve_specs:
            for entry in cache.values():
                entry.wdw = None
            hl_action = "invalidated_wdw"
        else:
            cache.clear()
            hl_action = "cleared_cache"

        if clear_last_fifo:
            self._last_fifo.pop(int(gen_index), None)

        readout_entry = self._readout_wave_store.get(gen_index)
        if readout_entry is not None:
            if preserve_specs:
                readout_entry.wdw = None
            else:
                self._readout_wave_store.pop(gen_index, None)

        self.logger.debug(
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

    def reset_envelopes(
        self,
        *,
        gen_index: int,
        preserve_wave_specs: bool = True,
        clear_last_fifo: bool = True,
    ) -> dict:
        """Reset the generator envelope memory and synchronize the High-Level wave cache.

        :param gen_index: Index of the target generator.
        :param preserve_wave_specs: If True, retains WaveEntry specs but invalidates WDWs.
        :param clear_last_fifo: If True, clears the record of the last programmed sequence.
        :return: A summary of the actions taken on the cache.
        """
        self.logger.debug(
            "reset_envelopes: gen=%d preserve_wave_specs=%s clear_last_fifo=%s",
            gen_index,
            preserve_wave_specs,
            clear_last_fifo,
        )

        gen = self._get_gen(gen_index)

        self._call(
            gen.reset_envelope_dict(),
            operation="reset_envelope_dict",
            driver_name="GeneratorDriver",
            config_error=True,
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

        self.logger.debug(
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
        :param source: Selection between "fifo" (programmed sequence) or "lfsr".
        :param seed: Optional LFSR seed value. Used only when source="lfsr".
        :return: Selected source status (and seed, if applied).
        """
        self.logger.debug("set_drive_source: gen=%d source=%s seed=%s", gen_index, source, seed)

        gen = self._get_gen(gen_index)

        source_lower = str(source).lower()
        if source_lower == "fifo":
            source_val = 0

        elif source_lower == "lfsr":
            source_val = 1

            if seed is not None:
                self._call(
                    gen.set_lfsr_seed(int(seed)),
                    operation="set_lfsr_seed",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )
        else:
            raise ConfigurationError(f"set_drive_source: invalid source='{source}'. Use 'fifo' or 'lfsr'.")

        self._call(
            gen.set_drive_order_source(source_val),
            operation="set_drive_order_source",
            driver_name="GeneratorDriver",
            config_error=True,
        )

        self.logger.debug("set_drive_source: done gen=%d source=%s", gen_index, source_lower)

        out = {"gen_index": int(gen_index), "source": source_lower}
        if source_lower == "lfsr" and seed is not None:
            out["seed"] = int(seed)
        return out


__all__ = ["WaveMixin"]
