"""DMA Node class for FIREQ system node representation."""

from __future__ import annotations

import logging

import numpy as np
from pynq import allocate
from pynq.lib import DMA

from ._generic_node import _GenericNode

logger = logging.getLogger(__name__)


class DMANode(_GenericNode):
    """Object representing the DMA IPs.

    Dict definition:
        _name: str, name of the trigger generator node/istance
        _ll_handler: DMA, handler to the low level driver
        _input_node: _GenericNode, input node
        _max_buffer_size: int, max internal buffer size in bytes for all transfers
        _input_interface: str, input interface name
    """

    nodetype = "dma"

    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _ll_handler: DMA,
        _input_node: _GenericNode,
        _max_buffer_size: int,
        _input_interface: str,
    ) -> None:
        """Initialize the DMA node.

        :param name: Name of the node
        :type name:str
        :param parent: Parent node
        :type parent: _GenericNode
        :param _ll_handler: Low level handler
        :type _ll_handler: DMA
        :param _input_node: Input node
        :type _input_node: _GenericNode
        :param _max_buffer_size: Max buffer size in bytes
        :type _max_buffer_size: int
        :param _input_interface: Input interface name
        :type _input_interface: str
        """
        super().__init__(name=name, parent=parent)
        self._ll_handler = _ll_handler
        self._input_node = _input_node
        # if the input node is a switch, store in a flag
        if self._input_node.nodetype == "switch":
            self._is_switch_input = True
        else:
            self._is_switch_input = False
        # allocate buffer, knowing that the max size is in bytes
        self._transffering = False
        self._current_payload = {}
        self._buffer = allocate(shape=(_max_buffer_size,), dtype=np.uint8)
        self._input_interface = _input_interface

    def init_dma(self) -> bool:
        """Initialize the DMA.

        :return: True if the DMA has been initialized correctly
        :rtype: bool
        """
        # if the input is a switch, set the master to the first payload
        if self._is_switch_input:
            self.current_payload = self._input_node.set_master_to_first_payload()
        else:
            self.current_payload = self._input_node.payload
        # if the payload is empty, or the payload is on the wrong interface, do nothing
        if not self.current_payload or self.current_payload["on_interface"] != self._input_interface:
            self._transffering = False
            return False
        self._transffering = True
        # if the payload is not empty, start the transfer
        self._ll_handler.recvchannel.transfer(self._buffer)
        return True

    def transfer_all(self, queque) -> bool:
        """Transfer all the data from the DMA.

        Returns False on a transfer error, including no data transfer.

        :param queque: Queue to put the data in
        :type queque: queue.Queue
        :return: True if the transfer has been completed correctly
        :rtype: bool
        """
        if not self._transffering:
            return False
        # if the input is a switch, transfer all payloads and then return true
        if self._is_switch_input:
            while True:
                # check for errors
                if self._ll_handler.recvchannel.error:
                    logger.error("DMA transfer error for node %s", self.name)
                    return False
                self._ll_handler.recvchannel.wait()
                queque.put((self.current_payload["from_node"], self._buffer[: self.current_payload["size"]].copy()))
                self.current_payload = self._input_node.set_master_to_next_payload()
                if not self.current_payload or self.current_payload["on_interface"] != self._input_interface:
                    break
                self._ll_handler.recvchannel.transfer(self._buffer)
            return True
        # if the input is not a switch, transfer the single payload and return true
        self._ll_handler.recvchannel.wait()
        queque.put((self._input_node.name, self._buffer[: self.current_payload["size"]].copy()))
        return True
