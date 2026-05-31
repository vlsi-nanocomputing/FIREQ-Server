"""FIFO Node class for FIREQ system node representation."""

from __future__ import annotations

import logging

from _generic_node import _GenericNode

from ._utils import _get_dict_hash

logger = logging.getLogger(__name__)


class FIFONode(_GenericNode):
    """Object representing the acquisition IPs.

    Dict definition:
        _name: str, name of the trigger generator node/istance
        _size: int, size of the FIFO in bytes
        _input_node: _GenericNode, input node of the FIFO
        _input_interface: str, name of the input interface for this node
        _output_interface: str, name of the output interface of this node
    """

    nodetype = "acquisition"

    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _size: int,
        _input_node: _GenericNode,
        _output_interface: str,
        _input_interface: str,
    ) -> None:
        """Initialize the FIFO node.

        :param name: Name of the node
        :type name: str
        :param parent: Parent node
        :type parent: _GenericNode
        :param _size: Size of the FIFO in bytes
        :type _size: int
        :param _input_node: Input node to this FIFO
        :type _input_node: _GenericNode
        :param _output_interface: Name of the output interface of this node
        :type _output_interface: str
        :param _input_interface: Name of the input interface of this node
        :type _input_interface: str
        """
        super().__init__(name=name, parent=parent)
        self._size = _size
        self._input_node = _input_node
        self._input_interface = _input_interface
        self._output_interface = _output_interface
        # the number of shots that this fifo can handle
        self.max_hw_shots = None
        # the number of hw shots to make
        self.hw_shots = None
        # this node output payload and payload hash
        self.payload = {}
        self.payload_hash = _get_dict_hash(self.payload)
        # register update functions
        self.parent.register_update_function(self.root.make_func_label(self, "max_hw_shots"), self.update_max_hw_shots)
        self.parent.register_update_function(self.root.make_func_label(self, "payload"), self.update_payload)

    def _build_dependencies(self) -> None:
        """Build the dependency for this node."""
        # the FIFO depends on the input node payload to compute the maximum number of shots
        # and on the trigger generator because the payload changes depending on the number of shots
        self.parent.add_dependency(
            self.root.make_func_label(self, "max_hw_shots"),
            depends_on=self.root.make_func_label(self._input_node, "payload"),
        )
        self.parent.add_dependency(
            self.root.make_func_label(self, "payload"),
            depends_on=[
                self.root.make_func_label(self.root, "hw_shots"),
                self.root.make_func_label(self._input_node, "payload"),
            ],
        )

    def update_max_hw_shots(self) -> bool:
        """Calculate the maximum number of shots that can be stored in the FIFO."""
        # check if the dict is empty that the payload is in the correct interface
        input_payload = self._input_node.payload
        if input_payload and input_payload["on_interface"] == self._input_interface:
            # this division should never be by zero because of how the payload is actually created
            mshots = self._size // input_payload["size"]
        else:
            mshots = None
        # check if the previous and new number are the same
        if mshots == self.max_hw_shots:
            return False
        self.max_hw_shots = mshots
        logger.debug("Max hw shots changed for FIFO node %s", self.name)
        return True

    def update_payload(self) -> bool:
        """Update FIFO payload based on the input node payload and the number of shots."""
        # compute the payload
        input_payload = self._input_node.payload
        if input_payload and input_payload["on_interface"] == self._input_interface:
            self.payload = {
                "size": self.hw_shots * input_payload["size"],
                "on_interface": self._output_interface,
            }
        else:
            self.payload = {}
        # check if the payload has changed
        new_hash = _get_dict_hash(self.payload)
        if new_hash == self.payload_hash:
            return False
        self.payload_hash = new_hash
        logger.debug("Payload changed for FIFO node %s", self.name)
        return True
