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

    def __init__(self, name: str, parent: TriggerGeneratorNode, _ttype: str, _channel_mask: int, _index: int) -> None:
        """Initialize a trigger delay item.

        This delay will be pushed to the trigger generator IP, the ""_channel_mask"" input will determine
        which channels of the relative trigger word (drive or readout) will be triggered.
        Setting this value to 0 will generate no triggers, thus it can be used to generate a delay without
        triggering any channel.

        :param name: Name of the delay item
        :type name: str
        :param parent: Parent trigger generator node
        :type parent: TriggerGeneratorNode
        :param _ttype: Type of the delay, either ``"drive"`` or ``"readout"``
        :type _ttype: str
        :param _channel_mask: Channel of the drive or readout trigger
        :type _channel_mask: int
        :param _index: Index of the drive trigger, defines the order, starts at 1
        :type _index: int
        :raises ValueError: If the delay type is not supported
        """
        super().__init__(name, parent)
        self._ttype = _ttype
        self._channel_mask = _channel_mask
        self._index = _index
        if self._ttype not in ["readout", "drive"]:
            self.log.error("unsupported delay type %s", self._ttype)
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
            return self.parent._ll_handler.insert_delay(
                self._channel_mask, True, self._index, _get_periods_from_clock(delay, self.parent._clock_frequency)
            )
        elif self._ttype == "drive":
            return self.parent._ll_handler.insert_delay(
                self._channel_mask, False, self._index, _get_periods_from_clock(delay, self.parent._clock_frequency)
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
        self._hw_supported_shots = _MutableRef(value=self._ll_handler.max_hw_repetitions)
        self.root.add_reference("hw_supported_hw_shots", self._hw_supported_shots)
        # update functions and outside reference
        self._hw_shots: _MutableRef | None = None
        self.root.register_update_function(self.root.make_func_label(self, "hw_shots"), self.update_hw_shots)

    def _reset_all(self) -> None:
        for child in self.children:
            child.parent = None
        self._hw_supported_shots.reset_hash()

    def _build_dependencies(self) -> None:
        """Build the dependencies for the trigger generator node."""
        # get the number of hw shots
        self._hw_shots = self.root.get_reference(self.root.make_func_label(self.root, "hw_shots"))
        # add dependency between the update function and the hw shots of root
        self.root.add_dependency(
            self.root.make_func_label(self, "hw_shots"), self.root.make_func_label(self.root, "hw_shots")
        )

    def update_hw_shots(self) -> bool:
        """Update the number of hardware shots."""
        if self._hw_shots:
            ret = self._ll_handler.set_number_of_shots(self._hw_shots["value"])
            if ret != 0:
                self.log.error("Failed to set the number of shots")
                raise RuntimeError("Failed to set the number of shots")
        else:
            self.log.error("Reference to hw shots is invalid")
            raise RuntimeError("Reference to hw shots is invalid")
        return True

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
            self.log.error("child with name %s already exists", name)
            raise ValueError(f"child with name {name} already exists")
        if of_type == "delay":
            return _DelayItem(name=name, parent=self, **kwargs)
        else:
            self.log.error("unsupported child type %s", of_type)
            raise ValueError(f"unsupported child type {of_type}")

    def start_experiment(self) -> None:
        """Start the experiment by calling the low-level handler."""
        self._ll_handler.start_experiment()

    def is_done(self) -> bool:
        """Check if the experiment is finished.

        :return: True if the experiment is finished, False if still running
        :rtype: bool
        """
        return self._ll_handler.is_done()
