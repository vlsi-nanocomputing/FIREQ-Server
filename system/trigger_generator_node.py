"""Trigger Generator Node class for FIREQ system node representation."""

from __future__ import annotations

import logging
from typing import Any

from FIREQ_LL_API import TriggerGeneratorDriver

from ._generic_node import _GenericNode
from ._utils import _get_periods_from_clock, _MutableRef

logger = logging.getLogger(__name__)


class _DelayItem(_GenericNode):
    """Object representing a trigger delay.

    Dictionary definition for configuration:

    .. list-table::
       :header-rows: 1

       * - Key
         - Type
         - Description
       * - ``_name``
         - ``str``
         - Name of the delay item
       * - ``_ttype``
         - ``str``
         - Either ``"drive"`` or ``"readout"``
       * - ``_channel``
         - ``int``
         - Channel of the drive or readout trigger
       * - ``_index``
         - ``int``
         - Only used for drive delays
       * - ``_generate_trigger``
         - ``bool``
         - If ``True``, a drive trigger is generated at the end of the delay
       * - ``$delay``
         - ``float``
         - Value of the delay in ns
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
        """Initialize a trigger delay item.

        The ``_generate_trigger`` and ``_index`` parameters should only be used
        for drive delays.

        :param name: Name of the delay item
        :type name: str
        :param parent: Parent trigger generator node
        :type parent: TriggerGeneratorNode
        :param _ttype: Type of the delay, either ``"drive"`` or ``"readout"``
        :type _ttype: str
        :param _channel: Channel of the drive or readout trigger
        :type _channel: int
        :param _index: Index of the drive trigger, defaults to 0
        :type _index: int
        :param _generate_trigger: Whether a drive trigger should be generated,
            defaults to ``False``
        :type _generate_trigger: bool
        :raises ValueError: If the delay type is not supported
        """
        super().__init__(name, parent)
        self._ttype = _ttype
        self._channel = _channel
        self._index = _index
        self._generate_trigger = _generate_trigger
        if self._ttype not in ["readout", "drive"]:
            logger.error("unsupported delay type %s", self._ttype)
            raise ValueError(f"unsupported delay type {self._ttype}")

    @_GenericNode.parameter_callback("$delay", sweepable=True, cost=3)
    def set_delay(self, delay: float) -> int:
        """Set the delay for this delay item.

        :param delay: Delay value in nanoseconds
        :type delay: float
        :return: Error code (0 on success)
        :rtype: int
        """
        if self._ttype == "readout":
            return self.parent._ll_handler.set_readout_delay(
                _get_periods_from_clock(delay, self.parent._clock_frequency), self._channel
            )
        elif self._ttype == "drive":
            return self.parent._ll_handler.insert_drive_delay(
                self._channel,
                self._index,
                _get_periods_from_clock(delay, self.parent._clock_frequency),
                int(self._generate_trigger),
            )
        return -3


class TriggerGeneratorNode(_GenericNode):
    """Object representing the trigger generator system.

    Dictionary definition for configuration:

    .. list-table::
       :header-rows: 1

       * - Key
         - Type
         - Description
       * - ``_name``
         - ``str``
         - Name of the trigger generator node instance
       * - ``_ll_handler``
         - ``TriggerGeneratorDriver``
         - Low-level handler for the trigger generator
       * - ``$experiment_duration``
         - ``float``
         - Duration of the experiment shot in ns
    """

    nodetype = "trigger_generator"
    wraps = [TriggerGeneratorDriver.__name__]

    def __init__(self, name: str, parent: _GenericNode, _ll_handler: TriggerGeneratorDriver) -> None:
        """Initialize the trigger generator node.

        :param name: Name of the trigger generator node
        :type name: str
        :param parent: Parent node in the system tree
        :type parent: _GenericNode
        :param _ll_handler: Low-level handler for the trigger generator
        :type _ll_handler: TriggerGeneratorDriver
        """
        super().__init__(name, parent)
        # get the clock frequency from the root node
        self._clock_frequency = self.root.get_fabric_frequency()
        self._ll_handler = _ll_handler
        self.root.register_update_function(self.root.make_func_label(self, "update_hw_shots"), self.update_hw_shots)
        # this will be initialized in _build_dependencies
        self._hw_shots: _MutableRef | None = None
        # reference to the most hw shots
        self._hw_supported_shots = _MutableRef(value=self._ll_handler.max_hw_repetitions)
        self.root.add_reference("hw_supported_hw_shots", self._hw_supported_shots)

    def _build_dependencies(self) -> None:
        """Build the dependencies for this node.

        Resolves the hw_shot dependencies.
        """
        # get the hw shots referece
        self._hw_shots = self.root.get_reference(self.root.make_func_label(self.root, "hw_shots"))
        # set the dependency between this node and the number of hw shots
        self.root.add_dependency(
            self.root.make_func_label(self, "update_hw_shots"),
            depends_on=self.root.make_func_label(self.root, "hw_shots"),
        )

    @_GenericNode.parameter_callback("$experiment_duration", sweepable=True, cost=1)
    def set_experiment_duration(self, duration: float) -> int:
        """Set the experiment duration.

        :param duration: Duration of the experiment in nanoseconds
        :type duration: float
        :return: Error code (0 on success)
        :rtype: int
        """
        clock_cycles = _get_periods_from_clock(duration, self._clock_frequency)
        return self._ll_handler.set_experiment_duration(int(clock_cycles))

    def create_child(self, name: str, of_type: str, **kwargs: dict[str, Any]) -> _DelayItem:
        """Create a child node of the specified type.

        :param name: Name of the child node
        :type name: str
        :param of_type: Type of child node — currently only ``"delay"`` is supported
        :type of_type: str
        :param kwargs: Additional arguments for the child node
        :type kwargs: dict[str, Any]
        :return: The created child node
        :rtype: _DelayItem
        :raises ValueError: If a child with the same name already exists or the
            type is not supported
        """
        if any(child.name == name for child in self.children):
            logger.error("child with name %s already exists", name)
            raise ValueError(f"child with name {name} already exists")
        if of_type == "delay":
            return _DelayItem(name=name, parent=self, **kwargs)
        else:
            logger.error("unsupported child type %s", of_type)
            raise ValueError(f"unsupported child type {of_type}")

    def update_hw_shots(self) -> bool:
        """Update the number of hardware shots executed in the experiment.

        This update function depends on the maximum number of shots that the data
        FIFOs can support.  It picks the minimum across all FIFOs to prevent
        overflow and coerces the value to the hardware's maximum supported
        repetitions.

        :return: ``True`` if the number of shots has changed, ``False`` otherwise
        :rtype: bool
        :raises ValueError: If the number of shots is ``None`` or zero, indicating
            a broken experiment setup or that a single-shot packet would overflow
            a FIFO
        :raises ValueError: If the driver call fails
        """
        # check that the number of hw shots is valid
        if not self._hw_shots or self._hw_shots["value"] == 0:
            logger.error("hw shots is None or zero")
            raise ValueError("hw shots is None or zero")
        # write the amount to the driver
        hw_shots = self._hw_shots["value"]
        ret = self._ll_handler.set_number_of_shots(hw_shots)
        if ret != 0:
            logger.error("failed to set number of shots %s", hw_shots)
            raise ValueError(f"failed to set number of shots {hw_shots}")
        return True
