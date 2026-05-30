from __future__ import annotations

import logging

import numpy as np
from pynq import allocate
from pynq.lib import DMA

from ._generic_node import _GenericNode

logger = logging.getLogger(__name__)


class DMANode(_GenericNode):
    # what does the dma need?
    # the dma should be able to automatically extract all experiment data in one pass
    # therefore, it should know: the maximum amount of data it can extract, depending on the fifos connected to it:
    #     this max should be the
    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _ll_handler: DMA,
        _input_node: _GenericNode,
        _max_buffer_size: int,
        _input_interface: str,
    ) -> None:
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
        """Initialize the DMA."""
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

    def transfer_all(self, queque):
        """Transfer all the data from the DMA.

        Returns false if no data has been transferred.
        """
        if not self._transfering:
            return False
        # if the input is a switch, transfer all payloads and then return true
        if self._is_switch_input:
            while True:
                # check for errors
                if self._ll_handler.recvchannel.error:
                    logger.error("DMA transfer error for node %s", self.name)
                    raise RuntimeError("DMA transfer error")
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
