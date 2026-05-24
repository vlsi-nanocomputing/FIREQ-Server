from __future__ import annotations

import logging

import numpy as np
from _generic_node import _GenericNode
from _utils import _get_periods_from_clock, _safe_float_cast

logger = logging.getLogger(__name__)


class _DelayItem(_GenericNode):
    """Object representing a trigger delay.

    Dict definition:
        _name: str, name of the delay
        _ttype: str, either 'drive' or 'readout'
        _channel: int, channel of the drive or readout trigger
        _index: int, only used for drive delays
        _generate_trigger: used to know if drive trigger should be generated
        $delay: float, value of the delay
    """

    nodetype = "delay"

    def __init__(self, name: str, parent: TriggerGeneratorNode = None, **kwargs: dict[str, Any]) -> None:
        super().__init__(name, parent, **kwargs)
        # validation of arguments
        if self._channel is None:
            logger.error("channel not specified")
            raise ValueError("channel not specified")
        if self._ttype not in ["readout", "drive"]:
            logger.error("unsupported delay type %s", self._ttype)
            raise ValueError("unsupported delay type")
        if self._ttype == "drive" and (self._index is None or self._generate_trigger is None):
            logger.error("index and/or generate_trigger not specified for drive delay")
            raise ValueError("index and/or generate_trigger not specified for drive delay")

    @_GenericNode.parameter_callback("$delay", sweepable=True, cost=3)
    def set_delay(self, delay: float) -> int:
        """Set the delay for this node."""
        if self.type == "readout":
            return self.parent._ll_handler.set_readout_delay(
                _get_periods_from_clock(delay, self.parent.clock_frequency), self._channel
            )
        elif self.type == "drive":
            return self.parent._ll_handler.insert_drive_delay(
                self.channel,
                self.index,
                _get_periods_from_clock(delay, self.parent.clock_frequency),
                int(self.generate_trigger),
            )


class TriggerGeneratorNode(_GenericNode):
    """Object representing the trigger generator system.

    Dict definition:
        _name: str, name of the trigger generator node/istance
        _clock_frequency: float, clock frequency of the trigger generator in MHz
        _ll_handler: TriggerGeneratorDriver, low level handler for the trigger generator
        $experiment_duration: float, duration of the experiment shot in ns
    """

    # TODO: actually handle dependencies

    nodetype = "trigger_generator"

    def __init__(self, name: str, parent: _GenericNode = None, **kwargs: dict[str, Any]) -> None:
        super().__init__(name, parent, **kwargs)
        # check attributes
        if self._clock_frequency is None:
            logger.error("clock_frequency not specified")
            raise ValueError("clock_frequency not specified")
        if self._ll_handler is None:
            logger.error("ll_handler not specified")
            raise ValueError("ll_handler not specified")

    @_GenericNode.parameter_callback("$experiment_duration", sweepable=True, cost=1)
    def set_experiment_duration(self, duration: str | float) -> int:
        """Set the experiment duration."""
        clock_cycles = _get_periods_from_clock(duration, self._clock_frequency)
        return self._ll_handler.set_experiment_duration(int(clock_cycles))

    def create_child(self, name: str, of_type: str, **kwargs: dict[str, Any]) -> _DelayItem:
        """Create a child node of the specified type."""
        if of_type == "delay":
            return _DelayItem(name=name, parent=self, **kwargs)
        else:
            logger.error("unsupported child type %s", of_type)
            raise ValueError("unsupported child type")
