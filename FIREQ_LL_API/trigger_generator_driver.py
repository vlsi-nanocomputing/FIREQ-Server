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
        # self.fifo_output_width = int(description["parameters"]["DriveTimerWidth"])
        # maximum drive delay
        self.drive_delay_max = pow(2, int(description["parameters"]["DriveTimerWidth"]))
        # experiment max
        self.experiment_timer_max = pow(2, int(description["parameters"]["ExperimentTimerWidth"]))
        # parse the size of the repetition counter
        self.max_hw_repetitions = pow(2, int(description["parameters"]["RepetitionWidth"]))

<<<<<<< HEAD
    def print_description(self, printer_func: callable) -> None:
        """Print the description of the trigger generator IP.

        :param: printer_func: Function to use to print the description
        :type printer_func: callable
        """
        printer_func(f"trigger_channels: {self.trigger_channels}")
        printer_func(f"fifo_interface_axi_depth: {self.fifo_interface_memory_depth}")
        printer_func(f"fifo_channel_depth: {self.channel_fifo_depth}")
        printer_func(f"maximum_number_of_hardware_repetitions: {self.max_hw_repetitions}")
=======
        # set up logger
        self.log = logging.getLogger(__name__)

    def print_description(self) -> None:
        """Print the description of the trigger generator IP."""
        print(f"trigger_channels: {self.trigger_channels}")
        print(f"fifo_interface_axi_depth: {self.fifo_interface_memory_depth}")
        print(f"fifo_channel_depth: {self.channel_fifo_depth}")
        print(f"maximum_number_of_hardware_repetitions: {self.max_hw_repetitions}")
>>>>>>> 02ec9ed (Modified trigger generator driver: support for multi-trigger drive/readout)

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


    def insert_delay(self, source: int, type: bool, index: int, delay: int) -> int:
        """Insert a delay and source value in the FIFO at the index for readout or drive.

        The source input is used to tell the trigger generator which channel should
        be triggered.

        :param source: Source channel (Fifthen bit mask, e.g. 0b0010 means channel 2)
        :type source: int
        :param type: Delay and trigger type (True for readout, False for drive)
        :type type: bool
        :param index: FIFO index (1 is the start)
        :type index: int
        :param delay: Delay in clock cycles (1 to drive_delay_max)
        :type delay: int
        :return: Error code (0 on success)
        :rtype: int
        """

        if source < 0 or source > pow(2,self.trigger_channels) - 1:
            self.log.error(f"source {source} is outside of range 0 to {pow(2,self.trigger_channels) - 1}")
            return -3

        if index < 1 or index > self.channel_fifo_depth // 2:
            self.log.error(f"index {index} is outside of range 1 to {self.channel_fifo_depth // 2}")
            return -3

        if delay < 1 or delay > self.drive_delay_max:
            self.log.error(f"delay {delay} is outside of range 1 to {self.drive_delay_max}")

            return -3
        
        
        source_index = (index * 2) + 0
        delay_index = (index * 2) + 1

        mask = (int(type) << 15) | (source & 0x7FFF)
        
        self._axi_full_interface_mmio.write(delay_index * 4, int(delay)-1)
        self._axi_full_interface_mmio.write(source_index * 4, int(mask))


        self.log.debug(
            "trigger, insert__delay, got the following for source: %s, type: %s, index: %s, delay: %s, ",
            source,
            type,

            index,
            delay,
        )

        return 0