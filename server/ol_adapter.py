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

from FIREQ_LL_API.overlay_driver import FIREQ_SoC
from dma_engine import AcquisitionEngine
from .exceptions import *


# Driver error codes (from GeneratorDriver documentation)
_ERR_ENVELOPE_NOT_FOUND = -1
_ERR_MEMORY_NOT_INITIALIZED = -2
_ERR_INVALID_PARAMS = -3
_ERR_MEMORY_FULL = -4


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
    Handle FIREQ low-level driver return codes (negative ints) and raise exceptions.

    Unlike _handle_driver_result(), this helper returns `result` when the call succeeds,
    so you can do one-liners like:

        wdw = handle_error_result(gen.create_wave_definition_word(...),
                                 operation="create_wave_definition_word",
                                 driver_name="GeneratorDriver",
                                 logger=self.logger,
                                 config_error=True)

    Parameters
    ----------
    result : Any
        Return value from a driver method. If it's a negative int => error.
    operation : str
        Name of the operation (method name) used for error reporting.
    driver_name : str
        Name of the driver class used for error reporting.
    logger : logging.Logger
        Logger instance.
    config_error : bool
        If True, raise ConfigurationError for negative codes (instead of DriverError).
    hint : Optional[str]
        Explicit hint message overriding any auto hint.
    error_hints : Optional[Dict[tuple, str]]
        Optional mapping (driver_name, operation, code) -> hint string.
        If provided and `hint` is None, the mapping is used.
    error_exc : Optional[Dict[tuple, type]]
        Optional mapping (driver_name, operation, code) -> Exception subclass.
        If provided and matches, that exception type is raised.

    Returns
    -------
    Any
        The original `result` if no error is detected.

    Raises
    ------
    ConfigurationError
        If result is a negative int and config_error=True.
    DriverError
        If result is a negative int and config_error=False.
    (custom exception)
        If error_exc matches (driver_name, operation, code).
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
    - To program FIFO sequences, we ensure each WDW is present in wave memory (slot-based).
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
        ("AcquistionDriver", "set_acquistion_parameters", -3):
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
        self.summary = ol.summary()  

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
        )

        # per-generator caches
        self._wave_id_to_name: Dict[int, Dict[int, str]] = {}   # gen_index -> {wdw_int: wave_name}
        self._last_fifo_len: Dict[int, int] = {}                # gen_index -> last programmed length

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

    def _dac_sr_hz(self) -> int:
        return int(self.ol.hw_specs["summary"]["dac_sr_hz"])

    def _adc_sr_mhz(self) -> float:
        return float(self.ol.hw_specs["summary"]["adc_sr_hz"]) / 1e6

    @staticmethod
    def _wave_name_from_wdw(wdw_int: int) -> str:
        # deterministic and idempotent
        return f"W_{wdw_int:032x}"

    def _get_wave_cache(self, gen_index: int) -> Dict[int, str]:
        if gen_index not in self._wave_id_to_name:
            self._wave_id_to_name[gen_index] = {}
        return self._wave_id_to_name[gen_index]

    def _ensure_wave_in_wave_memory(self, gen_index: int, wdw_int: int) -> str:
        """
        Ensure the given WDW is present in generator wave memory.
        Returns the wave_name used to reference it in FIFO.
        """
        gen = self._get_gen(gen_index)
        cache = self._get_wave_cache(gen_index)

        if wdw_int in cache:
            return cache[wdw_int]

        wave_name = self._wave_name_from_wdw(wdw_int)

        # if already in driver cache, don't re-add
        if wave_name in getattr(gen, "WaveMemoryDict", {}):
            cache[wdw_int] = wave_name
            return wave_name

        self._call(
            gen.add_wave_in_wave_memory(int(wdw_int), wave_name),
            operation="add_wave_in_wave_memory",
            driver_name="GeneratorDriver",
            config_error=True,
        )
        cache[wdw_int] = wave_name
        return wave_name

    def _iq_float_to_cint16(self, samples_iq: List[List[float]], sample_bits: int) -> np.ndarray:
        # prepare samples to envelope memory upload
        vmax = (1 << (sample_bits - 1)) - 1
        a = np.asarray(samples_iq, dtype=np.float64)
        i = np.clip(np.rint(a[:, 0] * vmax), -vmax - 1, vmax).astype(np.int16)
        q = np.clip(np.rint(a[:, 1] * vmax), -vmax - 1, vmax).astype(np.int16)
        return i + 1j * q

    # ------------------------------------------------------------
    # Macro command 1: upload_envelopes
    # ------------------------------------------------------------
    def upload_envelopes(
        self,
        *,
        gen_index: int,
        envelopes: List[dict],
        skip_if_exists: bool = False,
        auto_pad_noninterp: bool = True,
    ) -> dict:
        """
        Upload multiple envelopes into the generator envelope memory.

        envelopes item schema (server-purified):
        {
          "name": str,
          "for_interpolation": bool,
          "is_symmetric": bool,
          "i_even": bool,
          "q_even": bool,
          "samples_iq": [[float,float], ...]   # normalized in [-1,1]
        }
        """
        gen = self._get_gen(gen_index)
        loaded: List[str] = []
        failed: List[dict] = []

        env_cache = getattr(gen, "EnvelopeMemoryDict", {})

        for e in envelopes:
            name = str(e.get("name", ""))
            try:
                if not name:
                    raise ConfigurationError("Envelope name is empty")

                if skip_if_exists and name in env_cache:
                    loaded.append(name)
                    continue

                for_interp = bool(e["for_interpolation"])
                is_sym = bool(e.get("is_symmetric", False))
                i_even = bool(e.get("i_even", False))
                q_even = bool(e.get("q_even", False))
                samples_iq = e["samples_iq"]

                env = self._iq_float_to_cint16(samples_iq, int(gen.SampleSize))

                # Non-interp: must be multiple of generator parallelism
                if auto_pad_noninterp and (not for_interp):
                    par = int(gen.NumberOfChannels)
                    r = int(env.size) % par
                    if r != 0:
                        env = np.pad(env, (0, par - r), mode="constant")

                self._call(
                    gen.add_envelope_to_envelope_memory(env, for_interp, is_sym, i_even, q_even, name),
                    operation="add_envelope_to_envelope_memory",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )
                loaded.append(name)

            except Exception as ex:
                failed.append({"name": name, "error": str(ex)})

        return {"gen_index": int(gen_index), "loaded": loaded, "failed": failed}

    # ------------------------------------------------------------
    # Macro command 2: compile_waves
    # ------------------------------------------------------------
    def compile_waves(
        self,
        *,
        gen_index: int,
        waves: List[dict],
        also_insert_in_wave_memory: bool = True,
    ) -> dict:
        """
        Create WDW for each wave and optionally insert them into wave memory.

        waves item schema (server-purified):
        {
          "wave_key": str (optional),
          "envelope": str,
          "duration_samples": int (or "duration"),
          "gain": float,
          "switch_iq": bool,
          "keep_last": bool
        }
        """
        gen = self._get_gen(gen_index)
        out: List[dict] = []

        for w in waves:
            envelope = str(w["envelope"])
            duration = int(w.get("duration_samples", w.get("duration", 0)))
            gain = float(w["gain"])
            switch_iq = bool(w.get("switch_iq", False))
            keep_last = bool(w.get("keep_last", False))
            wave_key = str(w.get("wave_key", envelope))

            wdw = self._call(
                gen.create_wave_definition_word(envelope, duration, gain, switch_iq, keep_last),
                operation="create_wave_definition_word",
                driver_name="GeneratorDriver",
                config_error=True,
            )
            wdw_int = int(wdw)
            wave_id_hex = f"{wdw_int:032x}"

            wave_name = None
            if also_insert_in_wave_memory:
                wave_name = self._ensure_wave_in_wave_memory(gen_index, wdw_int)

            out.append(
                {
                    "wave_key": wave_key,
                    "wave_id": wave_id_hex,
                    "wave_name": wave_name,
                }
            )

        return {"gen_index": int(gen_index), "waves": out}

    # ------------------------------------------------------------
    # Utility: program FIFO drive sequence from wave_ids
    # ------------------------------------------------------------
    def program_drive_sequence(
        self,
        *,
        gen_index: int,
        drive_wave_ids: List[str],
    ) -> dict:
        """
        Programs the generator FIFO with a sequence of waves identified by wave_id hex (WDW).
        """
        gen = self._get_gen(gen_index)

        self._call(
            gen.set_drive_order_source(0),  # FIFO
            operation="set_drive_order_source",
            driver_name="GeneratorDriver",
            config_error=True,
        )

        for i, wid_hex in enumerate(drive_wave_ids, start=1):
            wdw_int = int(str(wid_hex), 16)
            wave_name = self._ensure_wave_in_wave_memory(gen_index, wdw_int)

            self._call(
                gen.add_wave_to_drive_wave_sequence(i, wave_name),
                operation="add_wave_to_drive_wave_sequence",
                driver_name="GeneratorDriver",
                config_error=True,
            )

        self._last_fifo_len[int(gen_index)] = len(drive_wave_ids)
        return {"gen_index": int(gen_index), "fifo_len": len(drive_wave_ids)}

    # ------------------------------------------------------------
    # Macro command 3: run_experiment
    # ------------------------------------------------------------
    def run_experiment(
        self,
        *,
        # routing + dma
        adc_index: int,
        mode: "Literal['raw','decimated','accumulated']",
        num_samples: int,
        shots: int,
        timeout_s: float,

        # which IPs
        gen_index: int = 0,
        acq_index: int = 0,

        # generator config (server-purified)
        generator_drive: Optional[dict] = None,
        generator_readout: Optional[dict] = None,
        readout_wave_id: Optional[str] = None,      # optional WDW hex for readout wave
        drive_wave_ids: Optional[List[str]] = None, # FIFO sequence

        # acquisition config (server-purified)
        acquisition_cfg: Optional[dict] = None,

        # trigger config (server-purified)
        trigger_duration_cycles: int = 0,
        trigger_drive_delays: Optional[Dict[int, dict]] = None,
        trigger_readout_delays: Optional[Dict[int, int]] = None,
    ) -> dict:
        """
        Execute a single experiment iteration.

        - Programs generator (trigger channels + DDS + readout wave + drive FIFO)
        - Programs acquisition (trigger channel + demod params + tof + output type)
        - Programs trigger generator (duration, shots, delays)
        - Arms DMA, starts experiment, retrieves acquisition
        """
        gen = self._get_gen(gen_index)
        acq = self._get_acq(acq_index)
        trig = self._get_trig()

        # --------------------------
        # Basic validation (server-grade)
        # --------------------------
        shots = int(shots)
        if shots < 1:
            raise ConfigurationError("shots must be >= 1")
        if shots > int(getattr(trig, "MaxHWRepetitions", shots)):
            raise ConfigurationError(f"shots={shots} exceeds HW MaxHWRepetitions={trig.MaxHWRepetitions}")

        if trigger_duration_cycles <= 0:
            raise ConfigurationError("trigger_duration_cycles must be > 0")
        if trigger_duration_cycles > int(getattr(trig, "ExperimentTimerMax", trigger_duration_cycles)):
            raise ConfigurationError(
                f"trigger_duration_cycles={trigger_duration_cycles} exceeds ExperimentTimerMax={trig.ExperimentTimerMax}"
            )

        # --------------------------
        # 1) Generator configuration
        # --------------------------
        dac_sr_hz = self._dac_sr_hz()

        if generator_drive is not None:
            # trigger channel for drive
            ch = int(generator_drive.get("channel", 0))
            self._call(
                gen.set_trigger_channel(ch, "drive"),
                operation="set_trigger_channel",
                driver_name="GeneratorDriver",
                config_error=True,
            )

            # DDS drive frequency (phase not supported in current HW)
            if "frequency" in generator_drive:
                self._call(
                    gen.set_drive_dds_parameters(float(generator_drive["frequency"]), dac_sr_hz),
                    operation="set_drive_dds_parameters",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )

            # If server sends phase, we ignore (no HW support)
            if "phase" in generator_drive and float(generator_drive["phase"]) != 0.0:
                self.logger.warning("Drive phase provided but not supported by current GeneratorDriver (ignored).")

        if generator_readout is not None:
            ch = int(generator_readout.get("channel", 0))
            self._call(
                gen.set_trigger_channel(ch, "readout"),
                operation="set_trigger_channel",
                driver_name="GeneratorDriver",
                config_error=True,
            )

            if "frequency" in generator_readout or "phase" in generator_readout:
                f = float(generator_readout.get("frequency", 0.0))
                ph = float(generator_readout.get("phase", 0.0))
                self._call(
                    gen.set_readout_dds_parameters(f, ph, dac_sr_hz),
                    operation="set_readout_dds_parameters",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )

        if readout_wave_id is not None:
            wdw_int = int(str(readout_wave_id), 16)
            self._call(
                gen.write_readout_wave(wdw_int),
                operation="write_readout_wave",
                driver_name="GeneratorDriver",
                config_error=True,
            )

        if drive_wave_ids is not None:
            self.program_drive_sequence(gen_index=gen_index, drive_wave_ids=drive_wave_ids)

        # --------------------------
        # 2) Acquisition configuration
        # --------------------------
        if acquisition_cfg is None:
            raise ConfigurationError("acquisition_cfg is required")

        # trigger channel
        if "channel" in acquisition_cfg:
            self._call(
                acq.set_trigger_channel(int(acquisition_cfg["channel"])),
                operation="set_trigger_channel",
                driver_name="AcquistionDriver",
                config_error=True,
            )

        # tof
        if "time_of_flight" in acquisition_cfg:
            self._call(
                acq.set_time_of_flight(int(acquisition_cfg["time_of_flight"])),
                operation="set_time_of_flight",
                driver_name="AcquistionDriver",
                config_error=True,
            )

        # output type for decimated stream
        if mode in ("decimated", "accumulated"):
            out_type = "accumulated" if mode == "accumulated" else "decimated"
            self._call(
                acq.set_decimated_output_type(out_type),
                operation="set_decimated_output_type",
                driver_name="AcquistionDriver",
                config_error=True,
            )

        # demod + duration
        adc_sr_mhz = self._adc_sr_mhz()
        freq_mhz = float(acquisition_cfg.get("frequency", acquisition_cfg.get("freq_mhz", 0.0)))
        phase_rad = float(acquisition_cfg.get("phase", acquisition_cfg.get("phase_rad", 0.0)))
        dur = int(acquisition_cfg.get("acquisition_duration", acquisition_cfg.get("duration_cycles", 0)))
        if dur <= 0:
            raise ConfigurationError("Acquisition duration must be > 0 (duration_cycles/acquisition_duration)")

        self._call(
            acq.set_acquistion_parameters(freq_mhz, phase_rad, dur, adc_sr_mhz),
            operation="set_acquistion_parameters",
            driver_name="AcquistionDriver",
            config_error=True,
        )

        # --------------------------
        # 3) Trigger generator configuration
        # --------------------------
        trig.set_experiment_duration(int(trigger_duration_cycles))
        trig.set_number_of_shots(int(shots))

        # Drive delays: {channel: {"delay":[...], "generate_trigger":[...]}}
        if trigger_drive_delays:
            for ch, cfg in trigger_drive_delays.items():
                ch_i = int(ch)
                delays = list(cfg.get("delay", []))
                gens = list(cfg.get("generate_trigger", []))
                if len(gens) != len(delays):
                    raise ConfigurationError(f"Drive delay list and generate_trigger list size mismatch on channel {ch_i}")

                for idx, (d, g) in enumerate(zip(delays, gens), start=1):
                    self._call(
                        trig.insert_drive_delay(ch_i, idx, int(d), int(bool(g))),
                        operation="insert_drive_delay",
                        driver_name="TriggerGeneratorDriver",
                        config_error=True,
                    )

        # Readout delays: {channel: delay}
        if trigger_readout_delays:
            for ch, d in trigger_readout_delays.items():
                self._call(
                    trig.set_readout_delay(int(d), int(ch)),
                    operation="set_readout_delay",
                    driver_name="TriggerGeneratorDriver",
                    config_error=True,
                )

        # --------------------------
        # 4) DMA arm -> start -> retrieve
        # --------------------------
        buf = self.dma_engine.arm_acquisition(int(num_samples), int(shots), mode, int(adc_index))
        trig.start_experiment()
        data = self.dma_engine.retrieve_acquisition(buf, mode, int(shots), float(timeout_s))

        return {
            "success": True,
            "mode": str(mode),
            "adc_index": int(adc_index),
            "shots": int(shots),
            "num_samples": int(num_samples),
            "data": data,
            "timing": {
                "duration_cycles_effective": int(trigger_duration_cycles),
            },
        }

