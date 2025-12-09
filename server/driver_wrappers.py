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
from typing import Any, Dict, List, Optional
import numpy as np

from FIREQ_LL_API.generator_driver import GeneratorDriver
from .exceptions import DriverError, ConfigurationError


# Driver error codes (from GeneratorDriver documentation)
_ERR_ENVELOPE_NOT_FOUND = -1
_ERR_MEMORY_NOT_INITIALIZED = -2
_ERR_INVALID_PARAMS = -3
_ERR_MEMORY_FULL = -4


def _handle_driver_result(
    result: Any,
    *,  # Force keyword arguments for clarity
    operation: str,
    driver_name: str,
    logger: logging.Logger,
    config_error: bool = False,
    hint: Optional[str] = None,
) -> None:
    """
    Handle driver return codes and raise appropriate exceptions.

    Parameters
    ----------
    result : Any
        The return value from the driver call.
    operation : str
        Name of the driver operation (for error messages).
    driver_name : str
        Name of the driver class (for error messages).
    logger : logging.Logger
        Logger instance for error logging.
    config_error : bool, optional
        If True, raise ConfigurationError instead of DriverError.
    hint : str, optional
        Custom error message. If None, a generic message is generated.

    Raises
    ------
    ConfigurationError
        If result is a negative int and config_error=True.
    DriverError
        If result is a negative int and config_error=False.
    """
    if isinstance(result, int) and result < 0:
        message = hint or f"{driver_name}.{operation} failed with code {result}"
        logger.error(message)

        if config_error:
            raise ConfigurationError(message)

        raise DriverError(
            message,
            driver_name=driver_name,
            operation=operation,
            return_code=result,
        )


class GeneratorAdapter:
    #TODO: data-converter handling, experiment FIFO setup, randomized banchmarking setup
    """
    Server-oriented adapter for the GeneratorDriver.

    This adapter manages:
      - Envelope uploads from parsed JSON payloads
      - Wave compilation (mapping wave_id -> hardware WDW)
      - Runtime wave_id -> WDW resolution during experiment execution

    Parameters
    ----------
    driver : GeneratorDriver
        The low-level driver instance to wrap.
    gen_index : int
        Index of the generator in the system (for logging/identification).
    logger : logging.Logger, optional
        Logger instance. If None, a module-level logger is created.

    Attributes
    ----------
    gen_index : int
        The generator index.
    logger : logging.Logger
        The logger instance.
    """

    def __init__(
        self,
        driver: GeneratorDriver,
        gen_index: int,
        logger: Optional[logging.Logger] = None,
    ):
        self._drv: GeneratorDriver = driver
        self.gen_index: int = gen_index
        self.logger = logger or logging.getLogger(__name__)

        # High-level cache: envelopes and waves
        # Cache of uploaded envelopes keyed by name 
        self._envelopes: Dict[str, Dict[str, Any]] = {}
        # Cache of compiled waves keyed by client id 
        self._wave_by_id: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Envelope upload
    # ------------------------------------------------------------------
    def upload_envelopes(self, envelopes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Upload a list of envelopes to the associated Generator.

        Parameters
        ----------
        envelopes : list of dict
            List of envelope specifications, each containing:
            - name : str
                Unique envelope identifier.
            - for_interpolation : bool, optional
                Enable interpolation mode (default: False).
            - is_symmetric : bool, optional
                Envelope is symmetric (default: False).
            - i_even : bool, optional
                I component has even symmetry (default: False).
            - q_even : bool, optional
                Q component has even symmetry (default: False).
            - samples_iq : list of [I, Q] pairs
                Complex envelope samples as Nx2 array.

        Returns
        -------
        dict
            Result dictionary with structure::

                {
                    "generator": <gen_index>,
                    "loaded": ["GAUSS_X", ...],
                    "failed": [
                        {"name": "BAD_ENV", "reason": "..."}, ...
                    ]
                }

        """
        loaded: List[str] = []
        failed: List[Dict[str, str]] = []

        for idx, env in enumerate(envelopes):
            name = env.get("name")
            name_for_log = name if isinstance(name, str) and name else f"<env_{idx}>"

            try:
                # -------------------------
                # 1. Server-side validation
                # -------------------------
                if not isinstance(name, str) or not name:
                    raise ConfigurationError("Envelope name must be a non-empty string")

                if name in self._envelopes:
                    # Treat as error for now; no silent overwrite
                    raise ConfigurationError(
                        f"Envelope '{name}' is already registered in adapter"
                    )

                # Boolean flags (default to False if missing)
                for_interpolation = bool(env.get("for_interpolation", False))
                is_symmetric = bool(env.get("is_symmetric", False))
                i_even = bool(env.get("i_even", False))
                q_even = bool(env.get("q_even", False))

                # samples_iq: list of [I, Q] pairs
                if "samples_iq" not in env:
                    raise ConfigurationError("Missing 'samples_iq' field")

                samples_iq = np.asarray(env["samples_iq"], dtype=float)
                if samples_iq.ndim != 2 or samples_iq.shape[1] != 2:
                    raise ConfigurationError(
                        f"'samples_iq' for envelope '{name}' must be an Nx2 list/array"
                    )

                # Build complex array: I + jQ
                samples_complex = samples_iq[:, 0] + 1j * samples_iq[:, 1]
                # Ensure complex128 dtype to pass driver validation
                samples_complex = np.asarray(samples_complex, dtype=complex)

                if samples_complex.size < 2:
                    raise ConfigurationError("Envelope must contain at least 2 samples")

                # Warning for amplitude > 1.0 (driver will saturate to int16 range)
                max_abs = float(np.max(np.abs(samples_complex)))
                if max_abs > 1.0:
                    self.logger.warning(
                        "Envelope '%s' has max amplitude %.3f > 1.0; "
                        "it will be saturated to int16 range by the driver.",
                        name,
                        max_abs,
                    )

                # -------------------------
                # 2. Low-level driver call
                # -------------------------
                rc = self._drv.add_envelope_to_envelope_memory(
                    samples_complex,
                    for_interpolation=for_interpolation,
                    is_symmetric=is_symmetric,
                    i_even=i_even,
                    q_even=q_even,
                    envelope_name=name,
                )

                # Translate driver error codes
                if isinstance(rc, int) and rc < 0:
                    if rc == _ERR_INVALID_PARAMS:
                        hint = (
                            "Invalid envelope parameters (name in use, non-complex dtype, "
                            "length < 2, symmetry/parallelism constraints)."
                        )
                    elif rc == _ERR_MEMORY_FULL:
                        hint = "Not enough space in envelope memory for this envelope."
                    else:
                        hint = f"Driver returned error code {rc} while uploading envelope."
                    _handle_driver_result(
                        rc,
                        operation="add_envelope_to_envelope_memory",
                        driver_name="GeneratorDriver",
                        logger=self.logger,
                        config_error=False,
                        hint=hint,
                    )

                # -------------------------
                # 3. Update internal cache
                # -------------------------
                self._envelopes[name] = {
                    "num_samples": int(samples_complex.size),
                    "for_interpolation": for_interpolation,
                    "is_symmetric": is_symmetric,
                    "i_even": i_even,
                    "q_even": q_even,
                }

                loaded.append(name)
                self.logger.info(
                    "Envelope '%s' uploaded successfully on generator %d "
                    "(samples=%d, interp=%s, sym=%s)",
                    name,
                    self.gen_index,
                    samples_complex.size,
                    for_interpolation,
                    is_symmetric,
                )

            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                self.logger.error(
                    "Failed to upload envelope '%s' on generator %d: %s",
                    name_for_log,
                    self.gen_index,
                    msg,
                )
                failed.append({"name": name_for_log, "reason": msg})

        return {
            "generator": self.gen_index,
            "loaded": loaded,
            "failed": failed,
        }

    # ------------------------------------------------------------------
    # Wave compilation
    # ------------------------------------------------------------------
    def compile_waves(self, waves: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Compile a list of wave specifications into hardware WDWs.

        Parameters
        ----------
        waves : list of dict
            List of wave specifications, each containing:
            - wave_id : str
                Unique logical identifier (client-side key).
            - envelope : str
                Name of a previously uploaded envelope.
            - gain : float
                Amplitude scaling factor in range [-1, 1].
            - duration_samples : int
                Output duration (0 = natural envelope length, else 2..MaxDuration).
            - switch_iq : bool, optional
                Swap I and Q channels (default: False).
            - keep_last : bool, optional
                Hold last sample value after completion (default: False).
            - kind : str, optional
                Wave type: "drive" or "readout" (default: "drive").

        Returns
        -------
        dict
            Result dictionary with structure::

                {
                    "generator": <gen_index>,
                    "compiled": [
                        {"wave_id": "...", "status": "ok" | "already_compiled"},
                        ...
                    ],
                    "failed": [
                        {"wave_id": "...", "reason": "..."}
                    ]
                }
        """
        compiled: List[Dict[str, Any]] = []
        failed: List[Dict[str, str]] = []

        for spec in waves:
            wave_id = spec.get("wave_id")
            if not isinstance(wave_id, str) or not wave_id:
                failed.append(
                    {
                        "wave_id": str(wave_id),
                        "reason": "ConfigurationError: Invalid or empty wave_id",
                    }
                )
                continue

            # Skip if already compiled
            if wave_id in self._wave_by_id:
                compiled.append({"wave_id": wave_id, "status": "already_compiled"})
                continue

            envelope = spec.get("envelope")
            gain = spec.get("gain")
            duration = spec.get("duration_samples")
            switch_iq = bool(spec.get("switch_iq", False))
            keep_last = bool(spec.get("keep_last", False))
            kind = spec.get("kind", "drive")

            try:
                # --- Server-side validation ---
                if envelope not in self._envelopes:
                    raise ConfigurationError(
                        f"Envelope '{envelope}' not found on generator {self.gen_index}"
                    )

                if not isinstance(gain, (int, float)):
                    raise ConfigurationError("Gain must be a number")
                if gain < -1.0 or gain > 1.0:
                    raise ConfigurationError(f"Gain {gain} out of range [-1, 1]")

                if not isinstance(duration, int):
                    raise ConfigurationError("duration_samples must be an integer")

                max_dur = getattr(self._drv, "MaximumDuration", None)
                if max_dur is not None and duration != 0:
                    if duration < 2 or duration > max_dur:
                        raise ConfigurationError(
                            f"duration_samples={duration} out of range [2, {max_dur}] "
                            "(or 0 for natural size)"
                        )

                # --- Create hardware WDW via driver ---
                wdw = self._drv.create_wave_definition_word(
                    envelope_name=envelope,
                    duration=duration,
                    gain=gain,
                    switch_iq=switch_iq,
                    keep_last=keep_last,
                )

                if isinstance(wdw, int) and wdw < 0:
                    if wdw == _ERR_ENVELOPE_NOT_FOUND:
                        hint = "Envelope not found in generator envelope memory"
                    elif wdw == _ERR_MEMORY_NOT_INITIALIZED:
                        hint = "Envelope memory not initialized"
                    elif wdw == _ERR_INVALID_PARAMS:
                        hint = "Invalid parameters (gain/duration out of range)"
                    else:
                        hint = f"Driver error code {wdw}"
                    _handle_driver_result(
                        wdw,
                        operation="create_wave_definition_word",
                        driver_name="GeneratorDriver",
                        logger=self.logger,
                        config_error=False,
                        hint=hint,
                    )

                if not isinstance(wdw, int):
                    # Non-numeric return value -> direct DriverError
                    raise DriverError(
                        f"Driver returned non-int WDW: {wdw!r}",
                        driver_name="GeneratorDriver",
                        operation="create_wave_definition_word",
                    )

                # --- Update internal cache ---
                self._wave_by_id[wave_id] = {
                    "wdw": wdw,
                    "envelope": envelope,
                    "gain": float(gain),
                    "duration_samples": int(duration),
                    "switch_iq": switch_iq,
                    "keep_last": keep_last,
                    "kind": kind,
                }

                compiled.append({"wave_id": wave_id, "status": "ok"})

            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                self.logger.error(
                    "Compilation failed for wave_id '%s' on generator %d: %s",
                    wave_id,
                    self.gen_index,
                    msg,
                )
                failed.append({"wave_id": wave_id, "reason": msg})

        return {
            "generator": self.gen_index,
            "compiled": compiled,
            "failed": failed,
        }

    # ------------------------------------------------------------------
    # Runtime resolution
    # ------------------------------------------------------------------
    def resolve_wdw_for_run(self, spec: Dict[str, Any]) -> int:
        """
        Resolve a wave_id to its hardware WDW for experiment execution.

        Parameters
        ----------
        spec : dict
            Sequence entry specification containing:
            - wave_id : str
                The logical wave identifier to resolve.
            - generator : int, optional
                Generator index (for context).
            - channel : int, optional
                Channel index (for context).

        Returns
        -------
        int
            The hardware Wave Definition Word (WDW) for the specified wave.

        Raises
        ------
        ConfigurationError
            If 'wdw' field is present (raw WDWs not allowed in sequences),
            if 'wave_id' is missing, or if the wave_id is not found in cache.

        Notes
        -----
        This method enforces that all waves must be precompiled and referenced
        by wave_id only. Direct WDW specification in run sequences is forbidden
        to ensure proper validation and traceability.
        """
        if "wdw" in spec and spec["wdw"] is not None:
            # Forbid raw WDW specification for safety
            raise ConfigurationError(
                "Field 'wdw' is not allowed in run_experiment sequence: "
                "waves must be precompiled and referenced by 'wave_id' only."
            )

        wave_id = spec.get("wave_id")
        if not wave_id:
            raise ConfigurationError("Missing 'wave_id' in generator sequence entry")

        info = self._wave_by_id.get(wave_id)
        if info is None:
            raise ConfigurationError(
                f"Unknown wave_id '{wave_id}' for generator {self.gen_index} "
                f"(wave not compiled or cache lost)."
            )

        return info["wdw"]

#TODO : adapt TriggerAdapter and AcquistionAdapter Classes to the new server structure



__all__ = ['GeneratorAdapter']
#__all__ = ['GeneratorAdapter', 'AcquisitionAdapter', 'TriggerAdapter']