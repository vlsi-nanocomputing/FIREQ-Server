"""Low-level driver for the FIREQ acquisition IP."""

from typing import Any

from ._utils import _compute_pinc_poff, _FIREQDriver, _set_bit, _set_bits

__all__ = ["AcquisitionDriver"]


class AcquisitionDriver(_FIREQDriver):
    """Driver class that controls the FIREQ Acquisition IP.

    Provides methods to define acquisition behaviour and output type (when applicable).
    """

    bindto = ["user.org:user:axisAcquisitionIP:1.0"]

    def __init__(self, description: dict[str, Any]) -> None:
        """Initialize the AcquisitionDriver with the given description.

        :param description: Dictionary containing IP parameters and configuration
        :type description: dict
        """
        super().__init__(description=description)
        # maximum acquisition duration in clock cycles
        self.duration_width = int(description["parameters"]["DurationWidth"])
        self.maximum_duration = pow(2, self.duration_width)
        # size of the samples in bits
        self.sample_size = int(description["parameters"]["SampleSize"])
        # parallelism of the acquisition in number of samples
        self.log_number_of_channels = int(description["parameters"]["LogNsamplesClock"])
        self.number_of_channels = pow(2, self.log_number_of_channels)
        # depth of the phase increment and offset in bits
        self.phase_depth = int(description["parameters"]["PhaseDepth"])
        # number of triggers on the input trigger channel
        self.trigger_channels = int(description["parameters"]["TriggerWordWidth"])
        # maximum time of flight delay in clock cycles
        self.time_of_flight_width = int(description["parameters"]["TimeOfFlightCounterWidth"])
        self.time_of_flight_max = pow(2, self.time_of_flight_width)
        # not decimated output width in bits
        self.non_decimated_output_width = int(description["parameters"]["C_M00_AXIS_TDATA_WIDTH"])
        # decimated output width in bits
        self.decimated_output_width = int(description["parameters"]["C_M01_AXIS_TDATA_WIDTH"])

        # Register offset definitions
        self._ctrl = 0
        self._readout_inc_l = 3
        self._readout_inc_h = 4
        self._readout_off_l = 1
        self._readout_off_h = 2

        # Bit position definitions
        self._manual_trigger_pos = 31
        self._accumulate_select_pos = 27

    def print_description(self) -> None:
        """Print the driver configuration parameters to stdout."""
        print("maximum_duration: " + str(self.maximum_duration) + ", maximum duration of acquisition in clock cycles")
        print("sample_size: " + str(self.sample_size) + ", width of samples (bits)")
        print(
            "number_of_channels: "
            + str(self.number_of_channels)
            + ", parallelism of the acquisition (samples/clock cycle)"
        )
        print("phase_depth: " + str(self.phase_depth) + ", width of phases (bits)")
        print(
            "trigger_channels: "
            + str(self.trigger_channels)
            + ", number of trigger channels for readout and drive (bits)"
        )
        print("time_of_flight_width: " + str(self.time_of_flight_width) + ", width of the time of flight timer (bits)")

    def init_axi_lite_interface(self, base_address: int, axi_depth: int) -> None:
        """Initialize the AXI Lite interface for register access.

        :param base_address: Base address of the AXI Lite interface
        :type base_address: int
        :param axi_depth: Depth of the AXI Lite address space
        :type axi_depth: int
        """
        super().init_axi_lite_interface(base_address, axi_depth)
        # delete the mmio object created by PYNQ
        del self.mmio

    def set_acquisition_dds_parameters(self, frequency: float, phase: float, adc_samplerate: float) -> int:
        """Set acquisition demodulation parameters.

        :param frequency: Frequency of the demodulation signal in MHz
        :type frequency: float
        :param phase: Phase offset of the demodulation signal in RADs
        :type phase: float
        :param adc_samplerate: Sampling frequency of the ADC in MHz
        :type adc_samplerate: float
        :return: Error code (0 on success)
        :rtype: int
        :raises ValueError: If frequency is negative
        """
        if frequency < 0:
            raise ValueError("Frequency must be non-negative")

        # get poff and pinc
        phase_parameters = _compute_pinc_poff(frequency * 1000000, phase, adc_samplerate, self.phase_depth)

        # masking off the LSB of the phases. This is done in an effort to keep the generation and acquisition in phase.
        # Generation is done (at the DAC) at a frequency that is double the one used for the ADC. As a result, the phase
        # increment for the acquisition (at equal modulation and demodulation frequencies) is equal to double the one
        # of the generation. Masking the LSB accounts for the truncation done in the generation portion, and keeps the
        # two systems in sync.
        pinc = phase_parameters[0] & (2**self.phase_depth - 2)
        poff = phase_parameters[1] & (2**self.phase_depth - 2)

        # write registers
        self._set_readout_pinc_poff(pinc, poff)

        return 0

    def _set_readout_pinc_poff(self, inc: int, off: int) -> int:
        """Set readout increment and offset values.

        :param inc: Increment value for readout
        :type inc: int
        :param off: Offset value for readout
        :type off: int
        :return: Error code (0 on success)
        :rtype: int
        """
        # write inc LOW
        self._axi_lite_interface_mmio.write(self._readout_inc_l * 4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self._axi_lite_interface_mmio.write(self._readout_inc_h * 4, inc >> 32)

        # write off LOW
        self._axi_lite_interface_mmio.write(self._readout_off_l * 4, off & 0xFFFFFFFF)
        # write off HIGH
        self._axi_lite_interface_mmio.write(self._readout_off_h * 4, off >> 32)

        return 0

    def trigger_manually(self) -> None:
        """Trigger the acquisition manually."""
        manual_trigger_mask = 1 << self._manual_trigger_pos
        control_register = self._axi_lite_interface_mmio.read(0) | manual_trigger_mask
        self._axi_lite_interface_mmio.write(0, control_register)

    def set_acquisition_duration(self, duration: int) -> int:
        """Set the acquisition duration.

        :param duration: Duration in clock cycles
        :type duration: int
        :return: Error code (0 on success)
        :rtype: int
        :raises ValueError: If duration is out of valid range
        """
        if duration < 1 or duration > self.maximum_duration:
            raise ValueError(f"Acquisition duration must be between 1 and {self.maximum_duration}")

        control_register = self._axi_lite_interface_mmio.read(self._ctrl * 4)
        control_register = _set_bits(control_register, self.trigger_channels, self.duration_width, duration - 1)
        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)
        return 0

    def set_trigger_channel(self, channel: int) -> int:
        """Set the readout trigger channel.

        :param channel: Channel selection, set to 0 to deactivate external triggers
        :type channel: int
        :return: Error code (0 on success)
        :rtype: int
        :raises ValueError: If channel is out of valid range
        """
        if channel < 0 or channel > self.trigger_channels:
            raise ValueError(f"Channel must be between 0 and {self.trigger_channels}")

        channel_mask = (1 << channel) >> 1
        control_register = self._axi_lite_interface_mmio.read(self._ctrl * 4)
        control_register = _set_bits(control_register, 0, self.trigger_channels, channel_mask)
        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)
        return 0

    def set_time_of_flight(self, time_of_flight: int) -> int:
        """Set time of flight.

        :param time_of_flight: Time of flight in clock cycles
        :type time_of_flight: int
        :return: Error code (0 on success)
        :rtype: int
        :raises ValueError: If time_of_flight is out of valid range
        """
        if time_of_flight < 1 or time_of_flight > self.time_of_flight_max:
            raise ValueError(f"Time of flight must be between 1 and {self.time_of_flight_max}")

        control_register = self._axi_lite_interface_mmio.read(self._ctrl * 4)
        control_register = _set_bits(
            control_register,
            self.trigger_channels + self.duration_width,
            self.time_of_flight_width,
            time_of_flight - 1,
        )
        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)
        return 0

    def set_decimated_output_type(self, output_type: str) -> int:
        """Set the type of output data of the decimated stream.

        Can be set to output the decimated samples or the accumulated values.

        :param output_type: Selection, allowed values are 'decimated' and 'accumulated'
        :type output_type: str
        :return: Error code (0 on success)
        :rtype: int
        :raises ValueError: If output_type is not a valid option
        """
        if output_type == "decimated":
            output_mode_bit = 0
        elif output_type == "accumulated":
            output_mode_bit = 1
        else:
            raise ValueError("Invalid output_type. Allowed values are 'decimated' and 'accumulated'")

        updated_control = _set_bit(
            self._axi_lite_interface_mmio.read(self._ctrl * 4),
            self._accumulate_select_pos,
            output_mode_bit,
        )
        self._axi_lite_interface_mmio.write(self._ctrl * 4, updated_control)
        return 0
