"""Acquisition trigger configuration.

This module handles:
- Acquisition trigger channel assignment

Configures which trigger channel each acquisition unit responds to.
Not to be confused with TriggerGeneratorOps (trigger generator IP control).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ....models.config_types import TriggerCommand

if TYPE_CHECKING:
    from .cache import AdapterContext


class TriggerOps:
    """Acquisition trigger configuration."""

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize TriggerOps.

        :param ctx: Shared adapter context with all dependencies.
        :type ctx: AdapterContext
        """
        self._ctx = ctx

    # ========================================================================
    # PUBLIC METHODS
    # ========================================================================

    def set_trigger_listener(self, acq_index: int, trig: TriggerCommand) -> dict:
        """Configure which trigger channel the acquisition should listen to.

        :param acq_index: Index of the target acquisition unit.
        :type acq_index: int
        :param trig: Dictionary defining the trigger source channel.
        :type trig: TriggerCommand
        :return: The applied trigger configuration.
        :rtype: dict
        """
        channel = trig["channel"]

        self._ctx.logger.debug("set_trigger_listener: acq=%d channel=%s", acq_index, channel)
        unit = self._ctx.ll.get_acq(acq_index)

        self._ctx.ll.call(
            unit.set_trigger_channel(channel=channel),
            operation="set_trigger_channel",
            driver_name="AcquisitionDriver",
            config_error=True,
        )

        if channel == 0:
            self._ctx.logger.debug("Acquisition %d is deaf to any trigger!", acq_index)
        else:
            self._ctx.logger.debug(
                "Acquisition %d listens to trigger_word channel %d",
                acq_index,
                channel,
            )
        self._ctx.cache.acq_trigger_channel[int(acq_index)] = int(channel)

        return {
            "acq_index": acq_index,
            "channel": channel,
        }


__all__ = ["TriggerOps"]
