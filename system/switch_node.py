"""Switch Node class for FIREQ system node representation."""

from __future__ import annotations

import logging

from FIREQ_LL_API import AXIStreamSwitchDriver

from ._generic_node import _GenericNode
from ._utils import _get_dict_hash

logger = logging.getLogger(__name__)


class SwitchNode(_GenericNode):
    """Object representing the acquisition IPs.

    Dict definition:
        _name: str, name of the switch instance
        _ll_handler: SwitchDriver, handler to the low level driver
    """

    nodetype = "data_switch"

    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _ll_handler: AXIStreamSwitchDriver,
    ) -> None:
        """Initialize the switch node.

        :param name: Name of the node
        :type name: str
        :param parent: Parent node
        :type parent: _GenericNode
        :param _ll_handler: Low level handler
        :type _ll_handler: AXIStreamSwitchDriver
        """
        super().__init__(name=name, parent=parent)
        self._ll_handler = _ll_handler
        # get the interface mapping and initialize the input nodes
        self._if_map = self.root.get_axi_stream_interface_map(self.name)
        self._input_interfaces = []
        self._output_interface = None
        for interface, if_id in self._if_map.items():
            if interface == "M_AXIS":
                self._output_interface = if_id
            else:
                # fix the order of input if here
                self._input_interfaces.append(if_id)
        # extraction order
        self.extraction_order = [False] * len(self._input_interfaces)
        self.extraction_order_hash = _get_dict_hash(self.extraction_order)
        self.root.register_update_function(f"{self._output_interface}/extraction_order", self.update_extraction_order)
        self.root.add_reference(f"{self._output_interface}/extraction_order", self.extraction_order)
        # payload, will be properly initialized in _build_dependencies
        self.payload = []
        self.root.add_reference(f"{self._output_interface}/payload", self.payload)
        # these parameters will become references

    def _build_dependencies(self) -> None:
        """Build the dependency for this node."""
        # the extraction order and the output payloads depend on the input nodes payloads
        # NOTE: in the future, if one wants to support input nodes like another switch, this would need to be changed
        self.root.add_dependency(
            self.root.make_func_label(self, "extraction_order"),
            depends_on=[f"{input_if}/payload" for input_if in self._input_interfaces],
        )
        for s_if in self._input_interfaces:
            # get the input payload and append it to the list
            self.payload.append(self.root.get_reference(f"{s_if}/payload"))

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
        for packet in self.payload:
            # if a payload exists on the slave interface, add it to the extraction order
            if packet:
                self.extraction_order[slave_index] = True
            else:
                self.extraction_order[slave_index] = False
            # FIXME: ORDER IS NOT GUARANTEED, PAYLOADS MAY NOT BE IN ORDER
            slave_index += 1
        # get the hash of the extraction order and compare it to the last computed hash
        phash = _get_dict_hash(self.extraction_order)
        # FIXME: i don't think this hash works
        if phash == self.extraction_order_hash:
            return False
        # a change has been detected
        logger.debug("Extraction order changed for switch node %s", self.name)
        self.extraction_order_hash = phash
        return True


#    def update_payloads(self) -> bool:
#        """Update the payload.
#
#        This update depends on the extraction order update and on the input payload.
#
#        :return: True if the payload has changed
#        :rtype: bool
#        """
#        self.payloads = {}
#        for slave_index in self.extraction_order:
#            # get the payload from the input node
#            node = self._input_nodes[slave_index]
#            self.payloads[slave_index] = node.payload.copy()
#            # fix the interface
#            self.payloads[slave_index]["on_interface"] = self._output_interface
#            # attach the name of the input node to the payload
#            self.payloads[slave_index]["from_node"] = self._input_nodes[slave_index].name
#        phash = _get_dict_hash(self.payloads)
#        if phash == self.payload_hash:
#            return False
#        # a change has been detected
#        logger.debug("Payloads changed for switch node %s", self.name)
#        self.payload_hash = phash
#        return True
