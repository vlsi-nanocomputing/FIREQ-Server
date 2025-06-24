from pynq import MMIO
import numpy as np
from ._Utils import *

__all__ = ['Acquisition_driver']

class Acquisition_driver(_FIREQDriver):

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

        # Reg definition
        self.ctrl = 0
        self.readout_inc_l = 3
        self.readout_inc_h = 4
        self.readout_off_l = 1
        self.readout_off_h = 2

        # Bit position definition
        self.ManTrigPos = 31

    def WriteDescription(self):
        """
        Print the description of the IP
        """
        print("MaximumDuration: " + str(self.MaximumDuration) + ", maximum duration of acquistion in clock cycles")
        print("SampleSize: " + str(self.SampleSize) + ", width of samples (bits)")
        print("NumberOfChannels: " + str(self.NumberOfChannels) + ", parallelism of the acquistion (samples/clock cycle)")
        print("PhaseDepth: " + str(self.PhaseDepth) + ", width of phases (bits)")
        print("TriggerChannels: " + str(self.TriggerChannels) + ", number of trigger channels for readout and drive (bits)")
        print("TimeOfFlightWidth: " + str(self.TimeOfFlightWidth) + ", width of the time of flight timer (bits)")

    def SetAcquistionParameters(self, frequency, phase, duration, adc_samplerate):
        """
        Set parameters for acquistion such as demodulation frequency, the phase offset of the demodulation
        and the duration of the acquistion
        
        :param frequency: Frequency of the demodulation signal in MHz
        :type frequency: float
        :param phase: Phase offset of the demodulation signal in RADs
        :type phase: float
        :param duration: Acquistion duration in clock cycles
        :type duration: uint
        :param ADC_SAMPLERATE: Sampling frequency of the ADC in MHz
        :type ADC_SAMPLERATE: float
        :return: Error code
        :rtype: int
        """
        # check inputs
        if(frequency < 0 or duration < 1  or duration > self.MaximumDuration):
            print("input parameters out of range")
            return -3

        # get poff and pinc
        value_tuple = _ComputePincPoff(frequency*1000000, phase, adc_samplerate, self.PhaseDepth)

        # this masking is due to the fact that the frequency of the dac is double. this prevents the ADC from
        # going out of phase wrt the generator which means that the readout channels will always be at a constant phase
        pinc = value_tuple[0]&(2**self.PhaseDepth - 2)
        poff = value_tuple[1]&(2**self.PhaseDepth - 2)

        # write registers
        self.SetReadoutIncOff(pinc,poff)

        # write the duration, note that this function removes one from duration before writing 
        self.SetDuration(duration)
        return 0
    
    def SetReadoutIncOff(self, inc, off):
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
        self.mmio.write(self.readout_inc_l*4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self.mmio.write(self.readout_inc_h*4, inc >> 32)

        # write off LOW
        self.mmio.write(self.readout_off_l*4, off & 0xFFFFFFFF)
        # write off HIGH
        self.mmio.write(self.readout_off_h*4, off >> 32)

        return 0
    
    def ManualTrigger(self):
        """
        trigger the acquisition manually
        """
        ormask = 0x80000000
        cntr = self.mmio.read(0) | ormask
        self.mmio.write(0,cntr)
        return
    
    def SetDuration(self, dur):
        """
        Set the acquistion duration
        
        :param dur: Duration in clock cycles
        :type dur: uint
        :return: Error Code
        :rtype: int
        """
        
        # TODO: check if this is correct in terms of the actual size of acquistion
        if (dur < 1 or dur > self.MaximumDuration):
            print("acquistion duration is out of range")
            return -3

        cntr = self.mmio.read(self.ctrl*4)
        cntr = _SetBits(cntr, self.TriggerChannels, self.DurationWidth, dur-1)
        self.mmio.write(self.ctrl*4,cntr)
    
    def SetTriggerChannel(self, trigger):
        """
        set the readout trigger channel

        :param trigger: trigger selection, set to 0 to deactivate external triggers
        :type trigger: uint
        :return: Error code
        :rtype: int
        """

        if trigger < 0 or trigger > self.TriggerChannels:
            print("source choice is out of range")
            return -3

        mask = (1 << trigger) >> 1
        cntr = self.mmio.read(self.ctrl*4)
        cntr = _SetBits(cntr, 0, self.TriggerChannels, mask)
        self.mmio.write(self.ctrl*4, cntr)
        return 0
    
    def SetTimeOfFlight(self, time_of_flight):
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

        cntr = self.mmio.read(self.ctrl*4)
        cntr = _SetBits(cntr, self.TriggerChannels + self.DurationWidth, self.TimeOfFlightWidth, time_of_flight-1)
        self.mmio.write(self.ctrl*4, cntr)