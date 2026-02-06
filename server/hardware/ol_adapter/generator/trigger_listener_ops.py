"""Generator trigger listener configuration.

This module handles:
- Generator trigger channel assignment
- Trigger type configuration (internal vs external)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ....models.config_types import TriggerCommand

if TYPE_CHECKING:
    from ..cache import AdapterContext


class TriggerListenerOps:
    """Generator trigger listener configuration.

    Note: This handles generator trigger listening (which trigger to respond to).
    This is distinct from the global TriggerOps class which controls the trigger generator hardware.
    """

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize TriggerListenerOps.

        :param ctx: Shared adapter context with all dependencies.
        """
        self._ctx = ctx

    def set_trigger_listener(self, gen_index: int, trig: TriggerCommand) -> dict:
        """Configure which trigger channel the generator should listen to.

        :param gen_index: Index of the target generator.
        :param trig: Dictionary defining the trigger type and source channel.
        :return: The applied trigger configuration.
        """
        channel = trig["channel"]
        ttype = trig["ttype"]

        self._ctx.logger.debug(
            "set_trigger_listener: gen=%d ttype=%s channel=%s",
            gen_index,
            ttype,
            channel,
        )
        unit = self._ctx.ll.get_gen(gen_index)

        self._ctx.ll.call(
            unit.set_trigger_channel(channel=channel, ttype=ttype),
            operation="set_trigger_channel",
            driver_name="GeneratorDriver",
            config_error=True,
        )

        if channel == 0:
            self._ctx.logger.debug("Generator %d is deaf to any trigger!", gen_index)
        else:
            self._ctx.logger.debug(
                "Generator %d listens to %s_trigger_word channel %d",
                gen_index,
                ttype,
                channel,
            )

        return {
            "gen_index": gen_index,
            "ttype": ttype,
            "channel": channel,
        }


__all__ = ["TriggerListenerOps"]
