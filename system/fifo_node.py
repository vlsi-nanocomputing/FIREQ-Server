"""FIFO Node class for FIREQ system node representation."""

from __future__ import annotations

import logging

from FIREQ_LL_API import FIFOWrapper

from ._generic_node import _GenericNode
from ._utils import _MutableRef

logger = logging.getLogger(__name__)


class FIFONode(_GenericNode):
    """Object representing an AXI-Stream FIFO.

    Dictionary definition for configuration:

    .. list-table::
       :header-rows: 1

       * - Key
         - Type
         - Description
       * - ``_name``
         - ``str``
         - Name of the FIFO node instance
       * - ``_ll_handler``
         - ``FIFOWrapper``
         - Low-level FIFO wrapper handler
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
        :param parent: Parent node in the system tree
        :type parent: _GenericNode
        :param _ll_handler: Low-level handler for the FIFO
        :type _ll_handler: FIFOWrapper
        """
        super().__init__(name=name, parent=parent)
        self._ll_handler = _ll_handler
        self._size: int = self._ll_handler.fifo_byte_size
        # get the interface mapping
        if_map = self.root.get_axi_stream_interface_map(self.name)
        self._input_interface = if_map["S_AXIS"]
        self._output_interface = if_map["M_AXIS"]
        # this node's maximum number of shots (before overflow) and output payload
        self.max_hw_shots = _MutableRef()
        self.payload = _MutableRef()
        self.max_payload_size = _MutableRef(value=self._size)
        # register update functions
        self.root.register_update_function(self.root.make_func_label(self, "max_hw_shots"), self.update_max_hw_shots)
        self.root.register_update_function(f"{self._output_interface}/payload", self.update_payload)
        # register the variables that other nodes may need
        self.root.add_reference(self.root.make_func_label(self, "max_hw_shots"), self.max_hw_shots)
        self.root.add_reference(f"{self._output_interface}/payload", self.payload)
        self.root.add_reference(f"{self._output_interface}/max_payload_size", self.max_payload_size)
        # the number of hw shots and input payload, these will be references to real values
        self._hw_shots = None
        self._input_payload = None

    def _build_dependencies(self) -> None:
        """Build the dependencies for this node.

        The FIFO depends on the input node payload to compute the maximum number
        of shots and on the trigger generator because the payload changes depending
        on the number of shots.
        """
        # the maximum number of shots is computed based on the input payload size
        self.root.add_dependency(
            self.root.make_func_label(self, "max_hw_shots"),
            depends_on=f"{self._input_interface}/payload",
        )
        # the output payload depends on the input payload and the number of hw shots
        self.root.add_dependency(
            f"{self._output_interface}/payload",
            depends_on=[
                self.root.make_func_label(self.root, "hw_shots"),
                f"{self._input_interface}/payload",
            ],
        )
        # resolve references from other nodes
        self._input_payload = self.root.get_reference(f"{self._input_interface}/payload")
        self._hw_shots = self.root.get_reference(self.root.make_func_label(self.root, "hw_shots"))
        logger.debug("Got input payload reference for FIFO node %s", self.name)

    def update_max_hw_shots(self) -> bool:
        """Calculate the maximum number of shots that can be stored in the FIFO.

        Depends on the input payload, and modifies :attr:`max_hw_shots`

        :return: ``True`` if the maximum number of hardware shots changed,
            ``False`` otherwise
        :rtype: bool
        """
        # check if the dict is empty and that the payload is in the correct interface
        if self._input_payload:
            self.max_hw_shots["value"] = self._size // self._input_payload["size"]
        else:
            self.max_hw_shots.clear()
        logger.debug("Recomputed max hw shots for FIFO node %s", self.name)
        return self.max_hw_shots.hash_and_compare()

    def update_payload(self) -> bool:
        """Update FIFO payload based on the input node payload and the number of shots.

        :return: ``True`` if the payload has changed since the last call,
            ``False`` otherwise
        :rtype: bool
        """
        # compute the payload
        self.payload.clear()
        if self._input_payload and self._hw_shots["value"]:
            self.payload["size"] = self._hw_shots["value"] * self._input_payload["size"]
            self.payload["source"] = self._input_payload["source"]
        logger.debug("Recomputed payload for FIFO node %s", self.name)
        return self.payload.hash_and_compare()
