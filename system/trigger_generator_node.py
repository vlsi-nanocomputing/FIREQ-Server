from __future__ import annotations

import logging
from typing import Any

from _generic_node import _GenericNode
from _utils import _get_periods_from_clock

from FIREQ_LL_API import TriggerGeneratorDriver

logger = logging.getLogger(__name__)


class _DelayItem(_GenericNode):
    """Object representing a trigger delay.

    Dict definition:
        _name: str, name of the delay
        _ttype: str, either 'drive' or 'readout'
        _channel: int, channel of the drive or readout trigger
        _index: int, only used for drive delays
        _generate_trigger: bool(true), used to know if drive trigger should be generated
        $delay: float, value of the delay
    """

    nodetype = "delay"

    def __init__(
        self,
        name: str,
        parent: TriggerGeneratorNode,
        _ttype: str,
        _channel: int,
        _index: int = 0,
        _generate_trigger: bool = False,
    ) -> None:
        """Initialize the a trigger delay item.

        The _generate_trigger and _index parameters should only be used for drive delays.

        :param name: name of the delay item
        :type name: str
        :param parent: parent node
        :type parent: TriggerGeneratorNode
        :param _ttype: type of the delay, either 'drive' or 'readout'
        :type _ttype: str
        :param _channel: channel of the drive or readout trigger
        :type _channel: int
        :param _index: index of the drive trigger
        :type _index: int
        :param _generate_trigger: if the drive trigger should be generated, defaults to False
        :type _generate_trigger: bool
        :raises ValueError: if the delay type is not supported
        """
        super().__init__(name, parent)
        self._ttype = _ttype
        self._channel = _channel
        self._index = _index
        self._generate_trigger = _generate_trigger
        if self._ttype not in ["readout", "drive"]:
            logger.error("unsupported delay type %s", self._ttype)
            raise ValueError("unsupported delay type")

    @_GenericNode.parameter_callback("$delay", sweepable=True, cost=3)
    def set_delay(self, delay: float) -> int:
        """Set the delay for this delay item.

        :param delay: delay value in ns
        :type delay: float
        """
        if self.type == "readout":
            return self.parent._ll_handler.set_readout_delay(
                _get_periods_from_clock(delay, self.parent._clock_frequency), self._channel
            )
        elif self.type == "drive":
            return self.parent._ll_handler.insert_drive_delay(
                self.channel,
                self.index,
                _get_periods_from_clock(delay, self.parent._clock_frequency),
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

    nodetype = "trigger_generator"

    def __init__(
        self, name: str, parent: _GenericNode, _clock_frequency: float, _ll_handler: TriggerGeneratorDriver
    ) -> None:
        """Initialize the trigger generator node.

        :param name: name of the trigger generator node
        :type name: str
        :param parent: parent node
        :type parent: _GenericNode
        :param _clock_frequency: clock frequency of the trigger generator in MHz
        :type _clock_frequency: float
        :param _ll_handler: low level handler for the trigger generator
        :type _ll_handler: TriggerGeneratorDriver
        """
        super().__init__(name, parent)
        self._clock_frequency = _clock_frequency
        self._ll_handler = _ll_handler
        self.root.register_update_function(self, self.update_hw_shots)
        self.hw_shots = 0

    @_GenericNode.parameter_callback("$experiment_duration", sweepable=True, cost=1)
    def set_experiment_duration(self, duration: float) -> int:
        """Set the experiment duration.

        :param duration: duration of the experiment in ns
        :type duration: float
        :return: Error code, 0 if successful
        :rtype: int
        """
        clock_cycles = _get_periods_from_clock(duration, self._clock_frequency)
        return self._ll_handler.set_experiment_duration(int(clock_cycles))

    def create_child(self, name: str, of_type: str, **kwargs: dict[str, Any]) -> _DelayItem:
        """Create a child node of the specified type.

        :param name: name of the child node
        :param of_type: type of the child node
        :param kwargs: additional arguments for the child node
        :return: the created child node
        :raises ValueError: if the child node already exists or if the type is not supported
        """
        if any(child.name == name for child in self.children):
            logger.error("child with name %s already exists", name)
            raise ValueError(f"child with name {name} already exists")
        if of_type == "delay":
            return _DelayItem(name=name, parent=self, **kwargs)
        else:
            logger.error("unsupported child type %s", of_type)
            raise ValueError("unsupported child type")

    def update_hw_shots(self) -> bool:
        """Update the number of hw shots that are executed in the experiment.

        This update function depends on the maximum number of shots that the data FIFO can support.
        The update function will therefore pick the minimum to make sure that the data FIFO is not overflown and set the `hw_shot` attribute accordingly.
        To avoid low level issues, the number of shots is also coerced to the maximum number of shots that the hardware can support.

        :return: True if the number of shots has changed, False otherwise
        :rtype: bool
        :raises ValueError: if the number of shots is None or zero, indicating a broken experiment setup (no data can be generated) or that a single shot packet would overflow a FIFO
        :raises ValueError: if the driver call failed, which should never happen
        """
        hw_shots = min(self.root.get_max_hw_shots())
        # check if the result is none or zero, in which case no data can be generated
        if hw_shots is None or hw_shots == 0:
            logger.error("hw shots is None or zero")
            raise ValueError("hw shots is None or zero")
        # coerce number of shots to the maximum supported by the hardware
        hw_shots = min(hw_shots, self._ll_handler.max_hw_repetitions)
        if self.hw_shots == hw_shots:
            return False
        self.hw_shots = hw_shots
        # write the amount to the driver
        ret = self._ll_handler.set_number_of_shots(hw_shots)
        if ret != 0:
            logger.error("failed to set number of shots %s", hw_shots)
            raise ValueError("failed to set number of shots")
        return True
