"""Low-level driver for the FIREQ acquisition IP."""

from typing import Any

from ._utils import _FIREQDriver, _set_bit, _set_bits

__all__ = ["AcquisitionDriver"]


class AcquisitionDriver(_FIREQDriver):
    """Driver class that controls the FIREQ Acquisition IP.

    Provides methods to define acquisition behaviour and output type (when applicable).
    """

    bindto = ["user.org:user:axisAcquisitionIP:1.0"]

    # Register offset definitions
    _ctrl = 0
    _readout_off_l = 1
    _readout_off_h = 2
    _readout_inc_l = 3
    _readout_inc_h = 4
    _trigger_mask_l = 5

    # Bit position definitions
    _manual_trigger_pos = 31
    _raw_enable_pos = 27
    _accumulated_decimated_enable_pos = 26
    _accumulate_select_pos = 25
    _duration_pos = 0
    _trigger_mask_pos = 0

    # Output interfaces mapping the acquisition mode to the interface responsable for the output
    _output_interfaces = {
        "raw": "m00_axis",
        "decimated": "m01_axis",
        "accumulated": "m01_axis",
    }

    # Formats of the raw bytes coming from the buffers
    _formats = {
        "raw": (("real", "<i2"), ("imag", "<i2")),
        "decimated": (("real", "<i2"), ("imag", "<i2")),
        "accumulated": (("real", "<i4"), ("imag", "<i4")),
    }

    # Port name of the fabric clock
    fabric_clock_port = "HS_axi_clock"

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
        # payload of the acquisition, calculated depending on the parameters set
        # should be recalculated any time that the duration, output mode and trigger channel are set
        self._cache = {
            "active": False,
            "duration": 0,
            "output_mode": "raw",
        }
        self.payload = {}

    def _calculate_payload(self) -> None:
        """Calculate the payload of the acquisition for a single shot."""
        if self._cache["active"] and self._cache["duration"] > 0:
            if self._cache["output_mode"] == "raw":
                self.payload["size"] = self._cache["duration"] * self.non_decimated_output_width // 8
            elif self._cache["output_mode"] == "decimated":
                self.payload["size"] = ((self._cache["duration"] + 1) // 2) * self.decimated_output_width // 8
            elif self._cache["output_mode"] == "accumulated":
                self.payload["size"] = self.decimated_output_width // 8
            self.payload["on_interface"] = self._output_interfaces[self._cache["output_mode"]]
            self.payload["format"] = self._formats[self._cache["output_mode"]]
        else:
            self.payload = {}

    def print_description(self, printer_func: callable) -> None:
        """Print a detailed description of the acquisition IP configuration parameters.

        :param printer_func: Function to use to print the description
        :type printer_func: callable
        """
        printer_func(
            "maximum_duration: " + str(self.maximum_duration) + ", maximum duration of acquisition in clock cycles"
        )
        printer_func("sample_size: " + str(self.sample_size) + ", width of samples (bits)")
        printer_func(
            "number_of_channels: "
            + str(self.number_of_channels)
            + ", parallelism of the acquisition (samples/clock cycle)"
        )
        printer_func("phase_depth: " + str(self.phase_depth) + ", width of phases (bits)")
        printer_func(
            "trigger_channels: "
            + str(self.trigger_channels)
            + ", number of trigger channels for readout and drive (bits)"
        )
        printer_func(
            "time_of_flight_width: " + str(self.time_of_flight_width) + ", width of the time of flight timer (bits)"
        )

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

    def set_demodulation_frequency(self, frequency: float) -> int:
        """Set acquisition demodulation frequency.

        :param frequency: Frequency of the demodulation signal, normalized to the ADC sampling frequency
        :type frequency: float
        :return: Error code (0 on success)
        :rtype: int
        """
        normalized_frequency = frequency % 1.0

        # compute the phase increment
        pinc = int(2**self.phase_depth * normalized_frequency)

        # masking off the LSB of the phases. This is done in an effort to keep the generation and acquisition in phase.
        # Generation is done (at the DAC) at a frequency that is double the one used for the ADC. As a result, the phase
        # increment for the acquisition (at equal modulation and demodulation frequencies) is equal to double the one
        # of the generation. Masking the LSB accounts for the truncation done in the generation portion, and keeps the
        # two systems in sync.
        pinc = pinc & (2**self.phase_depth - 2)

        # write inc LOW
        self._axi_lite_interface_mmio.write(self._readout_inc_l * 4, pinc & 0xFFFFFFFF)
        # write inc HIGH
        self._axi_lite_interface_mmio.write(self._readout_inc_h * 4, pinc >> 32)

        # log with deferred formatting string arguments to avoid eager evaluation
        self.log.debug("set frequency to %s and phase increment to %s", frequency, pinc)

        return 0

    def set_demodulation_initial_phase(self, normalized_phase: float) -> int:
        """Set acquisition demodulation frequency.

        :param normalized_phase: Phase offset of the demodulation signal, normalized to 2pi
        :type normalized_phase: float
        :return: Error code (0 on success)
        :rtype: int
        """
        # compute the phase increment
        poff = int(2**self.phase_depth * normalized_phase)

        # masking off the LSB of the phases. This is done in an effort to keep the generation and acquisition in phase.
        # Generation is done (at the DAC) at a frequency that is double the one used for the ADC. As a result, the phase
        # increment for the acquisition (at equal modulation and demodulation frequencies) is equal to double the one
        # of the generation. Masking the LSB accounts for the truncation done in the generation portion, and keeps the
        # two systems in sync.
        # TODO: check that this is ok even when moving away from the first nyquist zone.
        #       I am unsure if I have to invert the phase or not when in the second nyquist zone.
        poff = poff & (2**self.phase_depth - 2)

        # write inc LOW
        self._axi_lite_interface_mmio.write(self._readout_off_l * 4, poff & 0xFFFFFFFF)
        # write inc HIGH
        self._axi_lite_interface_mmio.write(self._readout_off_h * 4, poff >> 32)
        self.log.debug("set initial phase to %s and phase offset to %s", normalized_phase, poff)

        return 0

    def trigger_manually(self) -> int:
        """Trigger the acquisition manually."""
        manual_trigger_mask = 1 << self._manual_trigger_pos
        control_register = self._axi_lite_interface_mmio.read(0) | manual_trigger_mask

        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)
        self.log.debug("acquisition triggered manually")

        return 0

    def set_acquisition_duration(self, duration: int) -> int:
        """Set the acquisition duration.

        :param duration: Duration in fabric clock cycles
        :type duration: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if duration < 1 or duration > self.maximum_duration:
            self.log.error("acquisition duration: %s out of range", duration)
            return -3

        control_register = self._axi_lite_interface_mmio.read(self._ctrl * 4)
        control_register = _set_bits(control_register, self._duration_pos, self.duration_width, duration - 1)

        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)
        self.log.debug("set the acquisition duration to %s fabric clock cycles", duration)
        self._cache["duration"] = duration
        self._calculate_payload()

        return 0

    def set_trigger_channel(self, channel: int) -> int:
        """Set the readout trigger channel.

        :param channel: Channel selection, set to 0 to deactivate external triggers
        :type channel: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if channel < 0 or channel > self.trigger_channels:
            self.log.error("channel choice: %s out of range", channel)
            return -3

        channel_mask = (1 << channel) >> 1
        trigger_mask_reg = self._axi_lite_interface_mmio.read(self._trigger_mask_l * 4)
        trigger_mask_reg = _set_bits(trigger_mask_reg, self._trigger_mask_pos, self.trigger_channels, channel_mask)

        self._axi_lite_interface_mmio.write(self._trigger_mask_l * 4, trigger_mask_reg)
        self.log.debug("set the acquisition trigger channel to %s", channel)
        self._cache["active"] = channel > 0
        self._calculate_payload()

        return 0

    def set_time_of_flight(self, time_of_flight: int) -> int:
        """Set time of flight.

        :param time_of_flight: Time of flight in fabric clock cycles
        :type time_of_flight: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if time_of_flight < 1 or time_of_flight > self.time_of_flight_max:
            self.log.error("time of flight: %s out of range", time_of_flight)
            return -3

        control_register = self._axi_lite_interface_mmio.read(self._ctrl * 4)
        control_register = _set_bits(
            control_register,
            self._duration_pos + self.duration_width,
            self.time_of_flight_width,
            time_of_flight - 1,
        )

        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)
        self.log.debug("set the time of flight to %s fabric clock cycles", time_of_flight)

        return 0

    def set_output_mode(self, output_mode: str) -> int:
        """Set the type of output data of the decimated stream.

        Can be set to output the decimated samples or the accumulated values.

        :param output_type: Selection, allowed values are 'raw', 'decimated' and 'accumulated'
        :type output_type: str
        :return: Error code (0 on success)
        :rtype: int
        """
        if output_mode == "raw":
            self._cache["output_mode"] = "raw"
            raw_enable = 1
            acc_dec_enable = 0
            acc_dec_mode = 0
        elif output_mode == "decimated":
            self._cache["output_mode"] = "decimated"
            raw_enable = 0
            acc_dec_enable = 1
            acc_dec_mode = 0
        elif output_mode == "accumulated":
            self._cache["output_mode"] = "accumulated"
            raw_enable = 0
            acc_dec_enable = 1
            acc_dec_mode = 1
        else:
            self.log.error("output_type: %s not recognized", output_mode)
            return -3

        # set the bits relative to the interface enable and decimated output mode
        ctl = _set_bit(
            self._axi_lite_interface_mmio.read(self._ctrl * 4),
            self._accumulate_select_pos,
            acc_dec_mode,
        )
        ctl = _set_bit(
            ctl,
            self._accumulated_decimated_enable_pos,
            acc_dec_enable,
        )
        ctl = _set_bit(
            ctl,
            self._raw_enable_pos,
            raw_enable,
        )

        # write the control register
        self._axi_lite_interface_mmio.write(self._ctrl * 4, ctl)
        self.log.debug("set the output mode to %s, ctl: %s", output_mode, hex(ctl))
        self._calculate_payload()

        return 0
