"""Low-level driver for the FIREQ trigger generator IP."""

import logging

from pynq import DefaultIP

logger = logging.getLogger(__name__)

__all__ = ["AXIStreamSwitchDriver"]


class AXIStreamSwitchDriver(DefaultIP):
    """Driver class for the switch IP.

    Provides methods to set the direction of the switch.
    """

    bindto = ["xilinx.com:ip:axis_switch:1.1"]

    # register offsets
    _ctrl = 0x00
    _mi_mux = 0x40

    # mask to commit changes
    _commit_mask = 0x00000002

    def __init__(self, description: dict[str, object]) -> None:
        """Initialize the AXIStreamSwitchDriver.

        :param description: Dictionary containing IP parameters and configuration
        :type description: dict
        """
        super().__init__(description=description)
        self.number_of_slaves = int(description["parameters"]["NUM_SI"])
        self.number_of_masters = int(description["parameters"]["NUM_MI"])
        if self.number_of_masters > 1:
            raise NotImplementedError("only one master is supported for data switches")

    @property
    def slave_number_to_interface_map(self) -> dict:
        """The map between the slave number sand the slave interface name."""
        slave_map = {}
        for i in range(self.number_of_slaves):
            # the slave interface is S00_AXIS, S01_AXIS, S02_AXIS, etc.
            slave_map[i] = f"S{i:02d}_AXIS"
        return slave_map

    def switch_to_input(self, input_number: int = 0) -> int:
        """Switch the switch to the selected input.

        :param input_number: Input number to switch to
        :type input_number: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if input_number < 1 or input_number > self.number_of_slaves:
            logger.error(f"input number: {input_number} out of range")
            return -3

        self.mmio.write(self._mi_mux, input_number - 1)
        self.mmio.write(self._ctrl, self._commit_mask)
        logger.debug(f"switched to input {input_number}")

        return 0
