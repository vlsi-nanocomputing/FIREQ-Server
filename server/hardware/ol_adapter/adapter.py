"""High-level composition-based adapter for FIREQ hardware.

This module implements the OverlayAdapter using composition of operation classes
instead of mixins, providing a cleaner, more maintainable architecture.
"""

import logging
from typing import Any

from ...models.exceptions import HardwareStateError
from ..dma_engine import AcquisitionEngine
from .acquisition import AcquisitionOps
from .cache import AdapterContext, CacheContainers
from .experiment import ExperimentOps
from .generator import GeneratorOps
from .ll_access import LowLevelAccess
from .trigger import TriggerOps


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

    Architecture
    ------------
    Operation classes share an AdapterContext containing all dependencies.
    Operation objects are stored in the context to enable cross-dependencies:
    - AcquisitionOps calls TriggerOps for hardware trigger coordination
    - ExperimentOps calls AcquisitionOps for sweep orchestration

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

    def __init__(self, overlay_driver: object, *, logger: logging.Logger | None = None) -> None:
        """Initialize the High-Level Adapter using composition with shared context.

        Uses a shared AdapterContext passed to all operation classes, enabling
        dependency injection with minimal constructor parameters.

        :param overlay_driver: The low-level overlay driver instance.
        :type overlay_driver: fireq_soc
        :param logger: Optional logger instance for telemetry. If None, a default logger
            is created.
        :type logger: Optional[logging.Logger]
        """
        # Fail-fast sanity check:
        if not overlay_driver.is_healthy:
            raise HardwareStateError("Unexpected Error: overlay upload failed!")

        self.overlay_driver = overlay_driver
        self.logger = logger or logging.getLogger(__name__)

        # DMA engine (needed for acquisition)
        if self.overlay_driver.dma is None or self.overlay_driver.axis_switch is None:
            raise HardwareStateError("DMA or AXI-Stream switch missing in overlay")

        # The DMA engine is constructed once as a long-lived resource.
        dma_engine = AcquisitionEngine(
            self.overlay_driver.dma,
            self.overlay_driver.axis_switch,
            logger=self.logger,
            hw_specs=self.overlay_driver.hw_specs,
        )

        # Create shared context with all dependencies
        # This replaces verbose individual parameter passing to operation classes
        self._ctx = AdapterContext(
            overlay_driver=self.overlay_driver,
            ll=LowLevelAccess(self.overlay_driver, self.logger),
            cache=CacheContainers(),
            logger=self.logger,
            dma_engine=dma_engine,
        )

        # Compose operation classes with single context parameter
        self.trigger = TriggerOps(self._ctx)
        self.generator = GeneratorOps(self._ctx)
        self.acquisition = AcquisitionOps(self._ctx)
        self.experiment = ExperimentOps(self._ctx)

        # Store operation references in context to enable cross-dependencies.
        # AcquisitionOps calls TriggerOps methods during DMA execution.
        # ExperimentOps calls AcquisitionOps methods for sweep orchestration.
        self._ctx.trigger = self.trigger
        self._ctx.generator = self.generator
        self._ctx.acquisition = self.acquisition

    # ========== Proxy Pattern for Driver Access ==========

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
        return self._ctx.ll.call(obj, method_name, *args, **kwargs)

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
        return getattr(self.overlay_driver, name)

    # ========== Public Properties ==========

    @property
    def last_timing_stats(self) -> dict:
        """Retrieve the last timing statistics from an acquisition.

        :return: Dictionary with timing breakdown (total_ms, fpga_wait_ms, dma_overhead_ms, sw_overhead_ms).
        :rtype: dict
        """
        return self._ctx.cache.last_timing_stats

    @last_timing_stats.setter
    def last_timing_stats(self, value: dict) -> None:
        """Set timing statistics (for testing purposes).

        :param value: Timing statistics dictionary.
        :type value: dict
        """
        self._ctx.cache.last_timing_stats = value


__all__ = ["OverlayAdapter"]
