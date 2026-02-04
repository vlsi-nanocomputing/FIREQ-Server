"""High-level composition-based adapter for FIREQ hardware.

This module implements the OverlayAdapter using composition of operation classes
instead of mixins, providing a cleaner, more maintainable architecture.
"""

import logging
from collections.abc import Iterator
from typing import Any, Literal

from ...models.config_types import Modulation, TriggerCommand
from ...models.exceptions import HardwareStateError
from ..dma_engine import AcquisitionEngine
from .acquisition import AcquisitionOps
from .cache import CacheContainers
from .experiment import ExperimentOps
from .generator import GeneratorOps
from .ll_access import LowLevelAccess
from .trigger import TriggerOps
from .types import EnvelopeSpec


class OverlayAdapter:
    """High-level adapter for FIREQ hardware control using composition.

    This class composes four operation classes to provide a server-facing
    interface on top of the low-level FIREQ hardware drivers:
    - GeneratorOps: Wave management and generator modulation
    - TriggerOps: Trigger generator control
    - AcquisitionOps: DMA acquisition execution
    - ExperimentOps: High-level experiment orchestration

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

    Attributes
    ----------
    generator : GeneratorOps
        Wave and generator modulation operations.
    trigger : TriggerOps
        Trigger generator operations.
    acquisition : AcquisitionOps
        DMA acquisition operations.
    experiment : ExperimentOps
        High-level experiment orchestration.
    """

    def __init__(self, ol: object, *, logger: logging.Logger | None = None) -> None:
        """Initialize the High-Level Adapter using composition.

        :param ol: The low-level overlay driver instance.
        :type ol: fireq_soc
        :param logger: Optional logger instance for telemetry. If None, a default logger
            is created.
        :type logger: Optional[logging.Logger]
        """
        # Fail-fast sanity check:
        if not ol.is_healthy:
            raise HardwareStateError("Unexpected Error: overlay upload failed!")

        self.ol = ol
        self.logger = logger or logging.getLogger(__name__)

        # DMA engine (needed for acquisition)
        if self.ol.dma is None or self.ol.axis_switch is None:
            raise HardwareStateError("DMA or AXI-Stream switch missing in overlay")

        # The DMA engine is constructed once as a long-lived resource.
        self.dma_engine = AcquisitionEngine(
            self.ol.dma,
            self.ol.axis_switch,
            logger=self.logger,
            hw_specs=self.ol.hw_specs,
        )

        # Shared cache containers for all operation classes
        self._cache = CacheContainers()

        # Low-level driver access helper with error handling
        self._ll = LowLevelAccess(self.ol, self.logger)

        # Compose operation classes in dependency order
        self.trigger = TriggerOps(
            ll=self._ll,
            cache=self._cache,
            logger=self.logger,
        )

        self.generator = GeneratorOps(
            ll=self._ll,
            cache=self._cache,
            logger=self.logger,
        )

        self.acquisition = AcquisitionOps(
            ll=self._ll,
            cache=self._cache,
            logger=self.logger,
            dma_engine=self.dma_engine,
            trigger=self.trigger,
        )

        self.experiment = ExperimentOps(
            ll=self._ll,
            cache=self._cache,
            logger=self.logger,
            trigger=self.trigger,
            acquisition=self.acquisition,
        )

    # ========== Proxy Pattern for Backward Compatibility ==========

    def _call(self, obj: object, method_name: str, *args: Any, **kwargs: Any) -> int:  # noqa: ANN401
        """Unified error handling wrapper for low-level driver calls.

        This method delegates to the LowLevelAccess helper for consistent
        error translation and handling.

        :param obj: The hardware object to call the method on.
        :type obj: object
        :param method_name: The name of the method to call.
        :type method_name: str
        :param args: Positional arguments to pass to the method.
        :type args: tuple
        :param kwargs: Keyword arguments to pass to the method.
        :type kwargs: dict
        :return: The return code from the method call.
        :rtype: int
        """
        return self._ll.call(obj, method_name, *args, **kwargs)

    def __getattr__(self, name: str) -> object:
        """Delegate attribute access to the underlying low-level overlay driver.

        This method implements the Proxy pattern, allowing the adapter to transparently
        expose the full API of the wrapped ``fireq_soc`` instance. Any attribute or method
        not explicitly defined in this adapter is automatically forwarded to the hardware driver.

        Therefore, the "expert" user can directly use the underlying driver methods. The only purpose
        is to speedup debugging operation and ease developers' work.

        :param name: The name of the attribute to retrieve.
        :type name: str
        :return: The attribute value from the low-level driver.
        :rtype: object
        :raises AttributeError: If the attribute is not found in either the adapter or the underlying driver.
        """
        return getattr(self.ol, name)

    # ========== Public Properties for Backward Compatibility ==========

    @property
    def last_timing_stats(self) -> dict:
        """Retrieve the last timing statistics from an acquisition.

        :return: Dictionary with timing breakdown (total_ms, fpga_wait_ms, dma_overhead_ms, sw_overhead_ms).
        :rtype: dict
        """
        return self._cache.last_timing_stats

    @last_timing_stats.setter
    def last_timing_stats(self, value: dict) -> None:
        """Set timing statistics (for testing purposes).

        :param value: Timing statistics dictionary.
        :type value: dict
        """
        self._cache.last_timing_stats = value

    @property
    def _acq_trigger_channel(self) -> dict:
        """Expose acquisition trigger channel state for backward compatibility.

        :return: Dictionary mapping acquisition indices to their trigger channels.
        :rtype: dict
        """
        return self._cache.acq_trigger_channel

    # ========== Backward Compatibility: Delegation to Operation Classes ==========
    # These methods delegate to the operation classes for backward compatibility
    # with existing code that uses the flat API (e.g., adapter.upload_envelopes())
    # instead of the new nested API (e.g., adapter.generator.upload_envelopes())

    # Generator operations
    def upload_envelopes(
        self,
        *,
        gen_index: int,
        envelopes: list[EnvelopeSpec],
        auto_pad_noninterp: bool = True,
    ) -> dict:
        """Upload envelopes to generator memory (backward compatibility wrapper).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param envelopes: List of envelope specifications to upload.
        :type envelopes: list[EnvelopeSpec]
        :param auto_pad_noninterp: Automatically pad non-interpolated envelopes (default True).
        :type auto_pad_noninterp: bool
        :return: Result dictionary with loaded/failed envelopes.
        :rtype: dict
        """
        return self.generator.upload_envelopes(
            gen_index=gen_index,
            envelopes=envelopes,
            auto_pad_noninterp=auto_pad_noninterp,
        )

    def get_envelope_names(self, gen_index: int) -> list:
        """Get envelope names from generator cache (backward compatibility wrapper).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: List of envelope names.
        :rtype: list[str]
        """
        return self.generator.get_envelope_names(gen_index)

    def compile_waves(
        self,
        *,
        gen_index: int,
        waves: list[dict],
        replace: bool,
    ) -> dict:
        """Compile waves with hardware (backward compatibility wrapper).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param waves: List of wave specification dictionaries.
        :type waves: list[dict]
        :param replace: Whether to allow overwriting existing waves.
        :type replace: bool
        :return: Result dictionary with compiled waves.
        :rtype: dict
        """
        return self.generator.compile_waves(
            gen_index=gen_index,
            waves=waves,
            replace=replace,
        )

    def upload_readout_wave(self, **kwargs: Any) -> dict:  # noqa: ANN401
        """Upload readout wave to generator (backward compatibility wrapper).

        :param kwargs: Arguments passed to GeneratorOps.upload_readout_wave().
        :type kwargs: dict
        :return: Result dictionary with upload status.
        :rtype: dict
        """
        return self.generator.upload_readout_wave(**kwargs)

    def get_readout_wave_cache(self, gen_index: int) -> Any:  # noqa: ANN401
        """Get readout wave cache (backward compatibility wrapper).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: Readout wave cache entry or None.
        :rtype: WaveEntry | None
        """
        return self.generator.get_readout_wave_cache(gen_index)

    def get_wave_cache(self, gen_index: int) -> dict:
        """Get wave cache (backward compatibility wrapper).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :return: Wave cache dictionary.
        :rtype: dict[str, WaveEntry]
        """
        return self.generator.get_wave_cache(gen_index)

    def program_drive_sequence(self, **kwargs: Any) -> int:  # noqa: ANN401
        """Program drive sequence in trigger (backward compatibility wrapper).

        :param kwargs: Arguments passed to GeneratorOps.program_drive_sequence().
        :type kwargs: dict
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.generator.program_drive_sequence(**kwargs)

    def reset_wave_memory(self, **kwargs: Any) -> int:  # noqa: ANN401
        """Reset wave memory (backward compatibility wrapper).

        :param kwargs: Arguments passed to GeneratorOps.reset_wave_memory().
        :type kwargs: dict
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.generator.reset_wave_memory(**kwargs)

    def reset_envelopes(self, **kwargs: Any) -> int:  # noqa: ANN401
        """Reset envelopes (backward compatibility wrapper).

        :param kwargs: Arguments passed to GeneratorOps.reset_envelopes().
        :type kwargs: dict
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.generator.reset_envelopes(**kwargs)

    def set_drive_source(self, **kwargs: Any) -> int:  # noqa: ANN401
        """Set drive source (backward compatibility wrapper).

        :param kwargs: Arguments passed to GeneratorOps.set_drive_source().
        :type kwargs: dict
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.generator.set_drive_source(**kwargs)

    def generator_modulation(self, gen_index: int, label: str, gen_mod: Modulation) -> int:
        """Set generator modulation (backward compatibility wrapper for old API).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param label: Label ('drive' or 'readout').
        :type label: str
        :param gen_mod: Modulation specification with frequency_mhz and optional phase.
        :type gen_mod: Modulation
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.generator.set_modulation(gen_index=gen_index, label=label, mod=gen_mod)

    def gen_trigger2listen(self, gen_index: int, trig: TriggerCommand) -> int:
        """Set generator trigger listener (backward compatibility wrapper for old API).

        :param gen_index: Index of the target generator.
        :type gen_index: int
        :param trig: Trigger specification with type and channel.
        :type trig: TriggerCommand
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.generator.set_trigger_listener(gen_index=gen_index, trig=trig)

    # Trigger operations
    def tg_set_shots(self, shots: int) -> int:
        """Set number of trigger shots (backward compatibility wrapper for old API).

        :param shots: Number of shots.
        :type shots: int
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.trigger.set_shots(shots)

    def tg_set_duration(self, duration_cycles: int) -> int:
        """Set trigger duration (backward compatibility wrapper for old API).

        :param duration_cycles: Duration in cycles.
        :type duration_cycles: int
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.trigger.set_duration(duration_cycles)

    def tg_program_delays(
        self,
        *,
        drive: dict | None = None,
        readout: dict | None = None,
        drive_start_index: int = 1,
    ) -> int:
        """Program trigger delays (backward compatibility wrapper for old API).

        :param drive: Mapping of channel indices to lists of (delay, value) pairs for drive channels.
        :type drive: dict | None
        :param readout: Mapping of channel indices to readout delay specifications.
        :type readout: dict | None
        :param drive_start_index: FIFO index to start programming drive sequences (default 1).
        :type drive_start_index: int
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.trigger.program_delays(
            drive=drive,
            readout=readout,
            drive_start_index=drive_start_index,
        )

    def trigger_experiment(self) -> int:
        """Trigger experiment execution.

        Fires the trigger to start an experiment run.

        :return: Status code (0 for success).
        :rtype: int
        """
        return self.trigger.trigger_experiment()

    def tg_reset_drive_tracking(self) -> int:
        """Reset drive tracking state (backward compatibility wrapper for old API).

        Clears the high water mark for lazy FIFO cleanup.

        :return: Status code (0 for success).
        :rtype: int
        """
        return self.trigger.reset_drive_tracking()

    # Acquisition operations
    def acquisition_modulation(self, acq_index: int, acq_mod: Modulation) -> int:
        """Set acquisition modulation (backward compatibility wrapper for old API).

        :param acq_index: Index of the target acquisition.
        :type acq_index: int
        :param acq_mod: Modulation specification with frequency_mhz and optional phase.
        :type acq_mod: Modulation
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.acquisition.set_modulation(acq_index=acq_index, mod=acq_mod)

    def acq_trigger2listen(self, acq_index: int, trig: TriggerCommand) -> int:
        """Set acquisition trigger listener (backward compatibility wrapper for old API).

        :param acq_index: Index of the target acquisition.
        :type acq_index: int
        :param trig: Trigger specification with type and channel.
        :type trig: TriggerCommand
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.acquisition.set_trigger_listener(acq_index=acq_index, trig=trig)

    def acquisition_timing(self, acq_index: int, tof: int, duration: int) -> int:
        """Set acquisition timing parameters (backward compatibility wrapper for old API).

        :param acq_index: Index of the target acquisition.
        :type acq_index: int
        :param tof: Time-of-flight offset.
        :type tof: int
        :param duration: Acquisition duration.
        :type duration: int
        :return: Status code (0 for success).
        :rtype: int
        """
        return self.acquisition.set_timing(acq_index=acq_index, tof=tof, duration=duration)

    def run_multi_acquisition(
        self,
        *,
        adc_indices: list[int],
        mode: Literal["raw", "decimated", "accumulated"],
        shots: int,
        samp_per_shot: int,
        timeout: float | None = 1.0,
        validate_chunk: bool = True,
    ) -> Iterator[dict]:
        """Run multi-ADC acquisition with automatic chunking.

        :param adc_indices: List of ADC indices to acquire from.
        :type adc_indices: list[int]
        :param mode: Acquisition mode ('raw', 'decimated', or 'accumulated').
        :type mode: Literal["raw", "decimated", "accumulated"]
        :param shots: Total number of shots to acquire.
        :type shots: int
        :param samp_per_shot: Samples per shot.
        :type samp_per_shot: int
        :param timeout: Acquisition timeout in seconds (default 1.0).
        :type timeout: float | None
        :param validate_chunk: Whether to validate each chunk (default True).
        :type validate_chunk: bool
        :return: Generator yielding result dictionaries with acquired data.
        :rtype: Iterator[dict]
        """
        return self.acquisition.run_multi_acquisition(
            adc_indices=adc_indices,
            mode=mode,
            shots=shots,
            samp_per_shot=samp_per_shot,
            timeout=timeout,
            validate_chunk=validate_chunk,
        )

    def prepare_sweep(self, mode: str, adc_indices: list[int]) -> None:
        """Prepare acquisition system for sweep execution.

        :param mode: The acquisition mode (e.g., 'raw', 'decimated', 'accumulated').
        :type mode: str
        :param adc_indices: List of active ADC indices for the sweep.
        :type adc_indices: list[int]
        :return: None
        :rtype: None
        """
        return self.experiment.prepare_sweep(mode=mode, adc_indices=adc_indices)

    def end_sweep(self) -> None:
        """Finalize sweep execution and release resources.

        :return: None
        :rtype: None
        """
        return self.experiment.end_sweep()


__all__ = ["OverlayAdapter"]
