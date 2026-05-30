"""Switch Node class for FIREQ system node representation."""

from __future__ import annotations

import logging

from _generic_node import _GenericNode

from FIREQ_LL_API import AXIStreamSwitchDriver

from ._utils import _get_dict_hash

logger = logging.getLogger(__name__)


class SwitchNode(_GenericNode):
    """Object representing the acquisition IPs.

    Dict definition:
        _name: str, name of the switch instance
        _ll_handler: SwitchDriver, handler to the low level driver
        _input_nodes: list[_GenericNode], input node of the switch
        _input_interfaces: list[str], name of the interfaces connected to each node
        _output_interface: str, name of the output interface of this node
    """

    nodetype = "acquisition"

    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _ll_handler: AXIStreamSwitchDriver,
        _input_nodes: _GenericNode,
        _input_interfaces: list[str],
        _output_interface: str,
    ) -> None:
        """Initialize the switch node.

        :param name: Name of the node
        :type name: str
        :param parent: Parent node
        :type parent: _GenericNode
        :param _ll_handler: Low level handler
        :type _ll_handler: AXIStreamSwitchDriver
        :param _input_nodes: Input nodes to this switch
        :type _input_nodes: list[_GenericNode]
        :param _input_interfaces: Name of the interfaces connected to each node
        :type _input_interfaces: list[str]
        :param _output_interface: Name of the output interface of this node
        :type _output_interface: str
        """
        super().__init__(name=name, parent=parent)
        self._ll_handler = _ll_handler
        self._input_nodes = _input_nodes
        self._input_interfaces = _input_interfaces
        self._output_interface = _output_interface
        if len(_input_nodes) != len(_input_interfaces):
            logger.error("input nodes and interfaces must have the same length")
            raise ValueError("input nodes and interfaces must have the same length")
        # other paramters
        self.extraction_order = [0] * len(self._input_nodes)
        self.extraction_order_hash = _get_dict_hash(self.extraction_order)
        self.payloads = {}
        self.payload_hash = _get_dict_hash(self.payloads)
        # register the update functions with the orchestrator
        self.parent.register_update_function(
            identifier=f"{self.name}/update_extraction_order", func=self.update_extraction_order
        )
        self.parent.register_update_function(identifier=f"{self.name}/payloads", func=self.update_payload)

    def _build_dependencies(self) -> None:
        """Build the dependency for this node."""
        # the extraction order and the output payloads depend on the input nodes payloads
        self.parent.add_dependency(
            identifier=f"{self.name}/update_extraction_order",
            dependencies=[f"{node.name}/payload" for node in self._input_nodes],
        )
        self.parent.add_dependency(
            identifier=f"{self.name}/payloads",
            dependencies=[f"{node.name}/payload" for node in self._input_nodes],
        )
        # the output payloads depend on the extraction order
        self.parent.add_dependency(
            identifier=f"{self.name}/update_extraction_order",
            dependencies=[f"{self.name}/payloads"],
        )

    def set_master_to_first_payload(self) -> dict:
        """Set the master to the first payload in the extraction order."""
        self.current_input_index = 0
        if len(self.extraction_order) == 0:
            return {}
        self._ll_handler.switch_to_input(self.extraction_order[0])
        return self.payloads[self.current_input_index]

    def set_master_to_next_payload(self) -> dict:
        """Set the master to the next payload in the extraction order."""
        self.current_input_index += 1
        if self.current_input_index >= len(self.extraction_order):
            return {}
        self._ll_handler.switch_to_input(self.extraction_order[self.current_input_index])
        return self.payloads[self.current_input_index]

    def update_extraction_order(self) -> bool:
        """Update the extraction order.

        :return: True if the extraction order has changed
        :rtype: bool
        """
        self.extraction_order = []
        slave_index = 0
        for node, inteface in zip(self._input_nodes, self._input_interfaces, strict=True):
            # if a payload exists on the slave interface, add it to the extraction order
            if node.payload and node.payload["on_interface"] == inteface:
                self.extraction_order.append(slave_index)
            slave_index += 1
        # get the hash of the extraction order and compare it to the last computed hash
        phash = _get_dict_hash(self.extraction_order)
        if phash == self.extraction_order_hash:
            return False
        # a change has been detected
        logger.debug("Extraction order changed for switch node %s", self.name)
        self.extraction_order_hash = phash
        return True

    def update_payload(self) -> bool:
        """Update the payload.

        This update depends on the extraction order update and on the input payload.

        :return: True if the payload has changed
        :rtype: bool
        """
        self.payloads = {}
        for slave_index in self.extraction_order:
            # get the payload from the input node
            node = self._input_nodes[slave_index]
            self.payloads[slave_index] = node.payload.copy()
            # fix the interface
            self.payloads[slave_index]["on_interface"] = self._output_interface
            # attach the name of the input node to the payload
            self.payloads[slave_index]["from_node"] = self._input_nodes[slave_index].name
        phash = _get_dict_hash(self.payloads)
        if phash == self.payload_hash:
            return False
        # a change has been detected
        logger.debug("Payloads changed for switch node %s", self.name)
        self.payload_hash = phash
        return True
