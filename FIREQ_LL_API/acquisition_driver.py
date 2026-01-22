from pynq import MMIO
import numpy as np
from ._utils import *
from typing import TextIO

__all__ = ['AcquisitionDriver']

class AcquisitionDriver(_FIREQDriver):
    """
    Driver class that controls the FIREQ Acquisition IP.
    Provides methods to define acquisition behaviour and output type (when applicable).
    """

    bindto = ['user.org:user:axisAcquistionIP:1.0']

    def __init__(self, description):
        super().__init__(description=description)
        # maximum acquistion duration in clock cycles
        self.duration_width = int(description["parameters"]["DurationWidth"])
        self.maximum_duration = pow(2, self.duration_width)
        # size of the samples in bits
        self.sample_size = int(description["parameters"]["SampleSize"])
        # parallelism of the acquistion in number of samples
        self.log_number_of_channels = int(description["parameters"]["LogNsamplesClock"])
        self.number_of_channels = pow(2, self.log_number_of_channels)
        # depth of the phase increment and offset in bits
        self.phase_depth = int(description["parameters"]["PhaseDepth"])
        # number of triggers on the input trigger channel
        self.trigger_channels = int(description['parameters']['TriggerWordWidth'])
        # maximum time of flight delay in clock cycles
        self.time_of_flight_width = int(description["parameters"]["TimeOfFlightCounterWidth"])
        self.time_of_flight_max = pow(2, self.time_of_flight_width)
        # not decimated output width in bits
        self.non_decimated_output_width = int(description["parameters"]["C_M00_AXIS_TDATA_WIDTH"])
        # decimated output width in bits
        self.decimated_output_width = int(description["parameters"]["C_M01_AXIS_TDATA_WIDTH"])

        # Register offset definitions
        self.ctrl = 0
        self.readout_inc_l = 3
        self.readout_inc_h = 4
        self.readout_off_l = 1
        self.readout_off_h = 2

        # Bit position definitions
        self.manual_trigger_pos = 31
        self.accumulate_select_pos = 27

    def print_description(self):
        print("maximum_duration: " + str(self.maximum_duration) + ", maximum duration of acquistion in clock cycles")
        print("sample_size: " + str(self.sample_size) + ", width of samples (bits)")
        print("number_of_channels: " + str(self.number_of_channels) + ", parallelism of the acquistion (samples/clock cycle)")
        print("phase_depth: " + str(self.phase_depth) + ", width of phases (bits)")
        print("trigger_channels: " + str(self.trigger_channels) + ", number of trigger channels for readout and drive (bits)")
        print("time_of_flight_width: " + str(self.time_of_flight_width) + ", width of the time of flight timer (bits)")

    def init_axi_lite_interface(self, base_address : int, axi_depth : int):
        super().init_axi_lite_interface(base_address, axi_depth)
        # delete the mmio object created by PYNQ
        del self.mmio

    def set_acquisition_dds_parameters(self, frequency, phase, adc_samplerate):
        """
        Set parameters for acquistion such as demodulation frequency, the phase offset of the demodulation
        
        :param frequency: Frequency of the demodulation signal in MHz
        :type frequency: float
        :param phase: Phase offset of the demodulation signal in RADs
        :type phase: float
        :param adc_samplerate: Sampling frequency of the ADC in MHz
        :type adc_samplerate: float
        :return: Error code
        :rtype: int
        """

        # check inputs
        if(frequency < 0):
            print("input parameters out of range")
            return -3

        # get poff and pinc
        phase_parameters = _compute_pinc_poff(frequency*1000000, phase, adc_samplerate, self.phase_depth)

        # this masking is due to the fact that the frequency of the dac is double. this prevents the ADC from
        # going out of phase wrt the generator which means that the readout channels will always be at a constant phase
        pinc = phase_parameters[0]&(2**self.phase_depth - 2)
        poff = phase_parameters[1]&(2**self.phase_depth - 2)

        # write registers
        self._set_readout_pinc_poff(pinc,poff)

        return 0
    
    def _set_readout_pinc_poff(self, inc, off):
        """
        Set readout increment and offset values

        :param inc: Increment value for readout
        :type inc: int
        :param off: Offset value for readout
        :type off: int
        :return: Error code
        :rtype: int
        """

        # write inc LOW
        self.AxiLiteInterfaceMMIO.write(self.readout_inc_l*4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self.AxiLiteInterfaceMMIO.write(self.readout_inc_h*4, inc >> 32)

        # write off LOW
        self.AxiLiteInterfaceMMIO.write(self.readout_off_l*4, off & 0xFFFFFFFF)
        # write off HIGH
        self.AxiLiteInterfaceMMIO.write(self.readout_off_h*4, off >> 32)

        return 0
    
    def trigger_manually(self):
        """
        trigger the acquisition manually
        """

        manual_trigger_mask = 0x80000000
        control_register = self.AxiLiteInterfaceMMIO.read(0) | manual_trigger_mask
        self.AxiLiteInterfaceMMIO.write(0, control_register)
        return
    
    def set_acquisition_duration(self, duration):
        """
        Set the acquistion duration

        :param duration: Duration in clock cycles
        :type duration: uint
        :return: Error Code
        :rtype: int
        """

        if (duration < 1 or duration > self.maximum_duration):
            print("acquistion duration is out of range")
            return -3

        control_register = self.AxiLiteInterfaceMMIO.read(self.ctrl*4)
        control_register = _set_bits(control_register, self.trigger_channels, self.duration_width, duration-1)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, control_register)
    
    def set_trigger_channel(self, channel):
        """
        set the readout trigger channel

        :param channel: channel selection, set to 0 to deactivate external triggers
        :type channel: uint
        :return: Error code
        :rtype: int
        """

        if channel < 0 or channel > self.trigger_channels:
            print("source choice is out of range")
            return -3

        channel_mask = (1 << channel) >> 1
        control_register = self.AxiLiteInterfaceMMIO.read(self.ctrl*4)
        control_register = _set_bits(control_register, 0, self.trigger_channels, channel_mask)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, control_register)
        return 0
    
    def set_time_of_flight(self, time_of_flight):
        """
        Set time of flight

        :param time_of_flight: Time of flight in clock cycles
        :type time_of_flight: uint
        :return: Error code
        :rtype: int
        """

        if (time_of_flight < 1 or time_of_flight > self.time_of_flight_max):
            print("time of flight is out of range")
            return -3

        control_register = self.AxiLiteInterfaceMMIO.read(self.ctrl*4)
        control_register = _set_bits(control_register, self.trigger_channels + self.duration_width, self.time_of_flight_width, time_of_flight-1)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, control_register)

    def set_decimated_output_type(self, type):
        """
        Sets the type of output data of the decimated stream.\n
        Can be set to output the decimated samples or the accumulated values.

        :param type: Selection, allowed values are 'decimated' and 'accumulated'
        :type type: str
        """

        output_mode_bit = None

        if type == 'decimated':
            output_mode_bit = 0
        elif type == 'accumulated':
            output_mode_bit = 1
        else:
            print("error, input value for type is not recognized, allowed values are 'decimated' and 'accumulated'")
            return -3

        updated_control = _set_bit(self.AxiLiteInterfaceMMIO.read(self.ctrl), self.accumulate_select_pos, output_mode_bit)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, updated_control)
        return 0
