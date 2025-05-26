from pynq import DefaultIP
from pynq import MMIO
import numpy as np
import math

#################################################################################################################################
#      ___           ___           ___           ___           ___           ___           ___           ___           ___      #
#     /\  \         /\  \         /\__\         /\  \         /\  \         /\  \         /\  \         /\  \         /\  \     #
#    /::\  \       /::\  \       /::|  |       /::\  \       /::\  \       /::\  \        \:\  \       /::\  \       /::\  \    #
#   /:/\:\  \     /:/\:\  \     /:|:|  |      /:/\:\  \     /:/\:\  \     /:/\:\  \        \:\  \     /:/\:\  \     /:/\:\  \   #
#  /:/  \:\  \   /::\~\:\  \   /:/|:|  |__   /::\~\:\  \   /::\~\:\  \   /::\~\:\  \       /::\  \   /:/  \:\  \   /::\~\:\  \  #
# /:/__/_\:\__\ /:/\:\ \:\__\ /:/ |:| /\__\ /:/\:\ \:\__\ /:/\:\ \:\__\ /:/\:\ \:\__\     /:/\:\__\ /:/__/ \:\__\ /:/\:\ \:\__\ #
# \:\  /\ \/__/ \:\~\:\ \/__/ \/__|:|/:/  / \:\~\:\ \/__/ \/_|::\/:/  / \/__\:\/:/  /    /:/  \/__/ \:\  \ /:/  / \/_|::\/:/  / #
#  \:\ \:\__\    \:\ \:\__\       |:/:/  /   \:\ \:\__\      |:|::/  /       \::/  /    /:/  /       \:\  /:/  /     |:|::/  /  #
#   \:\/:/  /     \:\ \/__/       |::/  /     \:\ \/__/      |:|\/__/        /:/  /     \/__/         \:\/:/  /      |:|\/__/   #
#    \::/  /       \:\__\         /:/  /       \:\__\        |:|  |         /:/  /                     \::/  /       |:|  |     #
#     \/__/         \/__/         \/__/         \/__/         \|__|         \/__/                       \/__/         \|__|     #
#                                                                                                                               #
#################################################################################################################################

class Generator_driver(DefaultIP):

    bindto = ['user.org:user:AXIS_Generator_IP:1.0']

    def __init__(self, description):
        super().__init__(description=description)
        #TODO: check the correctness
        self.TriggerChannels = int(description['parameters']['TriggerWordWidth'])
        self.SampleMemoryDepth = pow(2,int(description['parameters']['C_S00_AXI_ADDR_WIDTH']))
        self.SampleMemoryMMIO = MMIO(description["phys_addr"]-self.SampleMemoryDepth, self.SampleMemoryDepth)
        self.LogNumberOfChannels = int(description["parameters"]["LogNsamplesClock"])
        self.NumberOfChannels = pow(2,self.LogNumberOfChannels)
        self.ChannelAddressWidth = int(description["parameters"]["AddressWidth"])
        self.ChannelSampleMemoryDepth = pow(2,self.ChannelAddressWidth)
        self.MaximumDuration = pow(2,int(description["parameters"]["DurationWidth"]))
        self.FractionalPrecision = int(description["parameters"]["IncrementFractionalPrecision"])

        self.SeedLfsrWidth = int(description['parameters']['MmFifoAndLfsrOutputWidth'])

        # MASK and Bit position definition
        self.TriggerMask = 0xFFFFFFFF << self.TriggerChannels
        self.SourcePos = 29
        self.TriggerPos = 30
        self.ManTrigPos = 31

    def WriteDescription(self):
        """
        Print the description of the IP
        """
        #TODO: check the correctness
        print("trigger channels: " + str(self.TriggerChannels))
        print("trigger mask: " + str(hex(self.TriggerMask)))
        print("sample memory depth: " + str(self.SampleMemoryDepth))
        print("number of channels: " + str(self.NumberOfChannels))
        print("addrwidth for each channel: " + str(self.ChannelAddressWidth))
        print("channel memory depth: " + str(self.ChannelSampleMemoryDepth))
        print("maximum sample duration: " + str(self.MaximumDuration))
        print("fractional precision: " + str(self.FractionalPrecision))

    def SetManualtTigger(self):
        """
        Trigger the generator manually

        :return: Error code
        :rtype: int
        """
        self.SetBit(reg = 0, pos = self.ManTrigPos, value = 1)
        return

    def SetTriggerChannel(self, channel, ttype):
        """
        set the trigger channel for the generator where triggers are received

        :param channel: Channel number, 1 to TriggerChannels
        :type channel: int
        :param ttype: trigger type: 0 for drive, 1 for readout
        :type ttype: int
        :return: Error code
        :rtype: int
        """
        if channel <= 0 or channel > self.TriggerChannels:
            print("channel choice is out of range")
            return -3
        if ttype != 0 and ttype != 1:
            print("type choice is out of range")
            return -3

        self.SetBit(reg = 0, pos = channel + ttype * self.TriggerChannels, value = 1)
        return 0

    def SetSource(self, source):
        """
        set the source for the generator: lfsr or fifo

        :param source: Source for wave, 1 for LFSR 0 for FIFO
        :type source: int
        :return: Error code
        :rtype: int
        """
        if source < 0 or source > 1:
            print("source choice is out of range")
            return -3

        self.SetBit(reg = 0, pos = self.SourcePos, value = source)
        return 0

    def SetLFSRSeed(self, seed):
        """
        set the seed for lfsr

        :param seed: Seed for LFSR
        :type seed: int
        :return: Error code
        :rtype: int
        """
        if seed < 0 or seed > (2**self.SeedLfsrWidth - 1):
            print("source choice is out of range")
            return -3

        andmask = 0xffffffff - ((2**self.SeedLfsrWidth - 1) << (2*self.TriggerChannels))
        ormask = seed << (2*self.TriggerChannels)
        cntr = self.mmio.read(0) & andmask
        cntr = cntr | ormask
        self.write(0, cntr)
        return 0

    def SetReadoutIncOff(self, inc, off):
        """
        Set readout increment and offset values

        :param inc: Increment value for readout
        :type inc: int
        :param off: Offset value for readout
        :type off: int
        :return: None
        :rtype: None
        """
        # write inc LOW
        self.write(5, inc | 0xFFFFFFFF)
        #write inc HIGH
        self.write(6, inc >> 32)

        # write off LOW
        self.write(9, off | 0xFFFFFFFF)
        #write off HIGH
        self.write(10, off >> 32)

    def SetDriveInc(self, inc):
        """
        Set readout increment value

        :param inc: Increment value for readout
        :type inc: int
        :return: None
        :rtype: None
        """
        # write inc LOW
        self.write(7, inc | 0xFFFFFFFF)
        #write inc HIGH
        self.write(8, inc >> 32)

    def SetTriggerSelector(self, trigger):
        """
        set the trigger: readout or drive

        :param trigger: trigger selection
        :type source: int
        :return: Error code
        :rtype: int
        """
        if trigger < 0 or trigger > 1:
            print("source choice is out of range")
            return -3

        self.SetBit(reg = 0, pos = self.TriggerPos, value = trigger)
        return 0

    def SetBit(self, reg, pos, value):
        andmask = 0xffffffff-(1<<pos)
        ormask = value << pos
        cntr = self.mmio.read(reg) & andmask
        cntr = cntr | ormask
        self.write(reg, cntr)

    def WriteCntrRegister(self, symmetric, even, invert, restart_phase_coherent_counter, forceone, keeplast, perpetual):
        """Write to the control register for manual generation

        Args:
            symmetric (bool): Tell if the wave is symmetric
            even (bool): Tell if the wave has even symmetry
            invert (bool): inverts the sign of the wave
            restart_phase_coherent_counter (bool): restarts the DDS local oscillator when the trigger is received
            forceone (bool): forces the wave to 1 before modulation, used to output a pure tone
            keeplast (bool): keeps last sample at the output of the generator
            perpetual (bool): ignores the duration register *deprecated*
        """
        value = self.mmio.read(0) & ~self.TriggerMask
        if symmetric:
            value = value | 0x40000000

        if even:
            value = value | 0x20000000

        if invert:
            value = value | 0x10000000

        if restart_phase_coherent_counter:
            value = value | 0x08000000

        if forceone:
            value = value | 0x04000000

        if keeplast:
            value = value | 0x02000000

        if perpetual:
            value = value | 0x01000000

        self.mmio.write(0,int(value))
        return

    def ManualTrigger(self):
        """trigger the generator manually
        """
        value = self.mmio.read(0) | 0x80000000
        self.mmio.write(0,value)
        return

    def ManualStop(self):
        """stop the generator manually
        """
        value = self.mmio.read(0) & ~self.TriggerMask
        self.mmio.write(0,value)
        self.ManualTrigger()
        return

    def SetCosineMANUAL(self, frequency, gain, DAC_SAMPLERATE):
        """set the control registers to output a fixed frequency tone with a specified phase and gain

        Args:
            frequency (integer): frequency in MHz
            gain (float): gain, within 0 and 1
            DAC_SAMPLERATE (integer): samplerate of the dac attached to this generator
        """

        # compute the phase increment
        pinc = 0

        if (frequency*1000000 > DAC_SAMPLERATE/2):
            pinc = 2**32 - ((frequency*1000000)*(2**32))/DAC_SAMPLERATE
        else:
            pinc = ((frequency*1000000)*(2**32))/DAC_SAMPLERATE

        # compute gain as fixed point 16-bit number
        gain = round(gain*0x7FFF)

        # set the frequency and phase
        self.WriteRegister('PINC', int(pinc))
        self.WriteRegister('POFF', 0)
        self.WriteRegister('GAIN',gain)

        # start a countinuous tone
        self.WriteCntrRegister(0,0,0,0,1,1,1)

        return

    def SetRectangularMANUAL(self, frequency, gain, duration, DAC_SAMPLERATE):
        """set the manual registers to generate a rectangular pulse of a certain duration

        Args:
            frequency (uint): frequency in MHz
            gain (float): gain, within 0 and 1
            duration (uint): duration in dac samples
            DAC_SAMPLERATE (uint): dac samplerate
        """

        # compute the phase increment
        pinc = 0

        if (frequency*1000000 > DAC_SAMPLERATE/2):
            pinc = 2**32 - ((frequency*1000000)*(2**32))/DAC_SAMPLERATE
        else:
            pinc = ((frequency*1000000)*(2**32))/DAC_SAMPLERATE

        # compute gain as fixed point 16-bit number
        gain = round(gain*0x7FFF)

        # set the frequency and duration
        self.WriteRegister('PINC', int(pinc))
        self.WriteRegister('POFF', 0)
        self.WriteRegister('GAIN',gain)
        self.WriteRegister('MAXDUR',int(duration-1))

        # set the control word for a pulse
        self.WriteCntrRegister(0,0,0,0,1,0,0)
        return

    def FillSampleMemory(self, start_address, samples, interpolation):

        """fill the sample memory of the generator

        Args:
            start_address (uint): address of generator memory where to start filling
            samples (complex array): samples used to fill the memory, numpy complex array, within the complex circle
            interpolation (bool): memory is intended to be used for interpolation
        """

        if (start_address < 0 or start_address >= self.ChannelSampleMemoryDepth):
            print("error, address: " + str(start_address) + " is outside of range 0 to " + str(self.SampleMemoryDepth-1))
            return

        size = samples.size

        if (interpolation):
            maxaddr = start_address + size
            address = start_address + self.NumberOfChannels*self.ChannelSampleMemoryDepth
        else:
            maxaddr = start_address + int(math.ceil(size/self.NumberOfChannels))
            address = start_address

        if (maxaddr >= self.ChannelSampleMemoryDepth):
            print("error, size of sample array is bigger than available memory: " + str(maxaddr))
            return

        channel_index = 0
        for i in samples:
            actual_address = address + channel_index*self.ChannelSampleMemoryDepth
            sample = int(i.real * (2**15 - 1)) << 16
            sample = sample + (int(i.imag * (2**15 - 1)) & 0xFFFF)
            # axi address is byte aligned
            self.SampleMemoryMMIO.write(actual_address << 2,sample)
            if (interpolation):
                address = address + 1
            else:
                channel_index = channel_index + 1
                address = address + channel_index//self.NumberOfChannels
                channel_index = channel_index%self.NumberOfChannels

        return

    def SetMemoryPulseMANUAL(self, frequency, gain, duration, start_address, symmetric, even, wavedepth, DAC_SAMPLERATE):
        """set manual registers to generate a wave using an envelope precedently stored in memory

        Args:
            frequency (uint): frequency in MHz
            gain (float): gain between 0 and 1
            duration (uint): duration in samples
            start_address (uint): address of the starting sample of envelope
            symmetric (bool): flag if memory is symmetric
            even (bool): type of symmetry
            wavedepth (uint): number of samples of the envelope
            DAC_SAMPLERATE (uint): dac samplerate
        """
        n_samples = wavedepth
        if (symmetric):
            n_samples = wavedepth*2

        incr = ((n_samples -1)<<self.FractionalPrecision)//(duration-1)
        off = (start_address<<26) + (((n_samples -1)<<self.FractionalPrecision)%(duration-1))//2

        #print("debug: increment = " + hex(incr) + " , offset = " + hex(off))

        self.SetRectangularMANUAL(frequency,gain,duration,DAC_SAMPLERATE)

        self.WriteRegister('MAXADDR', int(start_address+wavedepth-1))
        self.WriteRegister('SOFF', off)
        self.WriteRegister('INC', incr)

        #override the control register
        self.WriteCntrRegister(symmetric,even,0,0,0,0,0)

        return
    
#######################################################################################################################################################
#      ___           ___           ___           ___                       ___                       ___                       ___           ___      #
#     /\  \         /\  \         /\  \         /\__\          ___        /\  \          ___        /\  \          ___        /\  \         /\__\     #
#    /::\  \       /::\  \       /::\  \       /:/  /         /\  \      /::\  \        /\  \       \:\  \        /\  \      /::\  \       /::|  |    #
#   /:/\:\  \     /:/\:\  \     /:/\:\  \     /:/  /          \:\  \    /:/\ \  \       \:\  \       \:\  \       \:\  \    /:/\:\  \     /:|:|  |    #
#  /::\~\:\  \   /:/  \:\  \    \:\~\:\  \   /:/  /  ___      /::\__\  _\:\~\ \  \      /::\__\      /::\  \      /::\__\  /:/  \:\  \   /:/|:|  |__  #
# /:/\:\ \:\__\ /:/__/ \:\__\    \:\ \:\__\ /:/__/  /\__\  __/:/\/__/ /\ \:\ \ \__\  __/:/\/__/     /:/\:\__\  __/:/\/__/ /:/__/ \:\__\ /:/ |:| /\__\ #
# \/__\:\/:/  / \:\  \  \/__/     \:\/:/  / \:\  \ /:/  / /\/:/  /    \:\ \:\ \/__/ /\/:/  /       /:/  \/__/ /\/:/  /    \:\  \ /:/  / \/__|:|/:/  / #
#      \::/  /   \:\  \            \::/  /   \:\  /:/  /  \::/__/      \:\ \:\__\   \::/__/       /:/  /      \::/__/      \:\  /:/  /      |:/:/  /  #
#      /:/  /     \:\  \           /:/  /     \:\/:/  /    \:\__\       \:\/:/  /    \:\__\       \/__/        \:\__\       \:\/:/  /       |::/  /   #
#     /:/  /       \:\__\         /:/  /       \::/  /      \/__/        \::/  /      \/__/                     \/__/        \::/  /        /:/  /    #
#     \/__/         \/__/         \/__/         \/__/                     \/__/                                               \/__/         \/__/     #
#                                                                                                                                                     #
#######################################################################################################################################################

class Acquisition_driver(DefaultIP):

    bindto = ['user.org:user:AXIS_Acquisition_IP:1.0']

    def __init__(self, description):
        super().__init__(description=description)
        self.TriggerChannels = int(description['parameters']['TriggerWordWidth'])
        self.TriggerMask = 0xFFFFFFFF << self.TriggerChannels
        
        self.LogNumberOfChannels = int(description["parameters"]["LogNsamplesClock"])
        self.NumberOfChannels = pow(2,self.LogNumberOfChannels)

        self.MaximumDuration = pow(2,int(description["parameters"]["DurationWidth"]))

    def WriteDescription(self):
        print("trigger channels: " + str(self.TriggerChannels))
        print("trigger mask: " + str(hex(self.TriggerMask)))
        print("number of channels: " + str(self.NumberOfChannels))
        print("maximum sample duration: " + str(self.MaximumDuration))

    def SetAcquistionParameters(self, frequency, phase, duration, ADC_SAMPLERATE):
        pinc = 0
        #todo: add checks to input params
        pinc = ((frequency*1000000)*(2**32))/ADC_SAMPLERATE

        # this masking is due to the fact that the frequency of the dac is double. this prevents the ADC from
        # going out of phase wrt the generator which means that the readout channels will always be at a constant phase
        self.mmio.write(0x20,round(pinc)&0xFFFFFFFE)
        # todo: fix the phase not being used
        self.mmio.write(0x1c,0)
        # todo: check this out
        self.mmio.write(0x08,int(duration))
        return

    def ManualTrigger(self):
        """trigger the acquisition manually
        """
        self.mmio.write(0,0x80000000)
        return
    
    def SetTriggerChannel(self,channel):
        """set the trigger channel for the acquisition 

        Args:
            channel (int): channel number, 1 to TriggerChannels
        """
        if (channel <= 0 or channel > self.TriggerChannels):
            print("channel choice is out of range")
            return
         
        ormask = 1 << (channel - 1)
        self.mmio.write(0x10,ormask)
        return

######################################################################################################
#      ___           ___                       ___           ___           ___           ___         #
#     /\  \         /\  \          ___        /\  \         /\  \         /\  \         /\  \        #
#     \:\  \       /::\  \        /\  \      /::\  \       /::\  \       /::\  \       /::\  \       #
#      \:\  \     /:/\:\  \       \:\  \    /:/\:\  \     /:/\:\  \     /:/\:\  \     /:/\:\  \      #
#      /::\  \   /::\~\:\  \      /::\__\  /:/  \:\  \   /:/  \:\  \   /::\~\:\  \   /::\~\:\  \     #
#     /:/\:\__\ /:/\:\ \:\__\  __/:/\/__/ /:/__/_\:\__\ /:/__/_\:\__\ /:/\:\ \:\__\ /:/\:\ \:\__\    #
#    /:/  \/__/ \/_|::\/:/  / /\/:/  /    \:\  /\ \/__/ \:\  /\ \/__/ \:\~\:\ \/__/ \/_|::\/:/  /    #
#   /:/  /         |:|::/  /  \::/__/      \:\ \:\__\    \:\ \:\__\    \:\ \:\__\      |:|::/  /     #
#   \/__/          |:|\/__/    \:\__\       \:\/:/  /     \:\/:/  /     \:\ \/__/      |:|\/__/      #
#                  |:|  |       \/__/        \::/  /       \::/  /       \:\__\        |:|  |        #
#                   \|__|                     \/__/         \/__/         \/__/         \|__|        #  
#                                                                                                    #
######################################################################################################

class Trigger_driver(DefaultIP):

    bindto = ['user.org:user:AXIS_Trigger_IP:1.0']

    def __init__(self, description):

        super().__init__(description=description)
        # parse the number of channels of the trigger generator
        self.TriggerChannels = int(description['parameters']['n_occ'])
        self.TriggerMask = 0xFFFFFFFF << self.TriggerChannels

        # parse the fifo interface depth and create mmio handle
        self.FifoInterfaceMemoryDepth = pow(2,int(description['parameters']['C_S00_AXI_ADDR_WIDTH']))
        self.FifoInterfaceMMIO = MMIO(description["phys_addr"]-self.FifoInterfaceMemoryDepth, self.FifoInterfaceMemoryDepth)
        self.ChannelFifoDepth = pow(2,int(description['parameters']['FifoAddressWidth']))

        # parse the size of the repitition counter
        self.MaxHWRepetitions = pow(2,int(description['parameters']['reps_width'])) - 1

        #init fifo pointers
        self.FIFOpointers = [0]*self.TriggerChannels

    def WriteDescription(self):
        print("trigger channels: " + str(self.TriggerChannels))
        print("trigger mask: " + str(hex(self.TriggerMask)))
        print("fifo interface axi depth: " + str(self.FifoInterfaceMemoryDepth))
        print("fifo channel depth: " + str(self.ChannelFifoDepth))
        print("maximum number of hardware repetitions: " + str(self.MaxHWRepetitions))

    def SetTriggerARV(self, value):
        """set the trigger counter auto reload value

        Args:
            value (int): auto reload value, 32 bit unsigned
        """
        self.mmio.write(8,int(value))

    def SetTriggerShots(self, value):
        """set the number of hardware shots 

        Args:
            value (int): number of hardware shots
        """
        if(value <= 0 or value > self.MaxHWRepetitions):
            print("error: the numer of shots " + str(value) + " is outside of range 1 to " + str(self.MaxHWRepetitions))
            return

        self.mmio.write(4,int(value))

    def StartTrigger(self):
        """start the pheripheral
        """
        self.mmio.write(0,0x80000000)

    def IsDone(self):
        """checks if the trigger generator has stopped running

        Returns:
            bool: returns 1 if done
        """
        cntr = self.mmio.read(0)
        if ((cntr&0x40000000) == 0x40000000):
            return 1
        else:
            return 0

    def PushFIFO(self, channel, value):
        """push an output compare value to the fifo of a specified channel

        Args:
            channel (int): channel number, starts from 1
            value (int): unsigned 32 bit output compare value, 0 is invalid
        """
        if (channel<=0 or channel > self.TriggerChannels):
            print("error: channel " + str(channel) + " is outside of range 1 to " + str(self.TriggerChannels))
            return

        if (self.FIFOpointers[channel-1] == self.ChannelFifoDepth):
            print("error: the FIFO for channel " + str(channel) + " is full")
            return

        real_address = (channel-1)*self.ChannelFifoDepth + self.FIFOpointers[channel-1]
        # the left shift is because the axi address is byte aligned whereas the address computed as of now is 32-bit aligned
        self.FifoInterfaceMMIO.write(real_address << 2, int(value))
        self.FIFOpointers[channel-1] += 1
        return
    
    def ResetFIFOPointers(self):
        """reset the fifo pointer used by the driver to refill fifo. Does not physically empty the fifo
        """
        self.FIFOpointers = [0 for i in self.FIFOpointers]
        return
    


    