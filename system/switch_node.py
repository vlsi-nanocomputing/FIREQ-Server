"""Switch Node class for FIREQ system node representation."""

from __future__ import annotations

import logging

from FIREQ_LL_API import AXIStreamSwitchDriver

from ._generic_node import _GenericNode
from ._utils import _MutableRef

logger = logging.getLogger(__name__)


class SwitchNode(_GenericNode):
    """Object representing the AXI-Stream switch IP.

    Dictionary definition for configuration:

    .. list-table::
       :header-rows: 1

       * - Key
         - Type
         - Description
       * - ``_name``
         - ``str``
         - Name of the switch instance
       * - ``_ll_handler``
         - ``AXIStreamSwitchDriver``
         - Low-level handler for the switch driver
    """

    nodetype = "data_switch"
    wraps = [AXIStreamSwitchDriver.__name__]

    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _ll_handler: AXIStreamSwitchDriver,
    ) -> None:
        """Initialize the switch node.

        :param name: Name of the node
        :type name: str
        :param parent: Parent node in the system tree
        :type parent: _GenericNode
        :param _ll_handler: Low-level handler for the switch
        :type _ll_handler: AXIStreamSwitchDriver
        """
        super().__init__(name=name, parent=parent)
        self._ll_handler = _ll_handler
        # get the interface mapping and initialize the input interfaces
        self._if_map = self.root.get_axi_stream_interface_map(self.name)
        self._input_interfaces: list[str] = []
        self._output_interface: str | None = None
        for interface, if_id in self._if_map.items():
            if interface == "M_AXIS":
                self._output_interface = if_id
            else:
                # fix the order of input interfaces here
                self._input_interfaces.append(if_id)
        # sanity check
        if not self._input_interfaces or self._output_interface is None:
            raise RuntimeError(f"Could not resolve input or output interface for SWITCH {self.name}")
        # payload, will be properly initialized in _build_dependencies
        self.payload: list[_MutableRef] = []
        self.max_payload_size = _MutableRef()
        # add references for other nodes
        self.root.add_reference(f"{self._output_interface}/payload", self.payload)
        self.root.add_reference(f"{self._output_interface}/max_payload_size", self.max_payload_size)
        self.root.add_reference(f"{self._output_interface}/payload_switch_func", self.set_master_to_input)
        # tracking the current input index
        self.current_input_index: int = 0

    def _build_dependencies(self) -> None:
        """Build the dependencies for this node."""
        _max_size: int = 0
        for s_if in self._input_interfaces:
            # get the input payload and append it to the list
            # TODO: this list should be ordered, so that the switch user can correctly select the slave payload
            input_payload = self.root.get_reference(f"{s_if}/payload")
            if isinstance(input_payload, list):
                raise NotImplementedError(
                    f"Got a list as an input payload in SWITCH: {self.name} on input interface: {s_if}. Cascaded switch nodes are not implemented yet."
                )
            # TODO: add other checks on the input payload here
            self.payload.append(self.root.get_reference(f"{s_if}/payload"))
            _max_size = max(_max_size, self.root.get_reference(f"{s_if}/max_payload_size")["value"])
        self.max_payload_size["value"] = _max_size

    def set_master_to_input(self, slave_index: int) -> None:
        """Set the master to the specified slave input.

        :param slave_index: Index of the slave to connect the master to
        :type slave_index: int
        :raises ValueError: If the switch operation fails
        """
        ret = self._ll_handler.switch_to_input(slave_index)
        if ret != 0:
            logger.error("Failed to set master to input %s", slave_index)
            raise ValueError(f"Failed to set master to input {slave_index}")
        logger.debug("Set master to input %s", slave_index)
