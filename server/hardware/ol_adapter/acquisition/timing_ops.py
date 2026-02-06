"""Time-of-flight and duration timing configuration for acquisition.

This module provides the TimingOps class that handles:
- Time-of-flight (ToF) delay configuration
- Acquisition duration configuration
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cache import AdapterContext


class TimingOps:
    """Operation class for acquisition timing configuration.

    Handles timing parameters including:
    - Time-of-flight (delay before acquisition starts)
    - Acquisition duration in clock cycles

    Attributes:
    -----------
    _ctx : AdapterContext
        Shared context containing ll, cache, logger, and other dependencies.
    """

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize TimingOps.

        :param ctx: Shared adapter context with all dependencies.
        :type ctx: AdapterContext
        """
        self._ctx = ctx

    # ========================================================================
    # PUBLIC METHODS
    # ========================================================================

    def set_timing(self, acq_index: int, tof: int, duration: int) -> dict:
        """Configure the timing parameters (Time of Flight and acquisition duration).

        :param acq_index: Index of the acquisition unit.
        :type acq_index: int
        :param tof: Time of Flight delay in clock cycles.
        :type tof: int
        :param duration: Acquisition duration in clock cycles.
        :type duration: int
        :return: The applied timing configuration.
        :rtype: dict
        """
        self._ctx.logger.debug("set_timing: acq_index=%d tof=%d duration=%d", acq_index, tof, duration)
        acq = self._ctx.ll.get_acq(acq_index)

        self._ctx.ll.call(
            acq.set_acquisition_duration(duration),
            operation="set_acquisition_duration",
            driver_name="AcquisitionDriver",
            config_error=True,
        )

        self._ctx.ll.call(
            acq.set_time_of_flight(tof),
            operation="set_time_of_flight",
            driver_name="AcquisitionDriver",
            config_error=True,
        )
        return {
            "acq_index": acq_index,
            "tof": tof,
            "duration": duration,
        }


__all__ = ["TimingOps"]
