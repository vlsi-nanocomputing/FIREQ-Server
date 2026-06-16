"""Low-level driver for the FIREQ trigger generator IP."""

import logging

from ._utils import _FIREQDriver

__all__ = ["TriggerGeneratorDriver"]


class TriggerGeneratorDriver(_FIREQDriver):
    """Driver class for the trigger generator IP.

    Provides methods to set the generation time of pulses and acquisition events.
    """

    bindto = ["user.org:user:axisTriggerGeneratorIP:1.0"]

    # Register offset definitions
    _ctrl = 0
    _experiment_dur_l = 2
    _experiment_dur_h = 3
    _readout_delay_l = 4
    _readout_delay_h = 5
    _shots_num_l = 1

    # Bit position definition
    _manual_trigger_pos = 31

    # Port name of the fabric clock
    fabric_clock_port = "HS_axi_clock"

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

    def print_description(self, printer_func: callable) -> None:
        """Print the description of the trigger generator IP.

        :param: printer_func: Function to use to print the description
        :type printer_func: callable
        """
        printer_func(f"trigger_channels: {self.trigger_channels}")
        printer_func(f"fifo_interface_axi_depth: {self.fifo_interface_memory_depth}")
        printer_func(f"fifo_channel_depth: {self.channel_fifo_depth}")
        printer_func(f"maximum_number_of_hardware_repetitions: {self.max_hw_repetitions}")

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

        self.log.debug("trigger, set_experiment_duration, got the following for duration: %s", duration)

        return 0

    def set_number_of_shots(self, value: int) -> int:
        """Set the number of shots to execute in hardware.

        :param value: Number of shots (must be between 1 and max_hw_repetitions)
        :type value: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if value < 1 or value > self.max_hw_repetitions:
            self.log.error("number of shots %s is outside of range 1 to %s", value, self.max_hw_repetitions)
            return -3

        self._axi_lite_interface_mmio.write(self._shots_num_l * 4, int(value - 1))

        self.log.debug("Set the number of hw shots to: %s", value)

        return 0

    def start_experiment(self) -> None:
        """Start the generation of triggers."""
        self._axi_lite_interface_mmio.write(0, 1 << self._manual_trigger_pos)

        self.log.debug("Trigger generator started")

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
            self.log.error("channel %s is outside of range 1 to %s", channel, self.trigger_channels)
            return -3

        if index < 1 or index > self.channel_fifo_depth:
            self.log.error("index %s is outside of range 1 to %s", index, self.channel_fifo_depth)
            return -3

        if delay < 1 or delay > self.drive_delay_max:
            self.log.error("delay %s is outside of range 1 to %s", delay, self.drive_delay_max)
            return -3

        real_delay = (delay - 1) | (generate_trigger << 31)
        real_address = (channel - 1) * self.channel_fifo_depth + index - 1
        self._axi_full_interface_mmio.write(real_address * 4, int(real_delay))

        self.log.debug(
            "set channel: %s, index: %s and delay: %s, " "generate_trigger: %s",
            channel,
            index,
            delay,
            generate_trigger,
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
            self.log.error("channel %s is outside of range 1 to %s", channel, self.trigger_channels)
            return -3
        # write delay LOW
        self._axi_lite_interface_mmio.write((self._readout_delay_l + (channel - 1) * 2) * 4, delay & 0xFFFFFFFF)
        # write delay HIGH
        self._axi_lite_interface_mmio.write((self._readout_delay_h + (channel - 1) * 2) * 4, delay >> 32)

        self.log.debug("trigger, set_readout_delay, got the following for channel: %s, delay: %s", channel, delay)

        return 0
