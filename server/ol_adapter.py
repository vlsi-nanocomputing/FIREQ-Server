# file: fireq_orchestrator/hardware/driver_wrappers.py
"""
Adapter layer for FIREQ_LL_API drivers.

This module implements the Adapter pattern to provide a safe and simplified interface
to the low-level drivers. They are specifically designed for the FIREQ_Server interaction.
 It handles:
- Error code translation (from integer codes to Python exceptions).
- Type enforcement and safety checks.
- Hardware constraint validation.
- Automatic RF-DC Hardware Configuration (Mix-Mode/Nyquist Zone switching).
"""

# ======================================================================
# GENERATOR ADAPTER (server-oriented)
# ======================================================================

import logging
from typing import Any, Dict, List, Optional, Literal
import numpy as np
from dataclasses import dataclass
from FIREQ_LL_API.overlay_driver import FIREQ_SoC
from .dma_engine import AcquisitionEngine
from .exceptions import *
from typing import Any, Callable, Optional, Dict, TypedDict
import time

class modulation(TypedDict):
    frequency_mhz : float
    phase : Optional[float]

class trigger_command(TypedDict):
    ttype: str
    channel: int

class EnvelopeSpec(TypedDict):
    name: str
    for_interpolation: bool
    is_symmetric: bool
    i_even: bool
    q_even: bool
    samples_iq: List[List[float]]

@dataclass
class WaveEntry:
    envelope: str
    duration: int
    gain: float
    switch_iq: bool = False
    keep_last: bool = False
    wdw: Optional[int] = None
        
def handle_error_result( result: Any,
    *, # next methods MUST be specified when the function is used
    operation: str,
    driver_name: str,
    logger: logging.Logger,
    config_error: bool = False,
    hint: Optional[str] = None,
    error_hints: Optional[Dict[tuple, str]] = None,
    error_exc: Optional[Dict[tuple, type]] = None,
) -> Any:
    """
    Handle FIREQ low-level driver return codes and raise appropriate exceptions.

    :param result: Return value from a driver method. Negative integers indicate errors.
    :type result: Any
    :param operation: Name of the operation/method for error reporting.
    :type operation: str
    :param driver_name: Name of the driver class.
    :type driver_name: str
    :param logger: Logger instance for telemetry.
    :type logger: logging.Logger
    :param config_error: If True, raises ConfigurationError instead of DriverError.
    :type config_error: bool
    :param hint: Explicit hint message to override automatic hints.
    :type hint: Optional[str]
    :param error_hints: Mapping of (driver, op, code) to specific hint strings.
    :type error_hints: Optional[Dict[tuple, str]]
    :param error_exc: Mapping of (driver, op, code) to specific Exception classes.
    :type error_exc: Optional[Dict[tuple, type]]
    :return: The original result if non-negative.
    :rtype: Any
    :raises ConfigurationError: If result < 0 and config_error is True.
    :raises DriverError: If result < 0 and config_error is False.
    """
    # Success path: anything non-negative or non-int just passes through
    if not (isinstance(result, int) and result < 0):
        return result

    code = int(result)
    key = (driver_name, operation, code)

    # Resolve hint priority: explicit hint > mapping hint > generic
    mapping_hint = None
    if error_hints is not None:
        mapping_hint = error_hints.get(key)

    message = hint or mapping_hint or f"{driver_name}.{operation} failed with code {code}"

    logger.error(message)

    # Optional: raise a specific exception class for known cases
    if error_exc is not None:
        exc_cls = error_exc.get(key)
        if exc_cls is not None:
            raise exc_cls(message)

    # Default exceptions
    if config_error:
        raise ConfigurationError(message)

    raise DriverError(
        message,
        driver_name=driver_name,
        operation=operation,
        return_code=code,
    )

def _same_spec(a: WaveEntry, b: WaveEntry) -> bool:
        return (
            a.envelope == b.envelope
            and a.duration == b.duration
            and a.switch_iq == b.switch_iq
            and a.keep_last == b.keep_last
            and a.gain == b.gain
        )

class OL_adapter:
    """
    One-stop adapter: wraps FIREQ_SoC directly.

    Design goals:
    - Use the overlay driver in the most simple way: pass instructions to IPs via __getattar__.
    - Provide macro commands based on the server operational principles:
        * upload_envelopes()
        * compile_waves()
        * run_experiment()
    - Add server-grade safety checks + error handling.
    
    Notes:
    - Wave IDs are treated as 128-bit Wave Definition Words (WDW) serialized in hex.
    - To program FIFO sequences, we ensure each WDW is present in wave memory (wave_id-based).
    - wave_id: unique code from the user to identify the wanted wdw 
    """

    # Optional: user-friendly hints for common negative codes
    _ERROR_HINTS: Dict[tuple, str] = {
        ("GeneratorDriver", "add_envelope_to_envelope_memory", -3):
            "Check: samples must be complex (I+jQ), size>=2, non-interp size multiple of NumberOfChannels, name not already used.",
        ("GeneratorDriver", "create_wave_definition_word", -3):
            "Check: envelope name exists, gain in [-1,1], duration=0 allowed for natural size (esp. non-interp).",
        ("GeneratorDriver", "add_wave_in_wave_memory", -3):
            "Wave memory full or name already used. Consider reset_wave_memory_dict() if safe.",
        ("GeneratorDriver", "add_wave_to_drive_wave_sequence", -3):
            "Check: FIFO index valid, wave_name exists in WaveMemoryDict.",
        ("GeneratorDriver", "write_readout_wave", -3):
            "Check: wave_definition must be non-negative 128-bit integer.",
        ("AcquistionDriver", "set_acquistion_dds_parameters", -3):
            "Check: frequency>=0, duration in [1..MaximumDuration], adc_samplerate correct.",
        ("TriggerGeneratorDriver", "insert_drive_delay", -3):
            "Check: channel range, index range, delay range, generate_trigger is 0/1.",
        ("TriggerGeneratorDriver", "set_readout_delay", -3):
            "Check: readout channel range, delay non-negative and within HW limits.",
    }

    def __init__(self, ol: FIREQ_SoC, *, logger: Optional[logging.Logger] = None):
        if not ol.is_healthy:
            raise HardwareStateError("Unexpected Error: overlay upload failed!")

        self.ol = ol
        self.logger = logger or logging.getLogger(__name__)
        self._summary = ol.summary()  

        # DMA engine (needed for run_experiment)
        if self.ol.dma is None or self.ol.axis_switch is None:
            raise HardwareStateError("DMA or AXI-Stream switch missing in overlay")

        par = self.ol.hw_specs["summary"].get("adc_parallelism")
        if par is None:
            # fallback if not uniform across IPs (future developements)
            par_set = self.ol.hw_specs["summary"].get("adc_parallelism_set") or []
            par = (par_set[0] if isinstance(par_set, (list, tuple)) and len(par_set) else 8)

        self.dma_engine = AcquisitionEngine(
            self.ol.dma,
            self.ol.axis_switch,
            logger=self.logger,
            hw_specs={"adc_parallelism": int(par) },
            acq_drivers=self.ol.acquisitions 
        )

        # per-generator caches
        # Create a memory of the compiled WDW. Each wdw is accessible via the wave_id as key
        self._wave_store: Dict[int, Dict[str, WaveEntry]] = {}   # gen_index -> waves = { wave_id:str , WaveEntry]}
        # Create a memory of the last used experiment
        self._last_fifo: Dict[int, List[str]] = {}             # gen_index -> last programmed FIFO  : [wdw0, wdw1, ...] 
        # Create a memory for readout waves (one per generator)
        self._readout_wave_store: Dict[int, WaveEntry] = {}    # gen_index -> current readout WaveEntry 
    # ------------------------------------------------------------
    # Pass-through: everything not defined here goes to self.ol
    # ------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        return getattr(self.ol, name)   
    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------
    def _call(self, result: Any, 
                    *, 
                    operation: str,
                    driver_name: str,
                    config_error: bool = False,
                    hint: Optional[str] = None,) -> Any:
        
        """Uniform driver error handling (returns result if OK)."""
        return handle_error_result(
            result,
            operation=operation,
            driver_name=driver_name,
            logger=self.logger,
            config_error=config_error,
            hint=hint,
            error_hints=self._ERROR_HINTS,
            error_exc=None,
        )

    def _get_gen(self, gen_index: int):
        try:
            return self.ol.generators[int(gen_index)]
        except Exception as e:
            raise ConfigurationError(f"Invalid gen_index={gen_index}") from e

    def _get_acq(self, acq_index: int):
        try:
            return self.ol.acquisitions[int(acq_index)]
        except Exception as e:
            raise ConfigurationError(f"Invalid acq_index={acq_index}") from e

    def _get_trig(self):
        t = self.ol.trigger
        if t is None:
            raise HardwareStateError("No TriggerGenerator IP found")
        return t

    def _dac_sr_mhz(self) -> float:
        return float(self.ol.hw_specs["summary"]["dac_sr_hz"])/ 1e6

    def _adc_sr_mhz(self) -> float:
        return float(self.ol.hw_specs["summary"]["adc_sr_hz"]) / 1e6
    
    def _lookup_wave_in_wave_memory(self, gen_index: int, wdw_int: int) -> str:
        """
        Check if the WDW is associated to wave_id:
        - If exactly one wave_id has entry.wdw == wdw_int AND it exists in gen.WaveMemoryDict -> return wave_id
        - If none -> error
        - If more than one -> error
        """
        gen = self._get_gen(gen_index)          
        cache: Dict[str, WaveEntry] = self._get_wave_cache(gen_index)

        # find wave_ids whose cached WDW matches (skip entries not compiled yet: wdw=None)
        matches: List[str] = [
            wave_id
            for wave_id, entry in cache.items()
            if (entry.wdw is not None) and (int(entry.wdw) == int(wdw_int))
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
        if wave_id not in gen.WaveMemoryDict:
            raise ConfigurationError(
                f"Inconsistent state: wave_id='{wave_id}' has matching WDW in WaveEntry but not in driver WaveMemoryDict"
            )

        return wave_id

    def _iq_float_to_cint16(self, samples_iq: List[List[float]], sample_bits: int) -> np.ndarray:
        # prepare samples to envelope memory upload
        vmax = (1 << (sample_bits - 1)) - 1
        a = np.asarray(samples_iq, dtype=np.float64)
        i = np.clip(np.rint(a[:, 0] * vmax), -vmax - 1, vmax).astype(np.int16)
        q = np.clip(np.rint(a[:, 1] * vmax), -vmax - 1, vmax).astype(np.int16)
        return i + 1j * q

    
    # ------------------ GENERATOR IP MACROS ---------------------
    # ------------------------------------------------------------
    # Macro command G0: helpers for cache
    # ------------------------------------------------------------
    
    def get_wave_cache(self, gen_index: int) -> Dict[str, WaveEntry]:
        cache = self._wave_store.get(gen_index)
        if cache is None:
            cache = {}
            self._wave_store[gen_index] = cache
        return cache
    
    def get_envelope_names(self, gen_index: int) -> List[str]:
        """Expose LL envelope names (read-only inspection)."""
        gen = self._get_gen(gen_index)
        return list(getattr(gen, "EnvelopeMemoryDict", {}).keys())
    
    # ------------------------------------------------------------
    # Macro command G1: upload_envelopes
    # ------------------------------------------------------------
    def upload_envelopes(
        self,
        *,
        gen_index: int,
        envelopes: List[EnvelopeSpec],
        auto_pad_noninterp: bool = True,
    ) -> dict:
        """
        Upload multiple envelopes into the generator envelope memory.

        Parameters
        ----------
        gen_index : int
        envelopes : list[envelope_payload]
        auto_pad_noninterp : bool
            If True, zero-pad non-interpolated envelopes to match parallelism.
        """

        self.logger.info("upload_envelopes: gen=%d, n=%d, auto_pad_noninterp=%s",
                 gen_index, len(envelopes), auto_pad_noninterp)
                 
       
        gen = self._get_gen(gen_index)
        loaded: List[str] = []
        skipped: list[str] = []
        failed: List[dict] = []

        env_cache = getattr(gen, "EnvelopeMemoryDict", {})
        
        for e in envelopes:
            name = str(e.get("name", ""))
            try:
                if not name:
                    raise ConfigurationError("Envelope name is empty")
                
                if name.startswith("_"):
                    raise ConfigurationError("Envelope Name forbidden : '_' is for reserved name")

                if name in env_cache:
                    self.logger.debug("upload_envelopes: skip '%s' (already in EnvelopeMemoryDict)", name)
                    skipped.append(name)
                    continue

                for_interp = bool(e["for_interpolation"])
                is_sym = bool(e["is_symmetric"])
                i_even = bool(e["i_even"])
                q_even = bool(e["q_even"])
                samples_iq = e["samples_iq"]

                env = self._iq_float_to_cint16(samples_iq, int(gen.SampleSize))
                # Non-interp: automatic zero-padding on demand to allow non-interpolated 
                # envelopes upload.
                if auto_pad_noninterp and (not for_interp):
                    par = int(gen.NumberOfChannels)
                    r = int(env.size) % par
                    if r != 0:
                        old = int(env.size)
                        env = np.pad(env, (0, par - r), mode="constant")
                        self.logger.debug("upload_envelopes: padded '%s' from %d to %d (par=%d)",
                                        name, old, int(env.size), par)

                if not is_sym:
                    # i_even and q_even flags are irrelevant: forced to false
                    i_even = False
                    q_even = False 
                if is_sym and not for_interp:
                    # wrong configuration: abort before reaching LL drivers
                    raise ConfigurationError("Invalid envelope: the 'is_sym' flag is only for interpolated envelope.\nHint: set for_interp = True")
                
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

        self.logger.info("upload_envelopes: done gen=%d loaded=%d skipped=%d failed=%d",
                 gen_index, len(loaded), len(skipped), len(failed))
        return {"gen_index": int(gen_index), "loaded": loaded, "skipped" : skipped, "failed": failed}

    # ------------------------------------------------------------
    # Macro command G2: compile_waves
    # ------------------------------------------------------------
    def compile_waves(self, *, gen_index: int, waves: List[dict], replace : bool) -> dict:
        self.logger.info("compile_waves: gen=%d n=%d", gen_index, len(waves))
        self.logger.debug("compile_waves: waves=%s", waves)

        gen = self._get_gen(gen_index)
        out, replaced, skipped, failed = [], [], [], []
        cache: Dict[str, WaveEntry] = self.get_wave_cache(gen_index)

        for w in waves:
            try:
                wave_id = str(w["wave_id"])
                new_entry = WaveEntry(
                    envelope= str(w["envelope"]),
                    duration=int(w["duration"]),
                    gain=float(w["gain"]),
                    switch_iq=bool(w.get("switch_iq", False)),
                    keep_last=bool(w.get("keep_last", False)),
                    wdw=None,
                )
                
                # Elder definition with the same wave_id is exctracted, if any
                # Wave_id must be unique: therefore, new_entry with same wave_id can be either
                # a. skipped, if new and old have the same spec -> optimized compilation
                # b. discarded + error raise, if new and old have the same spec but replace = False [unauthorized replacement]
                # c. substituded, if new and old have not the same spec AND replace = True
                                

                old_entry = cache.get(wave_id)
                in_hw = (wave_id in gen.WaveMemoryDict)

                # ---  SKIP EARLY (no WDW computation)
                if old_entry is not None and _same_spec(old_entry, new_entry) and in_hw and (old_entry.wdw is not None):
                    
                    # 1. old_entry is not None : ensure this is not the first run/ run after a hard reset (reset_envelopes / reset_wave_memory with preserve_specs = False)
                    # 2. _same_spec(old_entry, new_entry) : waves are the same functionally
                    # 3. in_hw : old_entry == new_entry was actually compiled
                    # 4. old_entry.wdw is not None : ensure the wdw is not deprecated. For example, after a reset_wave_memory with preserve_specs = True. 
                    #    In that case, you preserve waves characteristics but you need to recompile the whole cache
                    
                    skipped.append(wave_id)
                    new_entry.wdw = old_entry.wdw
                    cache[wave_id] = new_entry 
                    out.append({"wave_id": wave_id, "WDW": hex(new_entry.wdw)})
                    self.logger.debug("compile_waves: wave_id '%s' already present (same spec) -> skipped", wave_id)
                    continue

                # --- Replacement
                if old_entry is not None and not _same_spec(old_entry, new_entry) and not replace:                  
                    #stop the execution : replacement not allowed by the user
                    raise ConfigurationError(
                        f"wave_id '{wave_id}' already exists but spec differs. "
                        f"OLD={old_entry} NEW={new_entry}. "
                        f"Hint: set replace=True or use a different wave_id."
                    )
                
                # --- HL and LL synch 
                # if any error lead to a wave_id already in HW but not in cache (high-level)
                # if replace = False -> Rise and error : incoherent state. Else, automatic synchronization
                if old_entry is None and in_hw and not replace :
                    raise ConfigurationError(
                        f"wave_id '{wave_id}' exists in HW but not in HL cache. "
                        f"Hint: set replace=True to re-sync or rebuild HL cache."
                    )

                # --- WDW writing
                wdw = self._call(
                    gen.create_wave_definition_word(
                        new_entry.envelope, 
                        new_entry.duration, 
                        new_entry.gain, 
                        new_entry.switch_iq, 
                        new_entry.keep_last
                        ),
                    operation="create_wave_definition_word",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )
                wdw = int(wdw)
                new_entry.wdw = wdw

                if in_hw:

                    #replace: either spec changed or synch HL-LL cache
                    self._call(
                        gen.replace_wave_in_wave_memory(wdw, wave_id, wave_id),
                        operation = "replace_wave_in_wave_memory",
                        driver_name = "GeneratorDriver",
                        config_error = True,
                    )
                    replaced.append(wave_id)
                
                
                else :
                    # completely new entry
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

        self.logger.info(
            "compile_waves: done gen=%d compiled=%d replaced=%d skipped=%d failed=%d",
            gen_index, len(out), len(replaced), len(skipped), len(failed)
        )
        return {
            "gen_index": int(gen_index),
            "waves": out,
            "replaced": replaced,
            "skipped": skipped,
            "failed": failed,
        }
    # ------------------------------------------------------------
    # Macro command G8: Upload Readout Wave
    # ------------------------------------------------------------
    def upload_readout_wave(self, *, gen_index: int, wave: dict, replace: bool = False) -> dict:

        self.logger.info(
            "upload_readout_wave: gen=%d replace=%s",
            gen_index, replace
        )
        self.logger.debug("upload_readout_wave: wave=%s", wave)

        gen = self._get_gen(gen_index)
        
        # Build WaveEntry from dict
        new_entry = WaveEntry(
            envelope=str(wave["envelope"]),
            duration=int(wave["duration"]),
            gain=float(wave["gain"]),
            switch_iq=bool(wave.get("switch_iq", False)),
            keep_last=bool(wave.get("keep_last", False)),
            wdw=None,
        )
        
        # Check existing readout wave in cache
        old_entry = self._readout_wave_store.get(gen_index)
        
        # --- SKIP EARLY (same spec, already compiled)
        if old_entry is not None and _same_spec(old_entry, new_entry) and (old_entry.wdw is not None):
            # Same spec and already written to HW -> skip recompilation
            new_entry.wdw = old_entry.wdw
            self._readout_wave_store[gen_index] = new_entry
            
            self.logger.info(
                "upload_readout_wave: skipped gen=%d (same spec, WDW=0x%X)",
                gen_index, new_entry.wdw
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
        
        # --- REPLACEMENT CHECK
        if old_entry is not None and not _same_spec(old_entry, new_entry) and not replace:
            raise ConfigurationError(
                f"Readout wave for gen_index={gen_index} already exists but spec differs. "
                f"OLD={old_entry} NEW={new_entry}. "
                f"Hint: set replace=True to overwrite."
            )
        
        # --- COMPILE WDW
        wdw = self._call(
            gen.create_wave_definition_word(
                new_entry.envelope,
                new_entry.duration,
                new_entry.gain,
                new_entry.switch_iq,
                new_entry.keep_last
            ),
            operation="create_wave_definition_word",
            driver_name="GeneratorDriver",
            config_error=True,
        )
        wdw = int(wdw)
        new_entry.wdw = wdw
        
        # --- WRITE TO HW (readout registers)
        self._call(
            gen.write_readout_wave(wdw),
            operation="write_readout_wave",
            driver_name="GeneratorDriver",
            config_error=True,
        )
        
        # Update cache
        was_replaced = (old_entry is not None)
        self._readout_wave_store[gen_index] = new_entry
        
        status = "replaced" if was_replaced else "compiled"
        self.logger.info(
            "upload_readout_wave: %s gen=%d WDW=0x%X",
            status, gen_index, wdw
        )
        
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

    def get_readout_wave_cache(self, gen_index: int) -> Optional[WaveEntry]:
        """
        Return the WaveEntry currently configured for readout, if any.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: The current WaveEntry or None if not configured.
        :rtype: Optional[WaveEntry]
        """
        return self._readout_wave_store.get(gen_index)
    # ------------------------------------------------------------
    # Macro command G3: program FIFO drive sequence from wave_ids
    # ------------------------------------------------------------
    def program_drive_sequence(
        self,
        *,
        gen_index: int,
        wave_id_list: List[str],
        start_index: int = 1,
    ) -> dict:
        """
        Programs the generator FIFO with a sequence of waves identified by their wave_id.
        """
        self.logger.info("program_drive_sequence: gen=%d n=%d", gen_index, len(wave_id_list))
        self.logger.debug("program_drive_sequence: wave_id_list=%s", wave_id_list)

        
        gen = self._get_gen(gen_index)
        cache = self.get_wave_cache(gen_index)
        start_index = int(start_index)
        if start_index < 1:
            raise ConfigurationError(f"program_drive_sequence: start_index must be >= 1, got {start_index}")

        # capacity check (avoid cryptic -3 later)
        max_entries = int(gen.MemoryMappedFifoSegmentDepth // 4)
        end_index = start_index + len(wave_id_list) - 1
        if end_index > max_entries:
            raise ConfigurationError(
                f"program_drive_sequence: overflow: end_index={end_index} > max_entries={max_entries}"
            )
        # Pre-check : avoid wrong FIFO listing 
        # Check 1 : High Level Cache [wave_id <--> stored & compiled wdw]
        missing_wave_id_HL = [ wid for wid in wave_id_list if (wid not in cache) or (cache[wid].wdw) is None]
        # Check 2 : Low Level Cache [wave_id <--> Low Level]
        missing_wave_id_LL = [ wid for wid in wave_id_list if wid not in gen.WaveMemoryDict]
        
        if missing_wave_id_HL:
            raise ConfigurationError(f"program_drive_sequence: wave_id not in HL cache: {missing_wave_id_HL}")
        if missing_wave_id_LL:
            raise ConfigurationError(f"program_drive_sequence: wave_id was never compiled (LL): {missing_wave_id_LL}")
        
        # set the driver source as FIFO
        self._call(
            gen.set_drive_order_source(0),  # FIFO
            operation="set_drive_order_source",
            driver_name="GeneratorDriver",
            config_error=True,
        )
        # Program FIFO (index starts at 1 in the LL driver)
        for i, wave_id in enumerate(wave_id_list, start=start_index):
            self._call(
                gen.add_wave_to_drive_wave_sequence(i, wave_id),
                operation="add_wave_to_drive_wave_sequence",
                driver_name="GeneratorDriver",
                config_error=True,
            )
        # Update last FIFO cache in a truthful way
        prev = self._last_fifo.get(int(gen_index), [])
        if start_index == 1:
            new_fifo = list(wave_id_list)
        else:
            # if we don't know the prefix, better fail than lie
            if len(prev) < (start_index - 1):
                raise ConfigurationError(
                    f"program_drive_sequence: cannot patch from start_index={start_index} "
                    f"because _last_fifo has only {len(prev)} entries. "
                    f"Program from 1 first, then patch."
                )
            suffix = prev[end_index:] if len(prev) >= end_index else []
            new_fifo = prev[: start_index - 1] + list(wave_id_list) + suffix

        self._last_fifo[int(gen_index)] = new_fifo

        self.logger.info("program_drive_sequence: done gen=%d fifo_len=%d", gen_index, len(wave_id_list))
        return {"gen_index": int(gen_index), "fifo":  self._last_fifo[int(gen_index)]}

    # ------------------------------------------------------------
    # Macro command G4: reset wave_memory
    # ------------------------------------------------------------
    
    # Set up only a reset of the wave memory and of the envelope memory
    def reset_wave_memory(
        self,
        *,
        gen_index: int,
        preserve_specs: bool = True,
        clear_last_fifo: bool = True,
    ) -> dict:
        """
        Reset the generator wave memory (HW + driver dict) and resync HL cache.

        preserve_specs:
            - True  -> keep WaveEntry specs but invalidate compiled WDW (set entry.wdw=None)
            - False -> clear HL wave cache entirely for this generator
        clear_last_fifo:
            - True  -> forget last programmed FIFO sequence in HL cache (recommended)
        """
        self.logger.info(
            "reset_wave_memory: gen=%d preserve_specs=%s clear_last_fifo=%s",
            gen_index, preserve_specs, clear_last_fifo
        )

        gen = self._get_gen(gen_index)

        # --- HW + LL reset
        # Driver should clear WaveMemoryDict and write zeros in wave memory.
        self._call(
            gen.reset_wave_memory_dict(),
            operation="reset_wave_memory_dict",
            driver_name="GeneratorDriver",
            config_error=True,
        )

        # --- HL resync
        cache = self.get_wave_cache(gen_index)
        n_before = len(cache)

        if preserve_specs:
            # Keep the spec, invalidate compilation result
            for entry in cache.values():
                entry.wdw = None
            hl_action = "invalidated_wdw"
        else:
            cache.clear()
            hl_action = "cleared_cache"

        if clear_last_fifo:
            self._last_fifo.pop(int(gen_index), None)

        # --- HL resync (readout wave)
        # Readout wave depends on envelopes, so invalidate or clear
        readout_entry = self._readout_wave_store.get(gen_index)
        if readout_entry is not None:
            if preserve_specs:
                readout_entry.wdw = None  # invalidate, keep spec
            else:
                self._readout_wave_store.pop(gen_index, None)  # clear entirely

        self.logger.info(
            "reset_wave_memory: done gen=%d hl_action=%s n_before=%d n_after=%d",
            gen_index, hl_action, n_before, len(cache)
        )

        return {
            "gen_index": int(gen_index),
            "hl_action": hl_action,
            "hl_wave_count_before": n_before,
            "hl_wave_count_after": len(cache),
            "cleared_last_fifo": bool(clear_last_fifo),
        }

    # ------------------------------------------------------------
    # Macro command G5: reset envelope_memory
    # ------------------------------------------------------------

    def reset_envelopes(
        self,
        *,
        gen_index: int,
        preserve_wave_specs: bool = True,
        clear_last_fifo: bool = True,
    ) -> dict:
        """
        Reset generator envelope memory (HW + driver dict) and resync HL wave cache.

        reset_waves:
            - True  -> also reset wave memory (recommended: waves depend on envelopes)
        preserve_wave_specs:
            - True  -> keep WaveEntry specs but invalidate compiled WDW only (entry.wdw=None)
            - False -> clear HL wave cache entirely
        clear_last_fifo:
            - True : delete the experiment fifo entirly
            - False : keep the experiment fifo. If you recompile the envelopes and the WDW properly, you can immediately rerun the experiment
        """
        self.logger.info(
            "reset_envelopes: gen=%d preserve_wave_specs=%s clear_last_fifo=%s",
            gen_index, preserve_wave_specs, clear_last_fifo
        )

        gen = self._get_gen(gen_index)

        # --- HW + LL reset envelopes
        # (rename this if your driver method name differs)
        self._call(
            gen.reset_envelope_dict(),
            operation="reset_envelope_dict",
            driver_name="GeneratorDriver",
            config_error=True,
        )


        # --- HL resync (waves)
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

        self.logger.info(
            "reset_envelopes: done gen=%d hl_action=%s n_before=%d n_after=%d",
            gen_index, hl_action, n_before, len(cache)
        )

        return {
            "gen_index": int(gen_index),
            "hl_action": hl_action,
            "hl_wave_count_before": n_before,
            "hl_wave_count_after": len(cache),
            "cleared_last_fifo": bool(clear_last_fifo),
        }
    
    # ------------------------------------------------------------
    # Macro command G6: Generator Modulation setup
    # ------------------------------------------------------------   
    def generator_modulation(self, gen_index: int, label: str , gen_mod : modulation ):

        self.logger.info(
            "generator_modulation: gen=%d label=%s frequency=%f phase (if readout )=%s",
            gen_index, label, gen_mod["frequency_mhz"], gen_mod["phase"]
        )
        gen = self._get_gen(gen_index)

        # Configure Mix-Mode via overlay (low-level handles tile/block mapping)
        try:
            mix_info = self.ol.configure_dac_mix_mode(gen_index, label, gen_mod["frequency_mhz"])
            if mix_info.get("changed"):
                self.logger.debug(
                    "Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                    mix_info["nyquist_zone"], mix_info["amd_zone"],
                    mix_info["tile"], mix_info["block"]
                )
        except ValueError as e:
            self.logger.debug(f"Mix-mode config skipped: {e}")

        if label in ["drive", "readout"]:
 
            if label == "drive":
                self._call(
                    gen.set_drive_dds_parameters(frequency=gen_mod["frequency_mhz"], dac_samplerate= self._dac_sr_mhz()),
                    operation= "set_drive_dds_parameters",
                    driver_name= "GeneratorDriver",
                    config_error= True
                )

            else:
                self._call(
                    gen.set_readout_dds_parameters(frequency=gen_mod["frequency_mhz"], phase= gen_mod["phase"], dac_samplerate= self._dac_sr_mhz()),
                    operation= "set_readout_dds_parameters",
                    driver_name= "GeneratorDriver",
                    config_error= True
                )
        else:
            raise ConfigurationError("Invalid mode selection!\nHint : select label =  'drive' or 'readout' ")
        self.logger.info("Modulation set-up!")
        return {
                "gen_index": gen_index,
                "label": label,
                "frequency_mhz": gen_mod["frequency_mhz"],
                "phase": gen_mod["phase"]
            }

    # ------------------------------------------------------------
    # Macro command G7: Generator Channel "listening" to trigger
    # ------------------------------------------------------------  
    def gen_trigger2listen(self, gen_index, trig: trigger_command):
        
        self.logger.info(
            "gen_trigger2listen: gen=%d ttype=%s channel=%s",
            gen_index, trig["ttype"], trig["channel"]
        )
        gen = self._get_gen(gen_index)
        
        self._call(
            gen.set_trigger_channel(channel= trig["channel"], ttype= trig["ttype"]),
            operation= "set_trigger_channel",
            driver_name= "GeneratorDriver",
            config_error= True
        )
    
        if(trig["channel"] == 0):
            self.logger.info("Generator %d is deaf to any trigger!", gen_index)
        else:
            self.logger.info("Generator %d listens to %s_trigger_word channel %d", gen_index,trig["ttype"], trig["channel"] )
        
        return {
            "gen_index": gen_index,
            "ttype": trig["ttype"],
            "channel":trig["channel"],
        }

    # ------------------------------------------------------------
    # Macro command G9: LFSR Configuration
    # ------------------------------------------------------------
    def configure_lfsr(
        self,
        *,
        gen_index: int,
        seed: int,
        enable: bool = True,
    ) -> dict:
        """
        Configure the Linear Feedback Shift Register (LFSR) for pseudo-random wave generation.

        When enabled, the wave sequence is determined by the LFSR output instead of the FIFO.

        :param gen_index: Index of the generator.
        :type gen_index: int
        :param seed: LFSR seed value (range [0, 2^SeedLfsrWidth - 1]).
        :type seed: int
        :param enable: If True, uses LFSR as sequence source. If False, uses FIFO.
        :type enable: bool
        :return: Applied LFSR configuration.
        :rtype: dict
        """
        self.logger.info(
            "configure_lfsr: gen=%d seed=%d enable=%s",
            gen_index, seed, enable
        )
        
        gen = self._get_gen(gen_index)
        
        
        # seed set-up
        self._call(
            gen.set_lfsr_seed(seed),
            operation="set_lfsr_seed",
            driver_name="GeneratorDriver",
            config_error=True,
        )
        
        # Select source (0=FIFO for deterministic sequence, 1=LFSR for random sequence)
        source = 1 if enable else 0
        self._call(
            gen.set_drive_order_source(source),
            operation="set_drive_order_source",
            driver_name="GeneratorDriver",
            config_error=True,
        )
        
        self.logger.info(
            "configure_lfsr: done gen=%d seed=%d source=%s",
            gen_index, seed, "LFSR" if enable else "FIFO"
        )
        
        return {
            "gen_index": gen_index,
            "seed": seed,
            "enabled": enable,
            "source": "LFSR" if enable else "FIFO",
            "max_seed": max_seed,
        }

    def set_drive_source(
        self,
        *,
        gen_index: int,
        source: Literal["fifo", "lfsr"],
    ) -> dict:
        """
        Select the source for the drive wave sequence.

        :param gen_index: Index of the generator.
        :type gen_index: int
        :param source: Selection between "fifo" (programmed sequence) or "lfsr" (pseudo-random).
        :type source: Literal["fifo", "lfsr"]
        :return: Selected source status.
        :rtype: dict
        """
        self.logger.info("set_drive_source: gen=%d source=%s", gen_index, source)
        
        gen = self._get_gen(gen_index)
        
        source_lower = str(source).lower()
        if source_lower == "fifo":
            source_val = 0
        elif source_lower == "lfsr":
            source_val = 1
        else:
            raise ConfigurationError(
                f"set_drive_source: invalid source='{source}'. Use 'fifo' or 'lfsr'."
            )
        
        self._call(
            gen.set_drive_order_source(source_val),
            operation="set_drive_order_source",
            driver_name="GeneratorDriver",
            config_error=True,
        )
        
        self.logger.info("set_drive_source: done gen=%d source=%s", gen_index, source_lower)
        
        return {
            "gen_index": gen_index,
            "source": source_lower,
        }
    
    # -------------- Trigger GENERATOR IP MACROS -----------------
    # ------------------------------------------------------------
    # Macro command TG1 : Program delay channels
    # ------------------------------------------------------------
    
    def tg_set_shots(self, shots: int) -> dict:

        self.logger.info("Setting up %d shots", shots)
        t = self._get_trig()
        shots_i = int(shots)

        if shots_i < 1 or shots_i > int(t.MaxHWRepetitions):
            raise ConfigurationError(
                f"shots={shots_i} out of range [1..{int(t.MaxHWRepetitions)}]"
            )

        self.logger.info("Success!")
        t.set_number_of_shots(shots_i)
        return {"shots": shots_i}
    
    def tg_set_duration(self, duration_cycles: int) -> dict:
        self.logger.info("Setting experiment duration. Clock Cycles : %d", duration_cycles)
        t = self._get_trig()
        dur_i = int(duration_cycles)
        if dur_i < 1 :
            raise ConfigurationError(
                f"duration={dur_i} is not Valid! Retry with a different value.]"
            )
        
        t.set_experiment_duration(dur_i)
        return {"experiment_duration": dur_i}
    
    def tg_program_delays(
            self,
            *,
            drive: dict | None = None,
            readout: dict | None = None,
            drive_start_index: int = 1,
            safe_pad: int = 0,
        ) -> dict:

        self.logger.info("Setting experiment delays in the Trigger Generator")
        self.logger.debug("---Experiment delay details--- \n1. drive_start_index = %d \n2.drive_delays = %s \n3.readout_delays= %s \n4. safe_pad = %d", drive_start_index, drive, readout, safe_pad)
        t = self._get_trig()
        drive = drive or {}
        readout = readout or {}

        start_idx = int(drive_start_index)
        if start_idx < 1 or start_idx > int(t.ChannelFifoDepth):
            raise ConfigurationError(f"drive_start_index={start_idx} out of range")

        # --- readout delays (1 scalar per channel)
        ro_programmed = []
        for ch_key, spec in readout.items():
            ch = int(ch_key)
            if not (isinstance(spec, dict) and "delay" in spec):
                raise ConfigurationError(f"readout[{ch}] must be dict with key 'delay'")
            ro_delay = int(spec["delay"])

            self._call(
                t.set_readout_delay(ro_delay, ch),
                operation="set_readout_delay",
                driver_name="TriggerGeneratorDriver",
                config_error=True,
            )
            ro_programmed.append(ch)

        # --- drive FIFO entries
        drive_report = {}
        for ch_key, spec in drive.items():
            ch = int(ch_key)
            if not (isinstance(spec, dict) and "delay" in spec):
                raise ConfigurationError(f"drive[{ch}] must be dict with key 'delay'")

            entries_list = list(spec["delay"])  # list of pairs

            # check capacity relative to start index
            max_writable = int(t.ChannelFifoDepth) - (start_idx - 1)
            if len(entries_list) > max_writable:
                raise ConfigurationError(
                    f"drive[{ch}] too long for start_index={start_idx}: "
                    f"{len(entries_list)} > {max_writable}"
                )

            # program
            for k, pair in enumerate(entries_list):
                if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                    raise ConfigurationError(f"drive[{ch}] entry #{k} must be (delay, gen_bit), got: {pair}")

                delay, gen = pair
                delay_i = int(delay)
                gen_i = 1 if int(gen) else 0

                fifo_index = start_idx + k  # LL index is 1-based
                self._call(
                    t.insert_drive_delay(ch, fifo_index, delay_i, gen_i),
                    operation="insert_drive_delay",
                    driver_name="TriggerGeneratorDriver",
                    config_error=True,
                )

            # optional padding after the written block
            if safe_pad:
                pad_to = min(int(t.ChannelFifoDepth), (start_idx - 1) + len(entries_list) + int(safe_pad))
                for fifo_index in range((start_idx + len(entries_list)), pad_to + 1):
                    self._call(
                        t.insert_drive_delay(ch, fifo_index, int(t.DriveDelayMax), 0),
                        operation="insert_drive_delay",
                        driver_name="TriggerGeneratorDriver",
                        config_error=True,
                    )

            drive_report[ch] = {
                "start_index": start_idx,
                "n_entries": len(entries_list),
                "padded": int(safe_pad),
            }

        return {
            "readout_channels_programmed": sorted(ro_programmed),
            "drive_programmed": drive_report,
        }

    # -------------- Acquisition IP MACROS -----------------
    # ------------------------------------------------------------
    # Macro command A1 :Acquisition IP setup 
    # ------------------------------------------------------------
    
    def acquisition_modulation(self, acq_index: int , acq_mod : modulation ):

        self.logger.info(
            "acquisition_modulation: acq=%d frequency=%s phase =%s ",
            acq_index, acq_mod["frequency_mhz"], acq_mod["phase"]
        )
        acq = self._get_acq(acq_index)

        # Configure Mix-Mode via overlay (low-level handles tile/block mapping)
        try:
            mix_info = self.ol.configure_adc_mix_mode(acq_index= acq_index, freq_mhz= acq_mod["frequency_mhz"])
            if mix_info.get("changed"):
                self.logger.debug(
                    "ADC Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                    mix_info["nyquist_zone"], mix_info["amd_zone"],
                    mix_info["tile"], mix_info["block"]
                )
        except ValueError as e:
            self.logger.warning(f"ADC Mix-mode config skipped: {e}")

        self._call(
                acq.set_acquisition_dds_parameters(frequency= acq_mod["frequency_mhz"] , phase= acq_mod["phase"], adc_samplerate= self._adc_sr_mhz()),
                operation= "set_acquisition_dds_parameters",
                driver_name= "AcquistionDriver",
                config_error= True
            )
        self.logger.info("acquisition_parameters: done acq=%d", acq_index)
        return {
            "acq_index": acq_index,
            "frequency_mhz": acq_mod["frequency_mhz"],
            "phase": acq_mod["phase"],
        }

    def acquisition_timing (self, acq_index, tof: int, duration: int): 

        self.logger.info(
            "acquisition_timing: acq_index=%d tof = %d",
            acq_index, tof
        )
        acq = self._get_acq(acq_index)
        
        self._call(
                acq.set_acquisition_duration(duration),
                operation= "set_acquisition_duration",
                driver_name= "AcquistionDriver",
                config_error= True
            )
        
        self._call(
                acq.set_time_of_flight(tof),
                operation= "set_time_of_flight",
                driver_name= "AcquistionDriver",
                config_error= True
            )
        self.logger.info("Acquisition timing set up!")
        return {
                "acq_index": acq_index,
                "tof": tof,
                "duration": duration,
            }

    def acq_trigger2listen(self, acq_index, trig: trigger_command):
        self.logger.info(
            "acq_trigger2listen: acq=%d channel=%s",
            acq_index, trig["channel"]
        )
        acq = self._get_acq(acq_index)
        
        self._call(
            acq.set_trigger_channel(channel= trig["channel"]),
            operation= "set_trigger_channel",
            driver_name= "AcquistionDriver",
            config_error= True
        )
    
        if(trig["channel"] == 0):
            self.logger.info("Acquisition %d is deaf to any trigger!", acq_index)
        else:
            self.logger.info("Generator %d listens to %s_trigger_word channel %d", acq_index, trig["ttype"], trig["channel"] )
        
        
        return {
            "acq_index": acq_index,
            "channel":trig["channel"],
        }
            

    def run_multi_acquisition(
        self,
        *,
        adc_indices: List[int],
        mode: Literal["raw", "decimated", "accumulated"],
        shots: int,
        samp_per_shot: int,
        timeout: Optional[float] = 10.0
    ) -> Dict[int, np.ndarray]:
        """
        Perform synchronized multi-ADC acquisition.

        Orchestrates the AXI-Stream switch routing, arms the DMA engine for the 
        primary ADC, triggers the global experiment, and retrieves data from 
        all specified channels sequentially.

        :param adc_indices: List of hardware ADC indices to acquire from.
        :type adc_indices: List[int]
        :param mode: Acquisition modality (raw, decimated, or accumulated).
        :type mode: Literal["raw", "decimated", "accumulated"]
        :param shots: Number of triggers/shots.
        :type shots: int
        :param samp_per_shot: Samples per single trigger.
        :type samp_per_shot: int
        :param timeout: Maximum wait time for DMA transfers in seconds.
        :type timeout: Optional[float]
        :return: Dictionary mapping ADC indices to their respective NumPy data arrays.
        :rtype: Dict[int, np.ndarray]
        """
        results = {}
        self.logger.info("run_multi_acquisition: ADCs=%s mode=%s shots=%d", adc_indices, mode, shots)

        if not adc_indices:
            return results

        # A. PRE-CONFIG:set-up a common configuration for all ADC
        #NOTE : can be update for future works
        for adc_i in adc_indices:
            if mode in ("decimated", "accumulated"):
                if adc_i < len(self.ol.acquisitions):
                    self.ol.acquisitions[adc_i].set_decimated_output_type(mode)

        # B. FIRST ADC: Switch + Arm DMA BEFORE the first trigger -> unless the acquisition would fail
        first_adc = adc_indices[0]
        self.logger.debug(f"Preparing first ADC {first_adc}: switch + arm DMA")

        first_buffer = self.dma_engine.arm_acquisition(
            samp_per_shot=samp_per_shot,
            shots=shots,
            mode=mode,
            adc_index=first_adc
        )

        # C. EXPERIMENT  
        self.logger.info("Firing GLOBAL Experiment Trigger...")
        self.ol.tg_set_shots(shots) 
        self.ol.trigger.start_experiment()

        # D. DATA COLLECTION : FIRST ADC REQUESTED
        try:
            data = self.dma_engine.retrieve_acquisition(
                buffer=first_buffer,
                mode=mode,
                shots=shots,
                timeout=timeout
            )
            results[first_adc] = data
            self.logger.debug(f"ADC {first_adc} data retrieved successfully")
        except Exception as e:
            self.logger.error(f"Failed first ADC {first_adc}: {e}")
            results[first_adc] = None

        # E.REMAINING ADCs
        for adc_i in adc_indices[1:]:
            self.logger.debug(f"Reading buffer from ADC {adc_i}")
            try:
                buffer = self.dma_engine.arm_acquisition(
                    samp_per_shot=samp_per_shot,
                    shots=shots,
                    mode=mode,
                    adc_index=adc_i
                )

                data = self.dma_engine.retrieve_acquisition(
                    buffer=buffer,
                    mode=mode,
                    shots=shots,
                    timeout=timeout
                )
                results[adc_i] = data

            except Exception as e:
                self.logger.error(f"Failed readout for ADC {adc_i}: {e}")
                results[adc_i] = None

        return results