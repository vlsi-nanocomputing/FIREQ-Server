"""DMA Node class for FIREQ system node representation."""

from __future__ import annotations

import copy
import logging
import queue

import numpy as np
from pynq import allocate
from pynq.lib import DMA

from ._generic_node import _GenericNode
from ._utils import _MutableRef

logger = logging.getLogger(__name__)


class DMANode(_GenericNode):
    """Object representing the DMA IP.

    Dictionary definition for configuration:

    .. list-table::
       :header-rows: 1

       * - Key
         - Type
         - Description
       * - ``_name``
         - ``str``
         - Name of the DMA node instance
       * - ``_ll_handler``
         - ``DMA``
         - Low-level DMA driver handler
    """

    nodetype = "dma"
    wraps = [DMA.__name__]

    def __init__(self, name: str, parent: _GenericNode, _ll_handler: DMA) -> None:
        """Initialize the DMA node.

        :param name: Name of the node
        :type name: str
        :param parent: Parent node in the system tree
        :type parent: _GenericNode
        :param _ll_handler: Low-level DMA driver handler
        :type _ll_handler: DMA
        """
        super().__init__(name=name, parent=parent)
        self._ll_handler = _ll_handler
        # get the interface mapping for the node
        self._if_map = self.root.get_axi_stream_interface_map(self.name)
        self._input_interface = self._if_map["S_AXIS_S2MM"]
        self._transferring: bool = False
        # these will be initialized later by _build_dependencies
        self._input_payload: _MutableRef | list[_MutableRef] = None
        self._max_payload_size: _MutableRef | None = None
        # buffer and other
        self._buffer: np.ndarray | None = None
        self._is_switch_input: bool = False
        self._switch_func: callable | None = None
        self._current_payload_index: int | None = None

    def _build_dependencies(self) -> None:
        """Build the dependencies for this node.

        Resolves input payload references and allocates the receive buffer.
        """
        # fetch the input payload and the maximum size of the input buffers
        self._input_payload = self.root.get_reference(f"{self._input_interface}/payload")
        self._max_payload_size = self.root.get_reference(f"{self._input_interface}/max_payload_size")
        # fetch the hw shots reference
        self._hw_shots = self.root.get_reference(self.root.make_func_label(self.root, "hw_shots"))
        # Do not allocate the buffer yet, since we do not know if the max payload size has a valid value
        # try to get the input switch node
        if isinstance(self._input_payload, list):
            self._switch_func = self.root.get_reference(f"{self._input_interface}/payload_switch_func")
            self._is_switch_input = True

    def init_dma(self) -> None:
        """Initialize the DMA transfer.

        Will start the transfer in the receive channel and optionally set the input of the
        switch if the input of the DMA is a switch.
        This also means that, for implementation reasons, this function must be called
        before any data is being sent to the input of the switch.
        Failure to do so may lead to unaligned payloads and DMA errors.

        :raises RuntimeError: If the DMA is already transferring
        """
        if self._buffer is None:
            if not self._max_payload_size:
                raise RuntimeError()
            self._buffer = allocate(shape=(self._max_payload_size["value"],), dtype=np.uint8)
        if self._transferring:
            self.log.error("DMA already transferring, cannot initialize")
            raise RuntimeError("DMA already transferring, cannot initialize")
        if not self._input_payload:
            self.log.warning("No payload to transfer for DMA node %s", self.name)
            raise RuntimeError("No payload to transfer for DMA node %s", self.name)
        # further checks and set the current payload
        if self._is_switch_input:
            for i, payload in enumerate(self._input_payload):
                if payload:
                    self._current_payload_index = i
                    # NOTE: adding one because the switch expects the first slave to be 1 not 0
                    self._switch_func(i + 1)
                    break
            else:
                # loop exhausted without break -> no valid payload found
                self.log.warning("No payload to transfer for DMA node %s", self.name)
                raise RuntimeError("No payload to transfer for DMA node %s", self.name)
        self._transferring = True
        # start the transfer
        self._ll_handler.recvchannel.transfer(self._buffer)

    def save_variables(self) -> None:
        """Save the variables needed for the transfer.

        This function must be called after the dma init_dma method and before the transfer_all method.
        After running this function, non-hw parameters in the dependency orchestrator can be changed.
        """
        self._saved_payload = copy.deepcopy(self._input_payload)
        self._saved_hw_shots = copy.deepcopy(self._hw_shots)

    def transfer_all(self, data_queue: queue.Queue) -> bool:
        """Transfer all available data from the DMA into the provided queue.

        This function expects saved variables to be set. This is done by the save_variables method.

        :param data_queue: Queue in which to put ``(source_name, data_array)`` tuples
        :type data_queue: queue.Queue
        :return: ``True`` if all transfers completed successfully, ``False`` on error
            or if no transfer was started
        :rtype: bool
        """
        if not self._transferring:
            return False
        # get the current payload
        current_payload = (
            self._saved_payload
            if self._current_payload_index is None
            else self._saved_payload[self._current_payload_index]
        )
        while True:
            # check for errors
            if self._ll_handler.recvchannel.error:
                self.log.error("DMA transfer error for node %s", self.name)
                return False
            # wait for the transfer to complete and put the data in the queue
            self._ll_handler.recvchannel.wait()
            data_queue.put(
                (
                    current_payload["source"],
                    self._saved_hw_shots["value"],
                    current_payload["format"],
                    self._buffer[: current_payload["size"]].tobytes(),
                )
            )
            # break the loop if the input is not a switch or if the current payload is the last
            if not self._is_switch_input or self._current_payload_index >= len(self._saved_payload) - 1:
                break
            # find the next valid input payload to transfer
            for i in range(self._current_payload_index + 1, len(self._saved_payload)):
                if self._saved_payload[i]:
                    self._current_payload_index = i
                    # NOTE: adding one because the switch expects the first slave to be 1 not 0
                    self._switch_func(i + 1)
                    current_payload = self._saved_payload[i]
                    break
            else:
                # no more valid payloads
                break
            self._ll_handler.recvchannel.transfer(self._buffer)
        # end of transfer
        self._current_payload_index = None
        self._transferring = False
        return True
