from pynq import MMIO
import numpy as np
from ._utils import *

__all__ = ['AcquisitionDriver']

class AcquisitionDriver(_FIREQDriver):

    bindto = ['user.org:user:axisAcquistionIP:1.0']

    def __init__(self, description):
        super().__init__(description=description)
        # maximum acquistion duration
        self.DurationWidth = int(description["parameters"]["DurationWidth"])
        self.MaximumDuration = pow(2,self.DurationWidth)
        # size of the samples
        self.SampleSize = int(description["parameters"]["SampleSize"])
        # parallelism of the acquistion
        self.LogNumberOfChannels = int(description["parameters"]["LogNsamplesClock"])
        self.NumberOfChannels = pow(2,self.LogNumberOfChannels)
        # depth of the phase increment and offset
        self.PhaseDepth = int(description["parameters"]["PhaseDepth"])
        # number of triggers on the input trigger channel
        self.TriggerChannels = int(description['parameters']['TriggerWordWidth'])
        # maximum time of flight delay
        self.TimeOfFlightWidth = int(description["parameters"]["TimeOfFlightCounterWidth"])
        self.TimeOfFlightMax = pow(2,self.TimeOfFlightWidth)
        # not decimated output width
        self.NDCMT_OutputWidth = int(description["parameters"]["C_M00_AXIS_TDATA_WIDTH"])
        # decimated output width
        self.DCMT_OutputWidth = int(description["parameters"]["C_M01_AXIS_TDATA_WIDTH"])


        # Reg definition
        self.ctrl = 0
        self.readout_inc_l = 3
        self.readout_inc_h = 4
        self.readout_off_l = 1
        self.readout_off_h = 2

        # Bit position definition
        self.ManTrigPos = 31
        self.AccumulateSelPos = 27

    def write_description(self):
        print("MaximumDuration: " + str(self.MaximumDuration) + ", maximum duration of acquistion in clock cycles")
        print("SampleSize: " + str(self.SampleSize) + ", width of samples (bits)")
        print("NumberOfChannels: " + str(self.NumberOfChannels) + ", parallelism of the acquistion (samples/clock cycle)")
        print("PhaseDepth: " + str(self.PhaseDepth) + ", width of phases (bits)")
        print("TriggerChannels: " + str(self.TriggerChannels) + ", number of trigger channels for readout and drive (bits)")
        print("TimeOfFlightWidth: " + str(self.TimeOfFlightWidth) + ", width of the time of flight timer (bits)")

    def init_axi_lite_interface(self, base_address : int, axi_depth : int):
        super().init_axi_lite_interface(base_address, axi_depth)
        # delete the mmio object created by PYNQ
        del self.mmio
    
    def set_debug_level(self, level : int, file_handler):
        
        if level == self.DebugLevel:
            return 0
        
        if level == 0:
            # no debug
            lite_mmio = self.AxiLiteInterfaceMMIO.replaces
            del self.AxiLiteInterfaceMMIO
            self.AxiLiteInterfaceMMIO = lite_mmio
        elif level == 1:
            self.AxiLiteInterfaceMMIO = _DebugMMIO(self.AxiLiteInterfaceMMIO, 1, file_handler)
        else:
            return 0
        
        self.DebugLevel = level
        return 0

    def set_acquisition_dds_parameters(self, frequency, phase, adc_samplerate):
        """
        Set parameters for acquistion such as demodulation frequency, the phase offset of the demodulation
        
        :param frequency: Frequency of the demodulation signal in MHz
        :type frequency: float
        :param phase: Phase offset of the demodulation signal in RADs
        :type phase: float
        :param ADC_SAMPLERATE: Sampling frequency of the ADC in MHz
        :type ADC_SAMPLERATE: float
        :return: Error code
        :rtype: int
        """
        # check inputs
        if(frequency < 0):
            print("input parameters out of range")
            return -3

        # get poff and pinc
        value_tuple = _compute_pinc_poff(frequency*1000000, phase, adc_samplerate, self.PhaseDepth)

        # this masking is due to the fact that the frequency of the dac is double. this prevents the ADC from
        # going out of phase wrt the generator which means that the readout channels will always be at a constant phase
        pinc = value_tuple[0]&(2**self.PhaseDepth - 2)
        poff = value_tuple[1]&(2**self.PhaseDepth - 2)

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
        ormask = 0x80000000
        cntr = self.AxiLiteInterfaceMMIO.read(0) | ormask
        self.AxiLiteInterfaceMMIO.write(0,cntr)
        return
    
    def set_acquisition_duration(self, duration):
        """
        Set the acquistion duration
        
        :param duration: Duration in clock cycles
        :type duration: uint
        :return: Error Code
        :rtype: int
        """
        
        # TODO: check if this is correct in terms of the actual size of acquistion
        if (duration < 1 or duration > self.MaximumDuration):
            print("acquistion duration is out of range")
            return -3

        cntr = self.AxiLiteInterfaceMMIO.read(self.ctrl*4)
        cntr = _set_bits(cntr, self.TriggerChannels, self.DurationWidth, duration-1)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4,cntr)
    
    def set_trigger_channel(self, channel):
        """
        set the readout trigger channel

        :param channel: channel selection, set to 0 to deactivate external triggers
        :type channel: uint
        :return: Error code
        :rtype: int
        """

        if channel < 0 or channel > self.TriggerChannels:
            print("source choice is out of range")
            return -3

        mask = (1 << channel) >> 1
        cntr = self.AxiLiteInterfaceMMIO.read(self.ctrl*4)
        cntr = _set_bits(cntr, 0, self.TriggerChannels, mask)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, cntr)
        return 0
    
    def set_time_of_flight(self, time_of_flight):
        """
        Set time of flight
        
        :param time_of_flight: Time of flight in clock cycles
        :type time_of_flight: uint
        :return: Error code
        :rtype: int
        """
        # TODO: check if this is correct in terms of the actual time of flight
        if (time_of_flight < 1 or time_of_flight > self.TimeOfFlightMax):
            print("time of flight is out of range")
            return -3

        cntr = self.AxiLiteInterfaceMMIO.read(self.ctrl*4)
        cntr = _set_bits(cntr, self.TriggerChannels + self.DurationWidth, self.TimeOfFlightWidth, time_of_flight-1)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, cntr)

    def set_decimated_output_type(self, type):
        """
        Sets the type of output data of the decimated stream.\n
        Can be set to output the decimated samples or the accumulated values.
        
        :param type: Selection, allowed values are 'decimated' and 'accumulated'
        :type type: 
        """

        bitvalue = None

        if type == 'decimated':
            bitvalue = 0
        elif type == 'accumulated':
            bitvalue = 1
        else:
            print("error, input value for type is not recognized, allowed values are 'decimated' and 'accumulated'")
            return -3
        
        control = _set_bit(self.AxiLiteInterfaceMMIO.read(self.ctrl), self.AccumulateSelPos, bitvalue)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, control)
        return 0
