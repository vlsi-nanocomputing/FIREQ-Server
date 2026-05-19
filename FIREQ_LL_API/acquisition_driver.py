"""Low-level driver for the FIREQ acquisition IP."""

import logging
from typing import Any

from ._utils import _compute_pinc_poff, _FIREQDriver, _set_bit, _set_bits

__all__ = ["AcquisitionDriver"]

logger = logging.getLogger(__name__)


class AcquisitionDriver(_FIREQDriver):
    """Driver class that controls the FIREQ Acquisition IP.

    Provides methods to define acquisition behaviour and output type (when applicable).
    """

    bindto = ["user.org:user:axisAcquisitionIP:1.0"]

    # Register offset definitions
    _ctrl = 0
    _readout_inc_l = 3
    _readout_inc_h = 4
    _readout_off_l = 1
    _readout_off_h = 2

    # Bit position definitions
    _manual_trigger_pos = 31
    _accumulate_select_pos = 27

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

    def set_demodulation_frequency(self, frequency: float):
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
        logger.debug("set frequency to %s and phase increment to %s", frequency, pinc)

        return 0

    def set_demodulation_initial_phase(self, phase: float):
        """Set acquisition demodulation frequency.

        :param phase: Phase offset of the demodulation signal, normalized to pi
        :type frequency: float
        :return: Error code (0 on success)
        :rtype: int
        """

        # get poff and pinc
        phase_parameters = _compute_pinc_poff(0, phase, self._adc_samplerate, self.phase_depth)

        # TODO: check that this is ok even when moving away from the first nyquist zone.
        #       I am unsure if I have to invert the phase or not when in the second nyquist zone.
        poff = phase_parameters[1] & (2**self.phase_depth - 2)

        # write inc LOW
        self._axi_lite_interface_mmio.write(self._readout_off_l * 4, poff & 0xFFFFFFFF)
        # write inc HIGH
        self._axi_lite_interface_mmio.write(self._readout_off_h * 4, poff >> 32)

        logger.debug("set initial phase to %s and phase offset to %s", phase, poff)

        return 0

    def trigger_manually(self) -> None:
        """Trigger the acquisition manually."""
        manual_trigger_mask = 1 << self._manual_trigger_pos
        control_register = self._axi_lite_interface_mmio.read(0) | manual_trigger_mask
        self._axi_lite_interface_mmio.write(0, control_register)

        logger.debug("acquistion triggered manually")

        return 0

    def set_acquisition_duration(self, duration: int) -> int:
        """Set the acquisition duration.

        :param duration: Duration in fabric clock cycles
        :type duration: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if duration < 1 or duration > self.maximum_duration:
            print("acquisition duration is out of range")
            logger.error(f"acquistion duration: {duration} out of range")
            return -3

        control_register = self._axi_lite_interface_mmio.read(self._ctrl * 4)
        control_register = _set_bits(control_register, self.trigger_channels, self.duration_width, duration - 1)
        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)

        logger.debug("set the acquisition duration to %s fabric clock cycles", duration)

        return 0

    def set_trigger_channel(self, channel: int) -> int:
        """Set the readout trigger channel.

        :param channel: Channel selection, set to 0 to deactivate external triggers
        :type channel: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if channel < 0 or channel > self.trigger_channels:
            logger.error(f"channel choice: {channel} out of range")
            return -3

        channel_mask = (1 << channel) >> 1
        control_register = self._axi_lite_interface_mmio.read(self._ctrl * 4)
        control_register = _set_bits(control_register, 0, self.trigger_channels, channel_mask)
        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)

        logger.debug("set the trigger channel to %s", channel)

        return 0

    def set_time_of_flight(self, time_of_flight: int) -> int:
        """Set time of flight.

        :param time_of_flight: Time of flight in fabric clock cycles
        :type time_of_flight: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if time_of_flight < 1 or time_of_flight > self.time_of_flight_max:
            logger.error(f"time of flight: {time_of_flight} out of range")
            return -3

        control_register = self._axi_lite_interface_mmio.read(self._ctrl * 4)
        control_register = _set_bits(
            control_register,
            self.trigger_channels + self.duration_width,
            self.time_of_flight_width,
            time_of_flight - 1,
        )
        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)

        logger.debug("set the time of flight to %s fabric clock cycles", time_of_flight)

        return 0

    def set_decimated_output_type(self, output_type: str) -> int:
        """Set the type of output data of the decimated stream.

        Can be set to output the decimated samples or the accumulated values.

        :param output_type: Selection, allowed values are 'decimated' and 'accumulated'
        :type output_type: str
        :return: Error code (0 on success)
        :rtype: int
        """
        if output_type == "decimated":
            output_mode_bit = 0
        elif output_type == "accumulated":
            output_mode_bit = 1
        else:
            logger.error(f"output_type: {output_type} not recognized")
            return -3

        updated_control = _set_bit(
            self._axi_lite_interface_mmio.read(self._ctrl * 4),
            self._accumulate_select_pos,
            output_mode_bit,
        )
        self._axi_lite_interface_mmio.write(self._ctrl * 4, updated_control)

        logger.debug("set the decimated output type to %s", output_type)

        return 0
