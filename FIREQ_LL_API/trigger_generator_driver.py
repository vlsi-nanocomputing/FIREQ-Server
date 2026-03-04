"""Low-level driver for the FIREQ trigger generator IP."""

import logging

from ._utils import _FIREQDriver

logger = logging.getLogger(__name__)

__all__ = ["TriggerGeneratorDriver"]


class TriggerGeneratorDriver(_FIREQDriver):
    """Driver class for the trigger generator IP.

    Provides methods to set the generation time of pulses and acquisition events.
    """

    bindto = ["user.org:user:axisTriggerGeneratorIP:1.0"]

    def __init__(self, description: dict[str, object]) -> None:
        """Initialize the TriggerGeneratorDriver.

        :param description: Dictionary containing IP parameters and configuration
        :type description: dict
        """
        super().__init__(description=description)
        # parse the number of channels of the trigger generator
        self.trigger_channels = int(description["parameters"]["TriggerWordWidth"])
        # depth of the axi full interface, also equal to the total depth of internal memory mapped fifos
        self.fifo_interface_memory_depth = pow(2, int(description["parameters"]["C_S00_AXI_ADDR_WIDTH"]))
        # fifo depth in number of words
        self.channel_fifo_depth = pow(2, int(description["parameters"]["FifoAddressWidth"]))
        # fifo output width
        self.fifo_output_width = int(description["parameters"]["FifoOutputWidth"])
        # maximum drive delay
        self.drive_delay_max = pow(2, self.fifo_output_width - 1)
        # experiment max
        self.experiment_timer_max = pow(2, int(description["parameters"]["ExperimentTimerWidth"]))
        # parse the size of the repetition counter
        self.max_hw_repetitions = pow(2, int(description["parameters"]["RepetitionWidth"]))

        self._ctrl = 0
        self._experiment_dur_l = 2
        self._experiment_dur_h = 3
        self._readout_delay_l = 4
        self._readout_delay_h = 5
        self._shots_num_l = 1

        # Bit position definition
        self._manual_trigger_pos = 31

    def print_description(self) -> None:
        """Print the description of the trigger generator IP."""
        print(f"trigger_channels: {self.trigger_channels}")
        print(f"fifo_interface_axi_depth: {self.fifo_interface_memory_depth}")
        print(f"fifo_channel_depth: {self.channel_fifo_depth}")
        print(f"maximum_number_of_hardware_repetitions: {self.max_hw_repetitions}")

    def init_axi_full_interface(self, base_address: int, axi_depth: int) -> None:
        """Initialize the AXI Full interface for this IP.

        :param base_address: Base address of the AXI Full interface
        :type base_address: int
        :param axi_depth: Depth of the AXI interface in bytes
        :type axi_depth: int
        """
        super().init_axi_full_interface(base_address, axi_depth)

    def init_axi_lite_interface(self, base_address: int, axi_depth: int) -> None:
        """Initialize the AXI Lite interface for this IP.

        :param base_address: Base address of the AXI Lite interface
        :type base_address: int
        :param axi_depth: Depth of the AXI interface in bytes
        :type axi_depth: int
        """
        super().init_axi_lite_interface(base_address, axi_depth)
        # delete the mmio object created by PYNQ
        del self.mmio

    def set_experiment_duration(self, duration: int) -> None:
        """Set the experiment duration for a single shot.

        :param duration: Duration in clock cycles
        :type duration: int
        """
        # write duration LOW
        self._axi_lite_interface_mmio.write(self._experiment_dur_l * 4, duration & 0xFFFFFFFF)
        # write duration HIGH
        self._axi_lite_interface_mmio.write(self._experiment_dur_h * 4, duration >> 32)

        logger.debug(f"trigger, set_experiment_duration, got the following for duration: {duration}")

        return 0

    def set_number_of_shots(self, value: int) -> int:
        """Set the number of shots to execute in hardware.

        :param value: Number of shots (must be between 1 and max_hw_repetitions)
        :type value: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if value < 1 or value > self.max_hw_repetitions:
            print(f"number of shots {value} is outside of range 1 to {self.max_hw_repetitions}")
            return -3

        self._axi_lite_interface_mmio.write(self._shots_num_l * 4, int(value - 1))

        logger.debug(f"trigger, set_number_of_shots, got the following for shots: {value}")

        return 0

    def start_experiment(self) -> None:
        """Start the generation of triggers."""
        self._axi_lite_interface_mmio.write(0, 1 << self._manual_trigger_pos)

        logger.debug(f"trigger, started experiment")

        return 0

    def is_done(self) -> bool:
        """Check if the experiment is finished.

        :return: True if the experiment is finished, False if still running
        :rtype: bool
        """
        control_register = self._axi_lite_interface_mmio.read(0)
        return (control_register & 0x40000000) == 0x40000000

    def insert_drive_delay(self, channel: int, index: int, delay: int, generate_trigger: int) -> int:
        """Insert a delay value in the FIFO of a drive channel at index.

        The generate_trigger input is used to tell the trigger generator if a trigger
        should be generated at the end of the delay.

        :param channel: Drive channel (1 to trigger_channels)
        :type channel: int
        :param index: FIFO index (1 is the start)
        :type index: int
        :param delay: Delay in clock cycles (1 to drive_delay_max)
        :type delay: int
        :param generate_trigger: Generates a trigger if set to 1
        :type generate_trigger: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if channel < 1 or channel > self.trigger_channels:
            print(f"channel {channel} is outside of range 1 to {self.trigger_channels}")
            return -3

        if index < 1 or index > self.channel_fifo_depth:
            print(f"index {index} is outside of range 1 to {self.channel_fifo_depth}")
            return -3

        if delay < 1 or delay > self.drive_delay_max:
            print(f"delay {delay} is outside of range 1 to {self.drive_delay_max}")
            return -3

        real_delay = (delay - 1) | (generate_trigger << 31)
        real_address = (channel - 1) * self.channel_fifo_depth + index - 1
        self._axi_full_interface_mmio.write(real_address * 4, int(real_delay))

        logger.debug(
            f"trigger, insert_drive_delay, got the following for channel: {channel}, index: {index}, delay: {delay}, generate_trigger: {generate_trigger}"
        )

        return 0

    def set_readout_delay(self, delay: int, channel: int) -> int:
        """Set the readout delay for a specific channel.

        :param delay: Delay in clock cycles
        :type delay: int
        :param channel: Channel number (1 to trigger_channels)
        :type channel: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if channel < 1 or channel > self.trigger_channels:
            print(f"channel {channel} is outside of range 1 to {self.trigger_channels}")
            return -3
        # write delay LOW
        self._axi_lite_interface_mmio.write((self._readout_delay_l + (channel - 1) * 2) * 4, delay & 0xFFFFFFFF)
        # write delay HIGH
        self._axi_lite_interface_mmio.write((self._readout_delay_h + (channel - 1) * 2) * 4, delay >> 32)

        logger.debug(f"trigger, set_readout_delay, got the following for channel: {channel}, delay: {delay}")

        return 0
