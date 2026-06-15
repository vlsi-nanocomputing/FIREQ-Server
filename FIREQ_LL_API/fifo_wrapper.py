"""Wrapper class for AXI Stream FIFOs."""

import logging

__all__ = ["FIFOWrapper"]


class FIFOWrapper:
    """Wrapper class for AXI Stream FIFOs.

    Wraps and contains the IP specific parametrization of the FIFO.
    """

    bindto = ["xilinx.com:ip:axis_data_fifo:2.0"]

    def __init__(self, parameters: dict[str, str]) -> None:
        """Initialize the FIFO wrapper with the given parameters.

        :param parameters: Dictionary containing the parametrization of this FIFO
        :type parameters: dict[str,str]
        """
        self.log = logging.getLogger(__name__)
        self._data_width = int(parameters["C_AXIS_TDATA_WIDTH"])
        self._data_depth = int(parameters["C_FIFO_DEPTH"])

    def set_logger(self, new_logger: logging.Logger) -> None:
        """Set the logger for this object.

        :param new_logger: Logger object to use
        :type new_logger: logging.Logger
        """
        self.log = new_logger

    @property
    def fifo_byte_size(self) -> int:
        """Return the size of the FIFO in bytes."""
        return self._data_width * self._data_depth // 8
