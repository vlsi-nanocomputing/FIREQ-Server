"""High-level composition-based adapter for FIREQ hardware.

This module implements the OverlayAdapter using flat operation classes.
"""

import logging

from ...models.exceptions import HardwareStateError
from ..dma_engine import DMAEngine
from ._acq_ops import AcquisitionOps
from ._gen_ops import GeneratorOps
from ._trigger_gen_ops import TriggerGeneratorOps


class OverlayAdapter:
    """High-level adapter for FIREQ hardware control using flat operation classes.

    This class composes three operation classes to provide a server-facing
    interface on top of the low-level FIREQ hardware drivers:

    - GeneratorOps: Wave management, envelope upload, FIFO, modulation, trigger
    - TriggerGeneratorOps: Trigger generator control
    - AcquisitionOps: DMA acquisition, sweep, modulation, trigger, timing

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

    Each operation class owns its own state. No shared mutable containers.

    Attributes
    ----------
    generator : GeneratorOps
        Wave and generator modulation operations.
    trigger : TriggerGeneratorOps
        Trigger generator operations.
    acquisition : AcquisitionOps
        DMA acquisition operations.
    """

    def __init__(self, fireq_soc: object, *, logger: logging.Logger | None = None) -> None:
        """Initialize the High-Level Adapter with flat operation classes.

        Each operation class receives only its required dependencies (no shared
        context object). Cross-dependencies are resolved via explicit constructor
        parameters.

        :param fireq_soc: The FIREQ_SoC hardware driver instance.
        :type fireq_soc: FIREQ_SoC-compatible
        :param logger: Optional logger instance for telemetry. If None, a default logger
            is created.
        :type logger: logging.Logger | None
        """
        # Fail-fast sanity check:
        if not fireq_soc.is_healthy:
            raise HardwareStateError("Unexpected Error: overlay upload failed!")

        self._fireq_soc = fireq_soc
        self._logger = logger or logging.getLogger(__name__)

        # DMA engine (needed for acquisition)
        if self._fireq_soc.dma is None or self._fireq_soc.axis_switch is None:
            raise HardwareStateError("DMA or AXI-Stream switch missing in overlay")

        dma_engine = DMAEngine(
            self._fireq_soc.dma,
            self._fireq_soc.axis_switch,
            logger=self._logger,
            hw_specs=self._fireq_soc.hw_specs,
        )

        # Compose flat operation classes with explicit dependencies
        self.trigger = TriggerGeneratorOps(self._fireq_soc, self._logger)
        self.generator = GeneratorOps(self._fireq_soc, self._logger)
        self.acquisition = AcquisitionOps(self._fireq_soc, self._logger, dma_engine, self.trigger)

    # ========== Explicit Delegations to FIREQ_SoC ==========

    def summary(self) -> dict:
        """Return hardware summary from the FIREQ_SoC driver.

        :return: Summary dictionary with DAC/ADC sample rates, parallelism, and IP counts.
        :rtype: dict
        """
        return self._fireq_soc.summary()

    def rf_mapping(self) -> dict:
        """Return RF tile/block mapping from the FIREQ_SoC driver.

        :return: RF mapping dictionary with DAC and ADC tile/block assignments.
        :rtype: dict
        """
        return self._fireq_soc.rf_mapping()

    @property
    def hw_specs(self) -> dict:
        """Hardware specifications from the FIREQ_SoC driver.

        :return: The full hw_specs dictionary.
        :rtype: dict
        """
        return self._fireq_soc.hw_specs

    @property
    def num_generators(self) -> int:
        """Number of generator IPs in the overlay.

        :return: Generator count.
        :rtype: int
        """
        return self._fireq_soc.num_generators

    @property
    def num_acquisitions(self) -> int:
        """Number of acquisition IPs in the overlay.

        :return: Acquisition count.
        :rtype: int
        """
        return self._fireq_soc.num_acquisitions

    def calibrate_adc(self, acq_index: int, gen_index: int, label: str, freq_mhz: float) -> None:
        """Perform ADC calibration via the FIREQ_SoC driver.

        :param acq_index: Acquisition IP index.
        :type acq_index: int
        :param gen_index: Generator IP index.
        :type gen_index: int
        :param label: Output selection ('drive' or 'readout').
        :type label: str
        :param freq_mhz: Calibration frequency in MHz.
        :type freq_mhz: float
        """
        self._fireq_soc.calibrate_adc(
            acq_index=acq_index,
            gen_index=gen_index,
            label=label,
            freq_mhz=freq_mhz,
        )


__all__ = ["OverlayAdapter"]
