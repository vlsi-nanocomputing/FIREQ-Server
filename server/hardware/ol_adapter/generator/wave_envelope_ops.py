"""Wave and envelope management for generator operations.

This module handles:
- Wave definition and compilation
- Envelope upload and processing
- Readout wave configuration
- Wave memory reset and synchronization
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from ....models.exceptions import ConfigurationError
from ..adapter_types import WaveEntry
from ..cache import get_wave_cache as _get_wave_cache_util
from .wave_utils import (
    build_wave_entry,
    check_readout_wave_cache,
    check_wave_replacement_policy,
)

if TYPE_CHECKING:
    from ..cache import AdapterContext


class WaveEnvelopeOps:
    """Wave and envelope management operations."""

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize WaveEnvelopeOps.

        :param ctx: Shared adapter context with all dependencies.
        """
        self._ctx = ctx

    def _lookup_wave_in_wave_memory(self, gen_index: int, wdw_int: int) -> str:
        """Resolve a compiled Wave Definition Word (WDW) back to its unique wave_id.

        This method enforces strict consistency between the High-Level (HL) cache and
        the Low-Level (LL) generator memory.

        :param gen_index: Index of the target generator.
        :param wdw_int: The integer representation of the Wave Definition Word.
        :return: The unique wave_id associated with the WDW.
        :raises ConfigurationError: If the WDW is not found, ambiguous, or inconsistent.
        """
        gen = self._ctx.ll.get_gen(gen_index)
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
        return _get_wave_cache_util(self._ctx.cache, gen_index)

    def get_envelope_names(self, gen_index: int) -> list[str]:
        """Retrieve the list of envelope names currently stored in the generator's memory.

        :param gen_index: Index of the target generator.
        :return: A list of envelope names available in the hardware driver.
        """
        gen = self._ctx.ll.get_gen(gen_index)
        return list(getattr(gen, "envelope_memory_dict", {}).keys())

    def _compile_wdw(self, gen: object, entry: WaveEntry) -> int:
        """Compile WaveEntry to Wave Definition Word (WDW).

        :param gen: Generator device object.
        :param entry: WaveEntry to compile.
        :return: Integer WDW value.
        """
        if entry.kind == "env":
            return int(
                self._ctx.ll.call(
                    gen.create_wave_definition_word(
                        entry.envelope,
                        entry.duration,
                        entry.gain,
                        entry.switch_iq,
                        entry.keep_last,
                    ),
                    operation="create_wave_definition_word",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )
            )
        else:
            return int(
                self._ctx.ll.call(
                    gen.create_vz_gate_definition_word(entry.vz_phase_rad),
                    operation="create_vz_gate_definition_word",
                    driver_name="GeneratorDriver",
                    config_error=True,
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
            self._ctx.ll.call(
                gen.replace_wave_in_wave_memory(wdw, wave_id, wave_id),
                operation="replace_wave_in_wave_memory",
                driver_name="GeneratorDriver",
                config_error=True,
            )
        else:
            self._ctx.ll.call(
                gen.add_wave_in_wave_memory(wdw, wave_id),
                operation="add_wave_in_wave_memory",
                driver_name="GeneratorDriver",
                config_error=True,
            )

    def _process_envelope_samples(
        self,
        samples_iq: np.ndarray,
        for_interp: bool,
        gen: object,
        auto_pad: bool,
    ) -> np.ndarray:
        """Convert float I/Q samples to cint16 and apply auto-padding if needed.

        :param samples_iq: Input I/Q samples (numpy array).
        :param for_interp: Whether this envelope is for interpolation.
        :param gen: Generator device object.
        :param auto_pad: If True, auto-pad non-interpolated envelopes.
        :return: Processed envelope data.
        """
        from . import wave_utils as gu  # noqa: PLC0415

        env, original_size = gu.process_envelope_samples(
            samples_iq, for_interp, int(gen.sample_size), int(gen.number_of_channels), auto_pad
        )

        if auto_pad and not for_interp and int(env.size) > original_size:
            self._ctx.logger.debug(
                "upload_envelopes: padded from %d to %d (par=%d)",
                original_size,
                int(env.size),
                int(gen.number_of_channels),
            )

        return env

    def upload_envelopes(
        self,
        *,
        gen_index: int,
        envelopes: list,
        auto_pad_noninterp: bool = True,
    ) -> dict:
        """Upload multiple envelopes into generator envelope memory.

        :param gen_index: Index of the target generator.
        :param envelopes: List of envelope specifications to upload.
        :param auto_pad_noninterp: If True, automatically zero-pads non-interpolated envelopes.
        :return: A summary dictionary containing lists of loaded, skipped, and failed names.
        """
        from . import wave_utils as gu  # noqa: PLC0415

        self._ctx.logger.debug(
            "upload_envelopes: gen=%d, n=%d, auto_pad_noninterp=%s",
            gen_index,
            len(envelopes),
            auto_pad_noninterp,
        )

        gen = self._ctx.ll.get_gen(gen_index)
        loaded: list[str] = []
        skipped: list[str] = []
        failed: list[dict] = []

        env_cache = getattr(gen, "EnvelopeMemoryDict", {})

        for e in envelopes:
            name = str(e.get("name", ""))
            try:
                gu.validate_envelope_spec(name)

                if name in env_cache:
                    self._ctx.logger.debug(
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

                env = self._process_envelope_samples(samples_iq, for_interp, gen, auto_pad_noninterp)
                i_even, q_even = gu.validate_envelope_symmetry(is_sym, i_even, q_even, for_interp)

                self._ctx.ll.call(
                    gen.add_envelope_to_envelope_memory(env, for_interp, is_sym, i_even, q_even, name),
                    operation="add_envelope_to_envelope_memory",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )
                loaded.append(name)

            except Exception as ex:
                self._ctx.logger.exception("upload_envelopes: failed '%s'", name)
                failed.append({"name": name, "error": str(ex)})

        self._ctx.logger.debug(
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
        self._ctx.logger.debug("compile_waves: gen=%d n=%d", gen_index, len(waves))

        gen = self._ctx.ll.get_gen(gen_index)
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
                    self._ctx.logger.debug(
                        "compile_waves: wave_id '%s' already present (same spec) -> skipped",
                        wave_id,
                    )
                    continue

                wdw = self._compile_wdw(gen, new_entry)
                new_entry.wdw = wdw
                self._store_wdw_in_hardware(gen, wdw, wave_id, action == "replace")

                if action == "replace":
                    replaced.append(wave_id)

                cache[wave_id] = new_entry
                out.append({"wave_id": wave_id, "WDW": hex(wdw)})

            except Exception as ex:
                self._ctx.logger.exception("compile_waves: failed wave=%s", w)
                failed.append({"wave_id": w.get("wave_id"), "error": str(ex)})

        self._ctx.logger.debug(
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
        self._ctx.logger.debug("upload_readout_wave: gen=%d replace=%s", gen_index, replace)

        gen = self._ctx.ll.get_gen(gen_index)

        new_entry = WaveEntry(
            envelope=str(wave["envelope"]),
            duration=int(wave["duration"]),
            gain=float(wave["gain"]),
            switch_iq=bool(wave.get("switch_iq", False)),
            keep_last=bool(wave.get("keep_last", False)),
            wdw=None,
        )

        old_entry, action = check_readout_wave_cache(gen_index, new_entry, self._ctx.cache.readout_wave_store, replace)

        if action == "skip":
            new_entry.wdw = old_entry.wdw
            self._ctx.cache.readout_wave_store[gen_index] = new_entry
            self._ctx.logger.debug(
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

        self._ctx.ll.call(
            gen.write_readout_wave(wdw),
            operation="write_readout_wave",
            driver_name="GeneratorDriver",
            config_error=True,
        )

        self._ctx.cache.readout_wave_store[gen_index] = new_entry
        status = "replaced" if action == "replace" else "compiled"
        self._ctx.logger.debug("upload_readout_wave: %s gen=%d WDW=0x%X", status, gen_index, wdw)

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
        return self._ctx.cache.readout_wave_store.get(gen_index)

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
        self._ctx.logger.debug(
            "reset_wave_memory: gen=%d preserve_specs=%s clear_last_fifo=%s",
            gen_index,
            preserve_specs,
            clear_last_fifo,
        )

        gen = self._ctx.ll.get_gen(gen_index)

        self._ctx.ll.call(
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
            self._ctx.cache.last_fifo.pop(int(gen_index), None)

        readout_entry = self._ctx.cache.readout_wave_store.get(gen_index)
        if readout_entry is not None:
            if preserve_specs:
                readout_entry.wdw = None
            else:
                self._ctx.cache.readout_wave_store.pop(gen_index, None)

        self._ctx.logger.debug(
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
        self._ctx.logger.debug(
            "reset_envelopes: gen=%d preserve_wave_specs=%s clear_last_fifo=%s",
            gen_index,
            preserve_wave_specs,
            clear_last_fifo,
        )

        gen = self._ctx.ll.get_gen(gen_index)

        self._ctx.ll.call(
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
            self._ctx.cache.last_fifo.pop(int(gen_index), None)

        self._ctx.logger.debug(
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


__all__ = ["WaveEnvelopeOps"]
