"""FIFO Node class for FIREQ system node representation."""

from __future__ import annotations

import logging

from FIREQ_LL_API import FIFOWrapper

from ._generic_node import _GenericNode
from ._utils import _get_dict_hash

logger = logging.getLogger(__name__)


class FIFONode(_GenericNode):
    """Object representing the acquisition IPs.

    Dict definition:
        _name: str, name of the trigger generator node/istance
        _ll_handler: FIFOWrapper, handler to the low level driver
    """

    nodetype = "acquisition_fifo"
    wraps = [FIFOWrapper.__name__]

    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _ll_handler: FIFOWrapper,
    ) -> None:
        """Initialize the FIFO node.

        :param name: Name of the node
        :type name: str
        :param parent: Parent node
        :type parent: _GenericNode
        :param _ll_handler: Low level handler for the FIFO
        :type _ll_handler: FIFOWrapper
        """
        super().__init__(name=name, parent=parent)
        self._ll_handler = _ll_handler
        self._size = self._ll_handler.fifo_byte_size
        # get the interface mapping
        if_map = self.root.get_axi_stream_interface_map(self.name)
        self._input_interface = if_map["S_AXIS"]
        self._output_interface = if_map["M_AXIS"]
        # the number of hw shots and input payload, these will be references to real values
        self._hw_shots = None
        self._input_payload = None
        # the number of shots that this fifo can handle
        self._max_hw_shots = {}
        self._max_hw_shots_hash = _get_dict_hash(self._max_hw_shots)
        # this node output payload and payload hash
        self.payload = {}
        self.payload_hash = _get_dict_hash(self.payload)
        # register update functions
        self.root.register_update_function(self.root.make_func_label(self, "max_hw_shots"), self.update_max_hw_shots)
        self.root.register_update_function(f"{self._output_interface}/payload", self.update_payload)
        # regiter the variables that other nodes may need
        self.root.add_reference(self.root.make_func_label(self, "max_hw_shots"), self._max_hw_shots)
        self.root.add_reference(f"{self._output_interface}/payload", self.payload)
        self.root.add_reference(f"{self._output_interface}/max_data_payload", self.size)

    def _resolve_dependencies(self) -> None:
        """Build the dependency for this node."""
        # the FIFO depends on the input node payload to compute the maximum number of shots
        # and on the trigger generator because the payload changes depending on the number of shots
        self.parent.add_dependency(
            self.root.make_func_label(self, "max_hw_shots"),
            depends_on=self.root.make_func_label(f"{self._input_interface}/payload"),
        )
        # build the dependencies and save the refs
        self._hw_shots, self._input_payload = self.parent.add_dependency(
            f"{self._output_interface}/payload",
            depends_on=[
                self.root.make_func_label(self.root, "hw_shots"),
                self.root.make_func_label(f"{self._input_interface}/payload"),
            ],
        )
        # resolve inputs from other nodes
        self._input_payload = self.root.get_reference(f"{self._input_interface}/payload")
        logger.debug("Got input payload reference for FIFO node")
        self._hw_shots = self.root.get_reference(self.root.make_func_label(self.root, "hw_shots"))

    def update_max_hw_shots(self) -> bool:
        """Calculate the maximum number of shots that can be stored in the FIFO.

        Depends on the input payload.
        """
        # check if the dict is empty that the payload is in the correct interface
        if self._input_payload:
            # this division should never be by zero because of how the payload is actually created
            mshots = {"value": self._size // self._input_payload["size"]}
        else:
            mshots = {}
        new_hash = _get_dict_hash(mshots)
        # check if the previous and new number are the same
        if new_hash == self._max_hw_shots_hash:
            return False
        self._max_hw_shots_hash = new_hash
        self._max_hw_shots = mshots
        logger.debug("Max hw shots changed for FIFO node %s", self.name)
        return True

    def update_payload(self) -> bool:
        """Update FIFO payload based on the input node payload and the number of shots."""
        # compute the payload
        if self._input_payload:
            self.payload = {"size": self._hw_shots[0] * self._input_payload["size"]}
        else:
            self.payload = {}
        # check if the payload has changed
        new_hash = _get_dict_hash(self.payload)
        if new_hash == self.payload_hash:
            return False
        self.payload_hash = new_hash
        logger.debug("Payload changed for FIFO node %s", self.name)
        return True
