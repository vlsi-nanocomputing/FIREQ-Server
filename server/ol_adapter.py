# file: fireq-utils/server/ol_adapter.py
"""
fireq_utils.server.ol_adapter
=====================================

Purpose
-------
This module implements a *server-facing adapter layer* on top of the low-level
FIREQ hardware drivers exposed by `FIREQ_LL_API.overlay_driver.FIREQ_SoC`.

It applies the Adapter pattern to:
- expose an API for experiment execution,
- expose meaningful error logs,
- keep a coherent High-Level (HL) cache synchronized with Low-Level (LL) driver state.

Key design principles
---------------------
- Fail fast on configuration errors (never let invalid states reach hardware).
- Make HL-LL synchronization explicit and verifiable.
- Expose JSON-serializable outputs suitable for logging and remote control.

Limitations
-----------
- It assumes exclusive ownership of the underlying overlay.
- It assumes hardware configuration does not change outside this API.
"""


# ======================================================================
# GENERATOR ADAPTER (server-oriented)
# ======================================================================

import logging
from typing import Any, Dict, List, Optional, Literal, TypedDict, Tuple
import numpy as np
from dataclasses import dataclass
from FIREQ_LL_API import FIREQ_SoC
from .dma_engine import AcquisitionEngine
from .exceptions import ConfigurationError, DriverError, HardwareStateError
import time

class Modulation(TypedDict):
    """
    Specification for Local Oscillator (LO) modulation parameters.

    :param frequency_mhz: The modulation frequency in MHz.
    :type frequency_mhz: float
    :param phase: The phase offset in degrees (optional, primarily for readout).
    :type phase: Optional[float]
    """
    frequency_mhz: float
    phase: Optional[float]

class TriggerCommand(TypedDict):
    """
    Specification for a trigger configuration command.

    :param ttype: The trigger type identifier (e.g., 'start', 'readout').
    :type ttype: str
    :param channel: The target channel index for the trigger.
    :type channel: int
    """
    ttype: str
    channel: int

class EnvelopeSpec(TypedDict):
    """
    Specification for a waveform envelope to be uploaded.

    :param name: Unique identifier for the envelope.
    :type name: str
    :param for_interpolation: Indicates if the envelope is designed for hardware interpolation.
    :type for_interpolation: bool
    :param is_symmetric: Indicates if the envelope has symmetry optimization.
    :type is_symmetric: bool
    :param i_even: Symmetry flag for the In-phase component.
    :type i_even: bool
    :param q_even: Symmetry flag for the Quadrature component.
    :type q_even: bool
    :param samples_iq: List of [I, Q] floating-point sample pairs.
    :type samples_iq: List[List[float]]
    """
    name: str
    for_interpolation: bool
    is_symmetric: bool
    i_even: bool
    q_even: bool
    samples_iq: List[List[float]]

# WAVE TYPES: regular waves vs virtual Z gates

WaveKind = Literal["env", "vz"]  # env = X/Y/readout, vz = Virtual-Z

@dataclass
class WaveEntry:
    """
    A unified high-level representation of a waveform or virtual gate command.

    This data structure serves as the primary cache entry for the generator, acting
    as a discriminated union between standard envelope-based waves and Virtual-Z (VZ) gates.
    It stores both the high-level specification and the compiled hardware instruction (WDW).

    :param kind: Discriminator tag. Use 'env' for standard pulses or 'vz' for phase updates.
    :type kind: WaveKind
    :param envelope: The name of the envelope shape stored in hardware memory (used only if kind='env').
    :type envelope: str
    :param duration: The duration of the pulse in FPGA clock cycles (used only if kind='env').
    :type duration: int
    :param gain: The digital gain scaling factor in range [-1.0, 1.0] (used only if kind='env').
    :type gain: float
    :param switch_iq: If True, swaps the I and Q signal paths (used only if kind='env').
    :type switch_iq: bool
    :param keep_last: If True, holds the last sample value after duration ends (used only if kind='env').
    :type keep_last: bool
    :param vz_phase_rad: The phase increment in radians (used only if kind='vz').
    :type vz_phase_rad: float
    :param wdw: The compiled 128-bit Wave Definition Word. If None, the entry requires compilation.
    :type wdw: Optional[int]
    """
    kind: WaveKind = "env"
    # --- env waves(X/Y/readout)
    envelope: str = ""
    duration: int = 1
    gain: float = 0.0
    switch_iq: bool = False
    keep_last: bool = False
    
    # --- vz waves ---
    vz_phase_rad: float = 0.0

    # --- compiled outcome ---
    wdw: Optional[int] = None

def _same_spec(a: WaveEntry, b: WaveEntry) -> bool:
    """
    Compare two WaveEntry objects for functional hardware equivalence.

    Equality here denotes that two instances produce the same Wave Definition Word (WDW)
    and FPGA behavior, allowing for safe compilation skipping.

    :param a: The first wave entry.
    :type a: WaveEntry
    :param b: The second wave entry.
    :type b: WaveEntry
    :return: True if the entries are functionally equivalent, False otherwise.
    :rtype: bool
    """
    if a.kind != b.kind:
        return False
    # X/Y/Readout  "envelope-centric"
    if a.kind == "env":
        return (
            a.envelope == b.envelope
            and a.duration == b.duration
            and a.gain == b.gain
            and a.switch_iq == b.switch_iq
            and a.keep_last == b.keep_last
        )

    # VZ: envelope/duration/gain : "phase-centric"
    return float(a.vz_phase_rad) == float(b.vz_phase_rad)
        
def handle_error_result(result: Any,
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
    Normalize FIREQ low-level driver return codes into Python exceptions.
    ----------------
    Low-level FIREQ drivers return integer error codes instead of raising
    exceptions. This function centralizes the translation logic to:

    - enforce a uniform error-handling policy across all drivers,
    - attach semantic context (driver name, operation),
    - optionally upgrade errors to configuration-time failures,
    - provide user-facing hints for known error patterns.
    
    Error policy
    ------------
    - Non-negative results (or non-integers) are treated as success and
    passed through unchanged.
    - Negative integers are interpreted as error codes.
    - Known error codes may be mapped to:
        * a specific exception class,
        * a human-readable diagnostic hint.
    - Unknown error codes fail fast with a generic DriverError.

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

class OverlayAdapter:
    """
    High-level adapter for FIREQ hardware control.

    This class provides a "server" interface on top of FIREQ_SoC,
    bundling together multiple low-level driver calls into coherent macro-operations.

    Responsibilities
    ----------------
    - Translate server commands into ordered hardware actions.
    - Maintain a High-Level (HL) cache of waves, envelopes, and FIFOs.
    - Enforce invariants before programming hardware.
    - Synchronize HL cache state with Low-Level (LL) driver state.
    - Centralize error handling and diagnostics.
    
    Statefulness
    ------------
    This adapter is intentionally stateful:
    - caches WaveEntry objects per generator,
    - tracks last programmed FIFO sequences,
    - remembers readout wave configuration,
    - accumulates timing statistics.

    This state is required to:
    - detect redundant operations safely,
    - enable fast paths correctly,
    - detect and stop inconsistencies early.

    Notes:
    - Wave IDs are treated as 128-bit Wave Definition Words (WDW) serialized in hex.
    - To program FIFO sequences, we ensure each WDW is present in wave memory (wave_id-based).
    - wave_id uniquely identifies a logical wave within a generator.
    - Replacement of existing waves is always explicit (replace=True).
    """

    # ERROR HINTS
    # user-friendly hints for common negative codes
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
        ("GeneratorDriver", "create_vz_gate_definition_word", -3):
            "Check: phase offset in radians is finite; driver expects a 48-bit signed value in WDW[47:0] and sets IS_VZ_GATE (bit 119).",
        ("AcquisitionDriver", "set_acquisition_dds_parameters", -3):
            "Check: frequency>=0, duration in [1..MaximumDuration], adc_samplerate correct.",
        ("TriggerGeneratorDriver", "insert_drive_delay", -3):
            "Check: channel range, index range, delay range, generate_trigger is 0/1.",
        ("TriggerGeneratorDriver", "set_readout_delay", -3):
            "Check: readout channel range, delay non-negative and within HW limits.",
    }

    def __init__(self, ol: FIREQ_SoC, *, logger: Optional[logging.Logger] = None):
        """
        Initialize the High-Level Adapter.

        :param ol: The low-level overlay driver instance.
        :type ol: FIREQ_SoC
        :param logger: Optional logger instance for telemetry. If None, a default logger is created.
        :type logger: Optional[logging.Logger]
        """
        # Fail-fast sanity check:
        if not ol.is_healthy:
            raise HardwareStateError("Unexpected Error: overlay upload failed!")

        self.ol = ol
        self.logger = logger or logging.getLogger(__name__)
        # DMA engine (needed for run_experiment)
        if self.ol.dma is None or self.ol.axis_switch is None:
            raise HardwareStateError("DMA or AXI-Stream switch missing in overlay")
        # The DMA engine is constructed once as a long-lived resource.
        self.dma_engine = AcquisitionEngine(
            self.ol.dma,
            self.ol.axis_switch,
            logger=self.logger,
            hw_specs= self.ol.hw_specs
            
        )

        # per-generator caches
        # Create a memory of the compiled WDW. Each wdw is accessible via the wave_id as key
        self._wave_store: Dict[int, Dict[str, WaveEntry]] = {}   # gen_index -> waves = { wave_id:str , WaveEntry]}
        # Create a memory of the last used experiment
        self._last_fifo: Dict[int, List[str]] = {}  # gen_index -> last programmed FIFO: [wdw0, wdw1, ...]
        # Create a memory for readout waves (one per generator)
        self._readout_wave_store: Dict[int, WaveEntry] = {}    # gen_index -> current readout WaveEntry 
        
        # Timing for statistics (fpga_active_ms is DMA wait time proxy).
        self.last_timing_stats = {
            "sw_overhead_ms": 0.0,
            "fpga_active_ms": 0.0
        }
        self._sweep_prepared = False
    # ------------------------------------------------------------
    # Pass-through: everything not defined here goes to self.ol
    # ------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        """
        Delegate attribute access to the underlying low-level overlay driver.

        This method implements the Proxy pattern, allowing the adapter to transparently
        expose the full API of the wrapped ``FIREQ_SoC`` instance. Any attribute or method
        not explicitly defined in this adapter is automatically forwarded to the hardware driver.

        Therefore, the "expert" user can directly use the underlying driver methods. The only purpose
        is to speedup debugging operation and ease developers' work.

        :param name: The name of the attribute to retrieve.
        :type name: str
        :return: The attribute value from the low-level driver.
        :rtype: Any
        :raises AttributeError: If the attribute is not found in either the adapter or the underlying driver.
        """
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
        
        """
        Uniform wrapper for low-level driver calls with centralized error handling.

        This method acts as a middleware that intercepts the integer return codes from
        the Low-Level API. It standardizes logging, injects diagnostic hints, and
        converts error codes into semantic Python exceptions.

        :param result: The raw return value from the low-level driver method (typically an integer status code).
        :type result: Any
        :param operation: The name of the specific operation being performed (used for logging context).
        :type operation: str
        :param driver_name: The name of the low-level driver class (used for logging context).
        :type driver_name: str
        :param config_error: Strategy flag. If True, maps failures to ``ConfigurationError`` (invalid user input). If False, maps failures to ``DriverError`` (hardware/runtime failure).
        :type config_error: bool
        :param hint: An explicit diagnostic hint to append to the error message if the call fails, overriding default lookups.
        :type hint: Optional[str]
        :return: The original ``result`` passed through unchanged if it indicates success (non-negative).
        :rtype: Any
        :raises ConfigurationError: If the result is negative and ``config_error`` is True.
        :raises DriverError: If the result is negative and ``config_error`` is False.
        """
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
        """
        Helper to safely retrieve the low-level driver for a specific generator.

        Wraps the list access to ensure that invalid indices raise a high-level
        configuration error rather than a generic Python lookup error.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: The low-level generator driver instance.
        :rtype: Any
        :raises ConfigurationError: If the index is out of bounds or invalid.
        """
        try:
            return self.ol.generators[int(gen_index)]
        except Exception as e:
            raise ConfigurationError(f"Invalid gen_index={gen_index}") from e

    def _get_acq(self, acq_index: int):
        """
        Helper to safely retrieve the low-level driver for a specific acquisition unit.

        Wraps the list access to ensure that invalid indices raise a high-level
        configuration error rather than a generic Python lookup error.

        :param acq_index: Index of the target acquisition unit.
        :type acq_index: int
        :return: The low-level acquisition driver instance.
        :rtype: Any
        :raises ConfigurationError: If the index is out of bounds or invalid.
        """
        try:
            return self.ol.acquisitions[int(acq_index)]
        except Exception as e:
            raise ConfigurationError(f"Invalid acq_index={acq_index}") from e

    def _get_trig(self):
        """
        Helper to safely retrieve the low-level Trigger Generator driver.

        Validates the existence of the trigger IP in the current overlay configuration
        before returning it.

        :return: The low-level trigger driver instance.
        :rtype: Any
        :raises HardwareStateError: If the TriggerGenerator IP is missing from the overlay.
        """
        t = self.ol.trigger
        if t is None:
            raise HardwareStateError("No TriggerGenerator IP found")
        return t

    def _dac_sr_mhz(self) -> float:
        """
        Retrieve the DAC sampling rate from hardware specifications and convert it to MHz.

        :return: The DAC sampling rate in MHz.
        :rtype: float
        """
        return float(self.ol.hw_specs["summary"]["dac_sr_hz"])/ 1e6

    def _adc_sr_mhz(self) -> float:
        """
        Retrieve the ADC sampling rate from hardware specifications and convert it to MHz.

        :return: The ADC sampling rate in MHz.
        :rtype: float
        """
        return float(self.ol.hw_specs["summary"]["adc_sr_hz"]) / 1e6
    
    def _lookup_wave_in_wave_memory(self, gen_index: int, wdw_int: int) -> str:
        """
        Resolve a compiled Wave Definition Word (WDW) back to its unique wave_id.

        This method enforces strict consistency between the High-Level (HL) cache
        and the Low-Level (LL) generator memory.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param wdw_int: The integer representation of the Wave Definition Word.
        :type wdw_int: int
        :return: The unique wave_id associated with the WDW.
        :rtype: str
        :raises ConfigurationError: If the WDW is not found, ambiguous, or inconsistent with LL state.
        """

        gen = self._get_gen(gen_index)          
        cache: Dict[str, WaveEntry] = self._get_wave_cache(gen_index)

        # find wave_ids whose cached WDW matches (skip entries not compiled yet: wdw=None)
        matches: List[str] = [
            wave_id
            for wave_id, entry in cache.items()
            if entry.wdw is not None and (int(entry.wdw) == int(wdw_int))
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
                f"Inconsistent state: wave_id='{wave_id}' has matching WDW in WaveEntry but not in driver WaveMemoryDict"
            )

        return wave_id

    def _iq_float_to_cint16(self, samples_iq: List[List[float]], sample_bits: int) -> np.ndarray:
        """
        Convert user-space floating-point IQ samples into fixed-point complex int16 format.

        This helper performs the necessary quantization for the FPGA, ensuring signal integrity via:
        1. **Scaling**: Maps the normalized input range [-1.0, 1.0] to the full dynamic range defined by ``sample_bits``.
        2. **Symmetric Rounding**: Uses standard rounding to the nearest integer to minimize quantization noise.
        3. **Hard Clipping**: Enforces explicit saturation limits to prevent arithmetic overflow.

        :param samples_iq: List of [I, Q] floating-point pairs.
        :type samples_iq: List[List[float]]
        :param sample_bits: The resolution (bit depth) of the target DAC or memory (e.g., 16).
        :type sample_bits: int
        :return: A numpy array of complex16 numbers ready for hardware upload.
        :rtype: np.ndarray
        """

        vmax = (1 << (sample_bits - 1)) - 1
        a = np.asarray(samples_iq, dtype=np.float64)
        i = np.clip(np.rint(a[:, 0] * vmax), -vmax - 1, vmax).astype(np.int16)
        q = np.clip(np.rint(a[:, 1] * vmax), -vmax - 1, vmax).astype(np.int16)
        return i + 1j * q

    def _compute_max_hw_shots(
        self,
        mode: str,
        samp_per_shot: int,
        adc_index: int,
    ) -> int:
        """
        Compute the maximum number of shots executable in a single hardware run.

        This method determines the safe upper bound by calculating the intersection
        of two hardware constraints:
        1. The Trigger Generator's repetition counter limit (10-bit register = 1024 shots).
        2. The available DMA buffer capacity for the specified acquisition mode and length.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated').
        :type mode: str
        :param samp_per_shot: Number of samples per individual shot.
        :type samp_per_shot: int
        :param adc_index: Index of the target ADC.
        :type adc_index: int
        :return: The maximum allowable shots for a single atomic execution.
        :rtype: int
        """
        TRIGGER_MAX_SHOTS = 1024  # 10-bit register limit
        buffer_max = self.dma_engine.get_max_shots(mode, samp_per_shot, adc_index)
        return min(TRIGGER_MAX_SHOTS, buffer_max)
    
    # ------------------ GENERATOR IP MACROS ---------------------
    # ------------------------------------------------------------
    # Macro command G0: helpers for cache
    # ------------------------------------------------------------
    
    def get_wave_cache(self, gen_index: int) -> Dict[str, WaveEntry]:
        """
        Retrieve the High-Level wave cache for a specific generator.

        This method employs lazy initialization: if the cache for the requested
        generator does not exist, an empty dictionary is created, stored, and returned.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: A dictionary mapping wave IDs to their corresponding WaveEntry objects.
        :rtype: Dict[str, WaveEntry]
        """
        cache = self._wave_store.get(gen_index)
        if cache is None:
            cache = {}
            self._wave_store[gen_index] = cache
        return cache
    
    def get_envelope_names(self, gen_index: int) -> List[str]:
        """
        Retrieve the list of envelope names currently stored in the generator's memory.

        This method provides read-only inspection of the Low-Level (LL) driver state,
        specifically querying the keys present in the internal "EnvelopeMemoryDict".

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: A list of envelope names available in the hardware driver.
        :rtype: List[str]
        """
        gen = self._get_gen(gen_index)
        return list(getattr(gen, "envelope_memory_dict", {}).keys())
    
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
        Upload multiple envelopes into generator envelope memory with per-envelope
        validation and partial failure isolation.

        Design choices
        --------------
        - Each envelope is validated independently.
        - Failures do not abort the entire batch.
        - Successfully uploaded envelopes are committed immediately.
        - Skipped envelopes are explicitly reported.

        Safety checks
        -------------
        - Reserved name protection.
        - Interpolation/symmetry consistency enforcement.
        - Optional zero-padding to satisfy hardware parallelism.

        The returned structure is intentionally "JSON-friendly" to allow
        direct logging and remote inspection.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param envelopes: List of envelope specifications to upload.
        :type envelopes: List[EnvelopeSpec]
        :param auto_pad_noninterp: If True, automatically zero-pads non-interpolated envelopes to match hardware parallelism.
        :type auto_pad_noninterp: bool
        :return: A summary dictionary containing lists of loaded, skipped, and failed envelope names.
        :rtype: dict
        """

        self.logger.info("upload_envelopes: gen=%d, n=%d, auto_pad_noninterp=%s",
                 gen_index, len(envelopes), auto_pad_noninterp)
                 
       
        gen = self._get_gen(gen_index)
        loaded: List[str] = []
        skipped: List[str] = []
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

                env = self._iq_float_to_cint16(samples_iq, int(gen.sample_size))
                # Non-interp: automatic zero-padding on demand to allow non-interpolated
                # envelopes upload.
                if auto_pad_noninterp and (not for_interp):
                    par = int(gen.number_of_channels)
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
        return {"gen_index": int(gen_index), "loaded": loaded, "skipped": skipped, "failed": failed}

    # ------------------------------------------------------------
    # Macro command G2: compile_waves
    # ------------------------------------------------------------
    def compile_waves(self, *, gen_index: int, waves: List[dict], replace: bool) -> dict:
        """
        Compile high-level wave definitions into hardware Wave Definition Words (WDW).

        Handles 'env' (Envelope) and 'vz' (Virtual-Z) wave types. Supports caching to skip
        re-compilation of identical specifications.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param waves: List of dictionaries defining the waves (must contain 'wave_id', 'kind', etc.).
        :type waves: List[dict]
        :param replace: If True, allows overwriting existing wave definitions with different specs.
        :type replace: bool
        :return: A summary dictionary detailing compiled, replaced, skipped, and failed waves.
        :rtype: dict
        """
        self.logger.info("compile_waves: gen=%d n=%d", gen_index, len(waves))
        self.logger.debug("compile_waves: waves=%s", waves)
        
        # each wave_id is handled independently, but HL–LL consistency is
        # enforced strictly to avoid latent corruption.

        gen = self._get_gen(gen_index)
        out, replaced, skipped, failed = [], [], [], []
        cache: Dict[str, WaveEntry] = self.get_wave_cache(gen_index)

        for w in waves:
            try:
                # check the wave type: raise an exception the first type an error is encountered
                # in case of misdefinition, the wave is default to "env" type: in case of inconsistency, any
                # exception will be captured and managed accordingly

                kind = str(w.get("kind", "env")).lower()
                if kind not in ("env", "vz"):
                    raise ConfigurationError(f"Unkknown wave kind '{kind}' (use 'env' or 'vz').")
                
                wave_id = str(w["wave_id"])

                # -----------------------------
                # Build the new WaveEntry by type
                # -----------------------------
                if kind == "env":    
                    new_entry = WaveEntry(
                        envelope= str(w["envelope"]),
                        duration=int(w["duration"]),
                        gain=float(w["gain"]),
                        switch_iq=bool(w.get("switch_iq", False)),
                        keep_last=bool(w.get("keep_last", False)),
                        wdw=None,
                    )
                else:
                    # VZ: envelope/duration/gain are meaningless -> don't require them
                    # VZ is only a "phase-preparation"
                    if "vz_phase_rad" not in w:
                        raise ConfigurationError(
                            f"VZ wave '{wave_id}' missing vz_phase_rad. "
                            f"Hint: provide vz_phase_rad (radians)."
                        )
                    phase = float(w["vz_phase_rad"])
                    new_entry = WaveEntry(
                        kind="vz",
                        envelope="",      # placeholder, unused for VZ
                        duration=0,       # placeholder, unused for VZ
                        gain=0.0,         # placeholder, unused for VZ
                        switch_iq=False,
                        keep_last=False,
                        vz_phase_rad=phase,
                        wdw=None,
                    )

                
                # Elder definition with the same wave_id is exctracted, if any
                # Wave_id must be unique: therefore, new_entry with same wave_id can be either
                # a. skipped, if new and old have the same spec -> optimized compilation
                # b. discarded + error raise, if new and old have the same spec but replace = False [unauthorized replacement]
                # c. substituted, if new and old have not the same spec AND replace = True     
                old_entry = cache.get(wave_id)
                in_hw = (wave_id in gen.wave_memory_dict)

                # Skip path:
                # allowed ONLY when:
                # - specs are semantically identical,
                # - the wave is already present in hardware,
                # - the cached WDW is valid.
                #
                # This avoids unnecessary WDW regeneration while preserving
                # exact reproducibility.

                # ---  SKIP EARLY (no WDW computation)
                if old_entry is not None and _same_spec(old_entry, new_entry) and in_hw and (old_entry.wdw is not None):
                    
                    # 1. old_entry is not None: ensure this is not the first run/ run after a hard reset (reset_envelopes / reset_wave_memory with preserve_specs = False)
                    # 2. _same_spec(old_entry, new_entry): waves are the same functionally
                    # 3. in_hw: old_entry == new_entry was actually compiled
                    # 4. old_entry.wdw is not None: ensure the wdw is not deprecated. For example, after a reset_wave_memory with preserve_specs = True. 
                    #    In that case, you preserve waves characteristics but you need to recompile the whole cache
                    
                    skipped.append(wave_id)
                    new_entry.wdw = old_entry.wdw
                    cache[wave_id] = new_entry 
                    out.append({"wave_id": wave_id, "WDW": hex(new_entry.wdw)})
                    self.logger.debug("compile_waves: wave_id '%s' already present (same spec) -> skipped", wave_id)
                    continue

                # --- Replacement
                if old_entry is not None and not _same_spec(old_entry, new_entry) and not replace:                  
                    # stop the execution: replacement not allowed by the user
                    raise ConfigurationError(
                        f"wave_id '{wave_id}' already exists but spec differs. "
                        f"OLD={old_entry} NEW={new_entry}. "
                        f"Hint: set replace=True or use a different wave_id."
                    )
                
                # HL–LL desynchronization guard:
                # a wave existing in hardware but not in HL cache indicates
                # an unsafe state unless explicitly acknowledged by replace=True.
                if old_entry is None and in_hw and not replace:
                    raise ConfigurationError(
                        f"wave_id '{wave_id}' exists in HW but not in HL cache. "
                        f"Hint: set replace=True to re-sync or rebuild HL cache."
                    )

                # -----------------------------
                # WDW generation by kind
                # -----------------------------
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

                    #replace: either spec changed or synch HL-LL cache
                    self._call(
                        gen.replace_wave_in_wave_memory(wdw, wave_id, wave_id),
                        operation="replace_wave_in_wave_memory",
                        driver_name="GeneratorDriver",
                        config_error=True,
                    )
                    replaced.append(wave_id)
                
                
                else:
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
        """
        Compile and upload a specific wave configuration for the readout operations.
        
        REMARK: the readout wave is managed differntly than drive, thus only one readout wave is currently
        supported.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param wave: Dictionary containing the wave specification (envelope, duration, gain, etc.).
        :type wave: dict
        :param replace: If True, allows overwriting an existing readout configuration.
        :type replace: bool
        :return: A dictionary summarizing the upload status and compiled WDW.
        :rtype: dict
        """
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
        Program the generator FIFO with a sequence of wave_ids.

        This method enforces strict preconditions before facing hardware:
        - FIFO capacity bounds,
        - HL cache presence and compilation state,
        - LL driver consistency.

        Partial FIFO patching
        ---------------------
        When "start_index > 1", this method behaves as a "patch" operation.
        It refuses to proceed unless the existing HL FIFO cache is complete
        enough to guarantee correctness.

        This conservative approach is meant to prevent silent FIFO corruption.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param wave_id_list: Ordered list of wave IDs to execute.
        :type wave_id_list: List[str]
        :param start_index: FIFO index to start writing at (default 1). Higher indices imply patching.
        :type start_index: int
        :return: A dictionary containing the updated FIFO sequence.
        :rtype: dict
        :raises ConfigurationError: If FIFO overflow occurs, wave IDs are missing from cache/hardware, or patching is attempted on an incomplete cache.
        """

        self.logger.info("program_drive_sequence: gen=%d n=%d", gen_index, len(wave_id_list))
        self.logger.debug("program_drive_sequence: wave_id_list=%s", wave_id_list)

        
        gen = self._get_gen(gen_index)
        cache = self.get_wave_cache(gen_index)
        start_index = int(start_index)
        if start_index < 1:
            raise ConfigurationError(f"program_drive_sequence: start_index must be >= 1, got {start_index}")

        # capacity check (avoid cryptic -3 later)
        max_entries = int(gen.memory_mapped_fifo_segment_depth // 4)
        end_index = start_index + len(wave_id_list) - 1
        if end_index > max_entries:
            raise ConfigurationError(
                f"program_drive_sequence: overflow: end_index={end_index} > max_entries={max_entries}"
            )
        # Pre-check: avoid wrong FIFO listing 
        # Check 1: High Level Cache [wave_id <--> stored & compiled wdw]
        missing_wave_id_HL = [wid for wid in wave_id_list if (wid not in cache) or (cache[wid].wdw) is None]
        # Check 2: Low Level Cache [wave_id <--> Low Level]
        missing_wave_id_LL = [wid for wid in wave_id_list if wid not in gen.wave_memory_dict]
        
        if missing_wave_id_HL:
            raise ConfigurationError(f"program_drive_sequence: wave_id not in HL cache: {missing_wave_id_HL}")
        if missing_wave_id_LL:
            raise ConfigurationError(f"program_drive_sequence: wave_id was never compiled (LL): {missing_wave_id_LL}")
        
        # set the driver source as FIFO
        self.set_drive_source(gen_index= gen_index, source = "fifo")

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
        Reset the generator wave memory and synchronize the High-Level cache.
        
        Features
        -----------------
        preserve_specs:
            - True  -> keep WaveEntry specs but invalidate compiled WDW (set entry.wdw=None)
            - False -> clear HL wave cache entirely for this generator
            - Design motivation: speedup reconfigurations after reset operations.

        clear_last_fifo:
            - True  -> forget last programmed FIFO sequence in HL cache (recommended)
            - Design motivation: future deployment, it can be useful to cache the experiment wave-sequence FIFO
              (e.g. redundancies and repetitions in Quantum Algorithm and Subroutines, repeating the same sequences in
               sweep experiments ...)

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param preserve_specs: If True, keeps WaveEntry objects in cache but invalidates their WDWs.
        :type preserve_specs: bool
        :param clear_last_fifo: If True, clears the record of the last programmed FIFO sequence.
        :type clear_last_fifo: bool
        :return: A summary of the cache state after reset.
        :rtype: dict
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
        Reset the generator envelope memory and synchronize the High-Level wave cache.

        Since compiled waves depend on specific envelopes, clearing the envelope memory
        requires invalidating the compiled Wave Definition Words (WDW).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param preserve_wave_specs: If True, retains the high-level WaveEntry specifications but invalidates their compiled WDWs (requires recompilation). If False, clears the wave cache entirely.
        :type preserve_wave_specs: bool
        :param clear_last_fifo: If True, clears the record of the last programmed experiment sequence. If False, keeps it (useful only if envelopes are re-uploaded identically).
        :type clear_last_fifo: bool
        :return: A summary of the actions taken on the cache.
        :rtype: dict
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
    def generator_modulation(self, gen_index: int, label: str, gen_mod: Modulation):
        """
        Configure the Direct Digital Synthesis (DDS) modulation parameters for a specific generator.

        This method handles both the digital frequency synthesis configuration and the
        analog-domain Mix-Mode settings (Nyquist zone selection) based on the target frequency.

        :param gen_index: The index of the target generator.
        :type gen_index: int
        :param label: The modulation context, must be either 'drive' (control) or 'readout' (measurement).
        :type label: str
        :param gen_mod: A dictionary containing the modulation parameters (frequency in MHz, phase in degrees).
        :type gen_mod: Modulation
        :return: A summary of the applied modulation configuration.
        :rtype: dict
        :raises ConfigurationError: If the ``label`` is not 'drive' or 'readout'.
        """
        
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
                    operation="set_drive_dds_parameters",
                    driver_name="GeneratorDriver",
                    config_error=True
                )

            else:
                self._call(
                    gen.set_readout_dds_parameters(frequency=gen_mod["frequency_mhz"], phase= gen_mod["phase"], dac_samplerate= self._dac_sr_mhz()),
                    operation="set_readout_dds_parameters",
                    driver_name="GeneratorDriver",
                    config_error=True
                )
        else:
            raise ConfigurationError("Invalid mode selection!\nHint: select label =  'drive' or 'readout' ")
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
    def gen_trigger2listen(self, gen_index, trig: TriggerCommand):
        """
        Configure which trigger channel the generator should listen to.

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param trig: Dictionary defining the trigger type and source channel.
        :type trig: TriggerCommand
        :return: The applied trigger configuration.
        :rtype: dict
        """
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
    
        if trig["channel"] == 0:
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

    def set_drive_source(
        self,
        *,
        gen_index: int,
        source: Literal["fifo", "lfsr"],
        seed: Optional[int] = None,
    ) -> dict:
        """
        Select the source for the drive wave sequence.

        If source="lfsr" and seed is provided, the LFSR seed is programmed before enabling LFSR.
        If source="fifo", the seed parameter is ignored.

        :param gen_index: Index of the generator.
        :type gen_index: int
        :param source: Selection between "fifo" (programmed sequence) or "lfsr" (pseudo-random).
        :type source: Literal["fifo", "lfsr"]
        :param seed: Optional LFSR seed value. Used only when source="lfsr".
        :type seed: Optional[int]
        :return: Selected source status (and seed, if applied).
        :rtype: dict
        """
        self.logger.info("set_drive_source: gen=%d source=%s seed=%s", gen_index, source, seed)

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

        out = {"gen_index": int(gen_index), "source": source_lower}
        if source_lower == "lfsr" and seed is not None:
            out["seed"] = int(seed)
        return out

    # -------------- Trigger GENERATOR IP MACROS -----------------
    # ------------------------------------------------------------
    # Macro command TG1: Program delay channels
    # ------------------------------------------------------------
    
    def tg_set_shots(self, shots: int) -> dict:
        """
        Set the number of hardware repetitions (shots) for the trigger generator.

        :param shots: Number of repetitions (must be within hardware limits).
        :type shots: int
        :return: Dictionary containing the set number of shots.
        :rtype: dict
        """
        self.logger.info("Setting up %d shots", shots)
        t = self._get_trig()
        shots_i = int(shots)

        if shots_i < 1 or shots_i > int(t.max_hw_repetitions):
            raise ConfigurationError(
                f"shots={shots_i} out of range [1..{int(t.max_hw_repetitions)}]"
            )

        self.logger.info("Success!")
        t.set_number_of_shots(shots_i)
        return {"shots": shots_i}
    
    def tg_set_duration(self, duration_cycles: int) -> dict:
        """
        Set the total duration of the experiment in clock cycles.

        This parameter defines the repetition period of the global trigger sequence.

        :param duration_cycles: The duration of the experiment in FPGA clock cycles.
        :type duration_cycles: int
        :return: A dictionary containing the configured experiment duration.
        :rtype: dict
        :raises ConfigurationError: If duration_cycles is less than 1.
        """
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
            drive: Optional[dict] = None,
            readout: Optional[dict] = None,
            drive_start_index: int = 1,
        ) -> dict:
        """
        Program the timing delays for drive and readout triggers.
        For each programmed drive channel, entries from ``drive_start_index + len(entries)`` to the FIFO end are cleared.
        This means partial patching does not preserve any existing tail.

        :param drive: Dictionary mapping channel indices to lists of (delay, value) pairs.
        :type drive: Optional[dict]
        :param readout: Dictionary mapping channel indices to readout delay specifications.
        :type readout: Optional[dict]
        :param drive_start_index: FIFO index to start writing drive delays (default 1). Higher indices imply patching.
        :type drive_start_index: int
        :return: Report of programmed readout channels and drive sequences.
        :rtype: dict
        """
        self.logger.info("Setting experiment delays in the Trigger Generator")
        self.logger.debug(
            "---Experiment delay details--- \n1. drive_start_index = %d \n2.drive_delays = %s \n3.readout_delays= %s",
            drive_start_index, drive, readout
        )
        t = self._get_trig()
        drive = drive or {}
        readout = readout or {}

        start_idx = int(drive_start_index)
        if start_idx < 1 or start_idx > int(t.channel_fifo_depth):
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
            max_writable = int(t.channel_fifo_depth) - (start_idx - 1)
            if len(entries_list) > max_writable:
                raise ConfigurationError(
                    f"drive[{ch}] too long for start_index={start_idx}: "
                    f"{len(entries_list)} > {max_writable}"
                )

            # program the requested block (patching supported via start_idx)
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

            # Clear the tail after the written block (relative to start_idx) to avoid stale entries.
            first_tail = start_idx + len(entries_list)
            for fifo_index in range(first_tail, int(t.channel_fifo_depth) + 1):
                self._call(
                    t.insert_drive_delay(ch, fifo_index, int(t.drive_delay_max), 0),
                    operation="insert_drive_delay",
                    driver_name="TriggerGeneratorDriver",
                    config_error=True,
                )

            drive_report[ch] = {
                "start_index": start_idx,
                "n_entries": len(entries_list),
                "padded": 0,
            }

        return {
            "readout_channels_programmed": sorted(ro_programmed),
            "drive_programmed": drive_report,
        }


    def trigger_experiment(self) -> None:
        """
        Trigger the experiment.

        """
        trigger = self._get_trig()
        self.logger.debug("Triggering experiment...")
        trigger.start_experiment()

    # -------------- Acquisition IP MACROS -----------------
    # ------------------------------------------------------------
    # Macro command A1: Acquisition IP setup
    # ------------------------------------------------------------
    
    def acquisition_modulation(self, acq_index: int, acq_mod: Modulation):
        """
        Configure the DDS modulation parameters for an acquisition unit.

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param acq_mod: Dictionary containing frequency and phase parameters.
        :type acq_mod: Modulation
        :return: The applied configuration.
        :rtype: dict
        """
        self.logger.info(
            "acquisition_modulation: acq=%d frequency=%s phase =%s ",
            acq_index, acq_mod["frequency_mhz"], acq_mod["phase"]
        )
        acq = self._get_acq(acq_index)

        # Configure Mix-Mode via overlay (low-level handles tile/block mapping)
        try:
            mix_info = self.ol.configure_adc_mix_mode(acq_index=acq_index, freq_mhz=acq_mod["frequency_mhz"])
            if mix_info.get("changed"):
                self.logger.debug(
                    "ADC Mix-mode updated: Zone %d (AMD=%d) on tile=%d block=%d",
                    mix_info["nyquist_zone"], mix_info["amd_zone"],
                    mix_info["tile"], mix_info["block"]
                )
        except ValueError as e:
            self.logger.warning(f"ADC Mix-mode config skipped: {e}")
        
        # Note:
        # The higher-level handlers default missing ``phase`` to ``0.0``.
        # Direct callers should provide a numeric phase value.
        self._call(
                acq.set_acquisition_dds_parameters(frequency= acq_mod["frequency_mhz"] , phase= acq_mod["phase"], adc_samplerate= self._adc_sr_mhz()),
                operation="set_acquisition_dds_parameters",
                driver_name="AcquisitionDriver",
                config_error=True
            )
        self.logger.info("acquisition_parameters: done acq=%d", acq_index)
        return {
            "acq_index": acq_index,
            "frequency_mhz": acq_mod["frequency_mhz"],
            "phase": acq_mod["phase"],
        }

    def acquisition_timing(self, acq_index, tof: int, duration: int):
        """
        Configure the timing parameters (Time of Flight and Duration) for an acquisition unit.

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param tof: Time of Flight delay in clock cycles.
        :type tof: int
        :param duration: Acquisition duration in clock cycles.
        :type duration: int
        :return: The applied timing configuration.
        :rtype: dict
        """
        self.logger.info(
            "acquisition_timing: acq_index=%d tof = %d",
            acq_index, tof
        )
        acq = self._get_acq(acq_index)
        
        self._call(
                acq.set_acquisition_duration(duration), # Clock Cycles
                operation="set_acquisition_duration",
                driver_name="AcquisitionDriver",
                config_error=True
            )
        
        self._call(
                acq.set_time_of_flight(tof),
                operation= "set_time_of_flight",
                driver_name= "AcquisitionDriver",
                config_error= True
            )
        self.logger.info("Acquisition timing set up!")
        return {
                "acq_index": acq_index,
                "tof": tof,
                "duration": duration,
            }

    def acq_trigger2listen(self, acq_index, trig: TriggerCommand):
        self.logger.info(
            "acq_trigger2listen: acq=%d channel=%s",
            acq_index, trig["channel"]
        )
        acq = self._get_acq(acq_index)
        
        self._call(
            acq.set_trigger_channel(channel= trig["channel"]),
            operation="set_trigger_channel",
            driver_name="AcquisitionDriver",
            config_error=True
        )
    
        if trig["channel"] == 0:
            self.logger.info("Acquisition %d is deaf to any trigger!", acq_index)
        else:
            self.logger.info("Generator %d listens to %s_trigger_word channel %d", acq_index, trig["ttype"], trig["channel"] )
        
        
        return {
            "acq_index": acq_index,
            "channel":trig["channel"],
        }

    # ------------------------------------------------------------
    # DMA & Experiments management macros
    # ------------------------------------------------------------

    def run_multi_acquisition(
        self,
        *,
        adc_indices: List[int],
        mode: Literal["raw", "decimated", "accumulated"],
        shots: int,
        samp_per_shot: int,
        timeout: Optional[float] = 1.0
    ) -> Dict[int, np.ndarray]:
        """
        Execute a multi-ADC acquisition with automatic hardware chunking.

        If the requested number of shots exceeds hardware repetition capacity (maximum
        number of hardware shots), the acquisition is transparently split into multiple 
        hardware runs and reassembled in software.

        Performance instrumentation
        ----------------------------
        This method explicitly measures:
        - FPGA active time (proxied by DMA wait time),
        - software overhead (everything else in the host process).

        This separation is intentional to support performance analysis
        and experimental reproducibility studies.
        """

        # Start total routine timer for performance analysis
        t_start_routine = time.perf_counter()
        hw_time_accumulator = 0.0

        # --- Input Validation ---
        if not adc_indices:
            raise ConfigurationError("No ADC indices provided.")
        if len(adc_indices) > len(self.hw_specs["acquisitions"]):
            raise ConfigurationError(
                f"Requested {len(adc_indices)} ADCs, but only {len(self.hw_specs['acquisitions'])} available."
            )
        
        # Compute hardware buffer limits
        max_hw_shots = min(self._compute_max_hw_shots(mode, samp_per_shot, adc) 
                           for adc in adc_indices)
        if max_hw_shots < 1:
            raise ConfigurationError(
                f"Impossible configuration: The requested single shot duration ({samp_per_shot} samples) "
                f"is larger than the entire hardware buffer available for mode '{mode}'. "
                "Try reducing the acquisition duration."
            )
    
        # Variable to store the consolidated result from either path
        final_result: Dict[int, np.ndarray] = {}

        # --- Case 1: Single Hardware Acquisition ---
        # The requested shots fit into a single hardware buffer execution.
        if shots <= max_hw_shots:
            
            final_result, hw_wait_s = self._run_single_hw_acquisition(
                adc_indices=adc_indices,
                mode=mode,
                shots=shots,
                samp_per_shot=samp_per_shot,
                timeout=timeout
            )
            hw_time_accumulator += hw_wait_s

        # --- Case 2: Multiple Hardware Acquisitions (Chunking) ---
        # The requested shots exceed the hardware buffer; split into chunks.
        else:
            self.logger.info(f"Splitting {shots} shots into chunks of {max_hw_shots}")
            results = {adc: [] for adc in adc_indices}
            remaining = shots
            
            while remaining > 0:
                hw_shots = min(max_hw_shots, remaining)

                data, hw_wait_s = self._run_single_hw_acquisition(
                    adc_indices=adc_indices,
                    mode=mode,
                    shots=hw_shots,
                    samp_per_shot=samp_per_shot,
                    timeout=timeout
                )
                hw_time_accumulator += hw_wait_s
                
                for adc in adc_indices:
                    results[adc].append(data[adc])
                
                remaining -= hw_shots
            
            # Concatenate chunks along the shot axis to form the final array
            final_result = {adc: np.concatenate(results[adc], axis=0) for adc in adc_indices}

        # --- Performance Calculation ---
        # Calculate total duration and isolate software overhead
        t_end_routine = time.perf_counter()
        total_duration = t_end_routine - t_start_routine
        sw_overhead = total_duration - hw_time_accumulator

        # Update statistics for telemetry
        self.last_timing_stats = {
            "sw_overhead_ms": sw_overhead * 1000.0,
            "fpga_active_ms": hw_time_accumulator * 1000.0
        }

        return final_result

    def _run_single_hw_acquisition(
        self,
        *,
        adc_indices: List[int],
        mode: Literal["raw", "decimated", "accumulated"],
        shots: int,
        samp_per_shot: int,
        timeout: Optional[float] = 1.0
    ) -> Tuple[Dict[int, np.ndarray], float]:
        """
        Execute a single hardware acquisition cycle.

        This method assumes that:
        - trigger configuration is already valid,
        - acquisition IPs are correctly pre-configured,
        - DMA engine is available and healthy.

        It represents the minimal atomic execution unit used by
        higher-level chunking logic.

        :param adc_indices: List of ADC indices.
        :type adc_indices: List[int]
        :param mode: Acquisition mode.
        :type mode: Literal["raw", "decimated", "accumulated"]
        :param shots: Number of shots for this specific hardware run.
        :type shots: int
        :param samp_per_shot: Samples per shot.
        :type samp_per_shot: int
        :param timeout: Timeout in seconds.
        :type timeout: Optional[float]
        :return: (acquired data for this chunk, DMA wait time in seconds).
        :rtype: Tuple[Dict[int, np.ndarray], float]
        """

        results = {}
        hw_wait_s = 0.0
        self.tg_set_shots(shots)

        # Pre-config ADCs
        for adc_i in adc_indices:
            acq = self._get_acq(adc_i)
            if mode in ("decimated", "accumulated"):
                if not self._sweep_prepared:
                    acq.set_decimated_output_type(mode)
            elif mode == "raw":
                ctrl = acq.AxiLiteInterfaceMMIO.read(acq.ctrl * 4)
                acq.AxiLiteInterfaceMMIO.write(acq.ctrl * 4, ctrl)
        
        # First ADC: arm before trigger
        first_adc = adc_indices[0]
        first_buffer = self.dma_engine.arm_acquisition(
            samp_per_shot=samp_per_shot,
            shots_per_exp=shots,
            mode=mode,
            adc_index=first_adc
        )
        
        # Trigger
        self.trigger_experiment()
        
        # Retrieve first ADC
        results[first_adc] = self.dma_engine.retrieve_acquisition(
            buffer=first_buffer,
            mode=mode,
            shots=shots,
            samp_per_shot=samp_per_shot,
            adc_index=first_adc,
            timeout=timeout
        )
        hw_wait_s += self.dma_engine.last_dma_wait_s
        
        # Remaining ADCs
        for adc_i in adc_indices[1:]:
            buffer = self.dma_engine.arm_acquisition(
                samp_per_shot=samp_per_shot,
                shots_per_exp=shots,
                mode=mode,
                adc_index=adc_i
            )
            results[adc_i] = self.dma_engine.retrieve_acquisition(
                buffer=buffer,
                mode=mode,
                shots=shots,
                samp_per_shot=samp_per_shot,
                adc_index=adc_i,
                timeout=timeout
            )
            hw_wait_s += self.dma_engine.last_dma_wait_s
        
        return results, hw_wait_s

    def prepare_sweep(self, mode: str, adc_indices: List[int]) -> None:
        """
        Prepare acquisition IPs and DMA engine for sweep-optimized execution.

        This configuration locks the acquisition hardware into the specified mode
        to guarantee invariant behavior across the sweep duration.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :type mode: str
        :param adc_indices: List of active ADC indices involved in the sweep.
        :type adc_indices: List[int]
        """

        # Pre-config acquisition IPs
        for adc_i in adc_indices:
            acq = self._get_acq(adc_i)
            if mode in ("decimated", "accumulated"):
                acq.set_decimated_output_type(mode)
        
        # Prepare DMA engine
        self.dma_engine.prepare_sweep(mode)
        self._sweep_prepared = True

    def end_sweep(self) -> None:
        """
        Finalize the sweep execution and release DMA engine resources.

        This method must be called at the end of a sweep sequence to ensure
        the DMA engine correctly exits the optimized state.
        """

        self.dma_engine.end_sweep()
        self._sweep_prepared = False
