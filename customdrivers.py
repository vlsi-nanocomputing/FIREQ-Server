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

        # Reg definition
        self.ctrl = 0
        self.readout_inc_l = 5
        self.readout_inc_h = 6
        self.readout_off_l = 9
        self.readout_off_h = 10
        self.drive_inc_l = 7
        self.drive_inc_h = 8

        # Bit position definition
        self.SourcePos = 27
        self.ManTrigSel = 28
        self.ManTrigPos = 31

    def WriteDescription(self):
        """
        Print the description of the IP
        """
        #TODO: check the correctness
        print("trigger channels: " + str(self.TriggerChannels))
        # print("trigger mask: " + str(hex(self.TriggerMask)))
        print("sample memory depth: " + str(self.SampleMemoryDepth))
        print("number of channels: " + str(self.NumberOfChannels))
        print("addrwidth for each channel: " + str(self.ChannelAddressWidth))
        print("channel memory depth: " + str(self.ChannelSampleMemoryDepth))
        print("maximum sample duration: " + str(self.MaximumDuration))
        print("fractional precision: " + str(self.FractionalPrecision))

    def SetManualTrigger(self):
        """
        Trigger the generator manually

        :return: Error code
        :rtype: int
        """
        self.SetBit(reg = self.ctrl, pos = self.ManTrigPos, value = 1)
        return 0

    def SetTriggerChannel(self, channel, ttype):
        """
        Set the channel where the generator is listening for a trigger for the readout and drive trigger types

        :param channel: Channel number, 1 to TriggerChannels
        :type channel: int
        :param ttype: trigger type: 0 for drive, 1 for readout
        :type ttype: int
        :return: Error code
        :rtype: int
        """
        if channel < 1 or channel > self.TriggerChannels:
            print("channel choice is out of range")
            return -3
        if ttype != 0 and ttype != 1:
            print("type choice is out of range")
            return -3

        # clear mask
        andmask = ~((2**self.TriggerChannels - 1) << (ttype*self.TriggerChannels))
        cntr = self.mmio.read(self.ctrl) & andmask
        self.write(self.ctrl, cntr)

        self.SetBit(reg = self.ctrl, pos = channel - 1 + ttype * self.TriggerChannels, value = 1)
        return 0

    def GetTriggerChannel(self, ttype):
        """
        get the trigger channel for the generator where triggers are received

        :param ttype: trigger type: 0 for drive, 1 for readout
        :type ttype: int
        :return: Error code
        :rtype: int
        """
        if ttype != 0 and ttype != 1:
            print("type choice is out of range")
            return -3

        cntr = self.mmio.read(self.ctrl)

        mask = (cntr >> ttype*self.TriggerChannels) & (2**self.TriggerChannels - 1)
        if ttype == 0:
            print("Trigger Drive mask: " + format(mask, f"0{self.TriggerChannels}b"))
        else:
            print("Trigger Readout mask: " + format(mask, f"0{self.TriggerChannels}b"))

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

        self.SetBit(reg = self.ctrl, pos = self.SourcePos, value = source)
        return 0

    def GetSource(self):
        """
        get the source for the generator: lfsr or fifo

        :return: Error code
        :rtype: int
        """
        if self.GetBit(reg = self.ctrl, pos = self.SourcePos) == 0:
            print("Source: FIFO")
        else:
            print("Source: LFSR")

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

        andmask = ~((2**self.SeedLfsrWidth - 1) << (2*self.TriggerChannels))
        ormask = seed << (2*self.TriggerChannels)
        cntr = self.mmio.read(self.ctrl) & andmask
        cntr = cntr | ormask
        self.write(self.ctrl, cntr)
        return 0

    def GetLFSRSeed(self):
        """
        get the seed for lfsr

        :return: Error code
        :rtype: int
        """
        cntr = self.mmio.read(self.ctrl) & ((2**self.SeedLfsrWidth - 1) << (2*self.TriggerChannels))
        print(f"LFSR seed: {cntr}")

        return 0

    def SetReadoutIncOff(self, inc, off):
        """
        Set readout phase increment and phase offset values, used to generate the 
        modulation carrier for waves on the readout output line

        :param inc: Increment value for readout
        :type inc: int
        :param off: Offset value for readout
        :type off: int
        :return: Error code
        :rtype: int
        """
        # write inc LOW
        self.write(self.readout_inc_l*4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self.write(self.readout_inc_h*4, inc >> 32)

        # write off LOW
        self.write(self.readout_off_l*4, off & 0xFFFFFFFF)
        # write off HIGH
        self.write(self.readout_off_h*4, off >> 32)

        return 0

    def GetReadoutIncOff(self):
        """
        Get readout phase increment and phase offset values

        :return: Error code
        :rtype: int
        """
        # read inc LOW
        inc = self.mmio.read(self.readout_inc_l*4)
        # read inc HIGH
        inc += self.mmio.read(self.readout_inc_h*4) << 32

        # read off LOW
        off = self.mmio.read(self.readout_off_l*4)
        # read off HIGH
        off += self.mmio.read(self.readout_off_h*4) << 32

        print(f"readout phase increment: {inc}, phase offset: {off}")

        return 0

    def SetDriveInc(self, inc):
        """
        Set readout phase increment value, used to generate the 
        modulation carrier for waves on the drive output line

        :param inc: Increment value for readout
        :type inc: int
        :return: Error code
        :rtype: int
        """
        # write inc LOW
        self.write(self.drive_inc_l*4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self.write(self.drive_inc_h*4, inc >> 32)

        return 0

    def GetDriveInc(self):
        """
        Get drive phaseincrement value

        :return: Error code
        :rtype: int
        """
        # read inc LOW
        inc = self.mmio.read(self.drive_inc_l*4)
        # read inc HIGH
        inc += self.mmio.read(self.drive_inc_h*4) << 32

        print(f"drive phase increment: {inc}")

        return 0

    def SetManualTriggerDestination(self, destination):
        """
        Set the destination of a manually generated wave.
        The wave to be generated is the one set for readout but you can select 
        if the manually generated wave should output on the readout or drive line.

        :param destination: 1 for readout output line, 0 for drive output line
        :type destination: int
        :return: Error code
        :rtype: int
        """
        if destination < 0 or destination > 1:
            print("source choice is out of range")
            return -3

        self.SetBit(reg = self.ctrl, pos = self.ManTrigSel, value = destination)
        return 0

    def SetBit(self, reg, pos, value):
        """
        Function to set a bit in a register

        :param reg: address of the register
        :type reg: int
        :param pos: position in the register to modify
        :type pos: int
        :param value: value to write in the register
        :type value: int, must be 0 or 1
        """
        andmask = 0xffffffff-(1<<pos)
        ormask = value << pos
        cntr = self.mmio.read(reg*4) & andmask
        cntr = cntr | ormask
        self.write(reg*4, cntr)

    def GetBit(self, reg, pos):
        """
        Function to get a bit from a register

        :param reg: address of the register
        :type reg: int
        :param pos: position in the register to read
        :type pos: int
        :return: Value read
        :rtype: int
        """
        cntr = self.mmio.read(reg*4)

        return (cntr >> pos) & 0x1

    # def WriteCntrRegister(self, symmetric, even, invert, restart_phase_coherent_counter, forceone, keeplast, perpetual):
    #     """Write to the control register for manual generation
    #
    #     Args:
    #         symmetric (bool): Tell if the wave is symmetric
    #         even (bool): Tell if the wave has even symmetry
    #         invert (bool): inverts the sign of the wave
    #         restart_phase_coherent_counter (bool): restarts the DDS local oscillator when the trigger is received
    #         forceone (bool): forces the wave to 1 before modulation, used to output a pure tone
    #         keeplast (bool): keeps last sample at the output of the generator
    #         perpetual (bool): ignores the duration register *deprecated*
    #     """
    #     value = self.mmio.read(0) & ~self.TriggerMask
    #     if symmetric:
    #         value = value | 0x40000000
    #
    #     if even:
    #         value = value | 0x20000000
    #
    #     if invert:
    #         value = value | 0x10000000
    #
    #     if restart_phase_coherent_counter:
    #         value = value | 0x08000000
    #
    #     if forceone:
    #         value = value | 0x04000000
    #
    #     if keeplast:
    #         value = value | 0x02000000
    #
    #     if perpetual:
    #         value = value | 0x01000000
    #
    #     self.mmio.write(0,int(value))
    #     return
    #
    # def ManualTrigger(self):
    #     """trigger the generator manually
    #     """
    #     value = self.mmio.read(0) | 0x80000000
    #     self.mmio.write(0,value)
    #     return
    #
    # def ManualStop(self):
    #     """stop the generator manually
    #     """
    #     value = self.mmio.read(0) & ~self.TriggerMask
    #     self.mmio.write(0,value)
    #     self.ManualTrigger()
    #     return
    #
    # def SetCosineMANUAL(self, frequency, gain, DAC_SAMPLERATE):
    #     """set the control registers to output a fixed frequency tone with a specified phase and gain
    #
    #     Args:
    #         frequency (integer): frequency in MHz
    #         gain (float): gain, within 0 and 1
    #         DAC_SAMPLERATE (integer): samplerate of the dac attached to this generator
    #     """
    #
    #     # compute the phase increment
    #     pinc = 0
    #
    #     if (frequency*1000000 > DAC_SAMPLERATE/2):
    #         pinc = 2**32 - ((frequency*1000000)*(2**32))/DAC_SAMPLERATE
    #     else:
    #         pinc = ((frequency*1000000)*(2**32))/DAC_SAMPLERATE
    #
    #     # compute gain as fixed point 16-bit number
    #     gain = round(gain*0x7FFF)
    #
    #     # set the frequency and phase
    #     self.WriteRegister('PINC', int(pinc))
    #     self.WriteRegister('POFF', 0)
    #     self.WriteRegister('GAIN',gain)
    #
    #     # start a countinuous tone
    #     self.WriteCntrRegister(0,0,0,0,1,1,1)
    #
    #     return
    #
    # def SetRectangularMANUAL(self, frequency, gain, duration, DAC_SAMPLERATE):
    #     """set the manual registers to generate a rectangular pulse of a certain duration
    #
    #     Args:
    #         frequency (uint): frequency in MHz
    #         gain (float): gain, within 0 and 1
    #         duration (uint): duration in dac samples
    #         DAC_SAMPLERATE (uint): dac samplerate
    #     """
    #
    #     # compute the phase increment
    #     pinc = 0
    #
    #     if (frequency*1000000 > DAC_SAMPLERATE/2):
    #         pinc = 2**32 - ((frequency*1000000)*(2**32))/DAC_SAMPLERATE
    #     else:
    #         pinc = ((frequency*1000000)*(2**32))/DAC_SAMPLERATE
    #
    #     # compute gain as fixed point 16-bit number
    #     gain = round(gain*0x7FFF)
    #
    #     # set the frequency and duration
    #     self.WriteRegister('PINC', int(pinc))
    #     self.WriteRegister('POFF', 0)
    #     self.WriteRegister('GAIN',gain)
    #     self.WriteRegister('MAXDUR',int(duration-1))
    #
    #     # set the control word for a pulse
    #     self.WriteCntrRegister(0,0,0,0,1,0,0)
    #     return
    #
    # def FillSampleMemory(self, start_address, samples, interpolation):
    #
    #     """fill the sample memory of the generator
    #
    #     Args:
    #         start_address (uint): address of generator memory where to start filling
    #         samples (complex array): samples used to fill the memory, numpy complex array, within the complex circle
    #         interpolation (bool): memory is intended to be used for interpolation
    #     """
    #
    #     if (start_address < 0 or start_address >= self.ChannelSampleMemoryDepth):
    #         print("error, address: " + str(start_address) + " is outside of range 0 to " + str(self.SampleMemoryDepth-1))
    #         return
    #
    #     size = samples.size
    #
    #     if (interpolation):
    #         maxaddr = start_address + size
    #         address = start_address + self.NumberOfChannels*self.ChannelSampleMemoryDepth
    #     else:
    #         maxaddr = start_address + int(math.ceil(size/self.NumberOfChannels))
    #         address = start_address
    #
    #     if (maxaddr >= self.ChannelSampleMemoryDepth):
    #         print("error, size of sample array is bigger than available memory: " + str(maxaddr))
    #         return
    #
    #     channel_index = 0
    #     for i in samples:
    #         actual_address = address + channel_index*self.ChannelSampleMemoryDepth
    #         sample = int(i.real * (2**15 - 1)) << 16
    #         sample = sample + (int(i.imag * (2**15 - 1)) & 0xFFFF)
    #         # axi address is byte aligned
    #         self.SampleMemoryMMIO.write(actual_address << 2,sample)
    #         if (interpolation):
    #             address = address + 1
    #         else:
    #             channel_index = channel_index + 1
    #             address = address + channel_index//self.NumberOfChannels
    #             channel_index = channel_index%self.NumberOfChannels
    #
    #     return
    #
    # def SetMemoryPulseMANUAL(self, frequency, gain, duration, start_address, symmetric, even, wavedepth, DAC_SAMPLERATE):
    #     """set manual registers to generate a wave using an envelope precedently stored in memory
    #
    #     Args:
    #         frequency (uint): frequency in MHz
    #         gain (float): gain between 0 and 1
    #         duration (uint): duration in samples
    #         start_address (uint): address of the starting sample of envelope
    #         symmetric (bool): flag if memory is symmetric
    #         even (bool): type of symmetry
    #         wavedepth (uint): number of samples of the envelope
    #         DAC_SAMPLERATE (uint): dac samplerate
    #     """
    #     n_samples = wavedepth
    #     if (symmetric):
    #         n_samples = wavedepth*2
    #
    #     incr = ((n_samples -1)<<self.FractionalPrecision)//(duration-1)
    #     off = (start_address<<26) + (((n_samples -1)<<self.FractionalPrecision)%(duration-1))//2
    #
    #     #print("debug: increment = " + hex(incr) + " , offset = " + hex(off))
    #
    #     self.SetRectangularMANUAL(frequency,gain,duration,DAC_SAMPLERATE)
    #
    #     self.WriteRegister('MAXADDR', int(start_address+wavedepth-1))
    #     self.WriteRegister('SOFF', off)
    #     self.WriteRegister('INC', incr)
    #
    #     #override the control register
    #     self.WriteCntrRegister(symmetric,even,0,0,0,0,0)
    #
    #     return
    
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
        # number of triggers on the input trigger channel
        self.TriggerChannels = int(description['parameters']['TriggerWordWidth'])
        # parallelism of the acquistion
        self.LogNumberOfChannels = int(description["parameters"]["LogNsamplesClock"])
        self.NumberOfChannels = pow(2,self.LogNumberOfChannels)
        # maximum acquistion duration
        self.DurationWidth = int(description["parameters"]["DurationWidth"])
        self.MaximumDuration = pow(2,self.DurationWidth)
        # depth of the phase increment and offset
        self.PhaseDepth = int(description["parameters"]["PhaseDepth"])
        # maximum time of flight delay
        self.TimeOfFlightMax = pow(2,int(description["parameters"]["TimeOfFlightCounterWidth"]))

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
        print("trigger channels: " + str(self.TriggerChannels))
        print("number of channels: " + str(self.NumberOfChannels))
        print("maximum acquistion duration: " + str(self.MaximumDuration))
        print("depth of phase increment and offset: " + str(self.PhaseDepth))
        print("maximum time of flight: " + str(self.TimeOfFlightMax))

    def SetAcquistionParameters(self, frequency, phase, duration, ADC_SAMPLERATE):
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
        if(frequency < 0 or duration < 0  or duration > self.MaximumDuration):
            print("input parameters out of range")
            return -3

        # get the bounded phase, mapping an unbounded radiant into (-2pi:2pi)
        bounded_phase = phase % (2*np.pi)
        # add 2pi to move the bounds to (0:4pi)
        bounded_phase = bounded_phase + 2*np.pi
        # now bound the phase to [0:2pi)
        bounded_phase = bounded_phase% (2*np.pi)

        # get the nyquist zone
        nyquist_zone = frequency//(ADC_SAMPLERATE/2)
        # get the reminder, e.g. the distance from the nyquist frequency
        nyquist_reminder = frequency%(ADC_SAMPLERATE/2)

        # compute the phase increment and offset
        pinc = 0
        poff = 0

        if (nyquist_zone%2 == 0):
            # checking that we are in the odd nyquist zones, note that nyquist_zone differs from the real nyquist zone by 1 so
            # the odd/even checks on the real nyquist zone are opposite
            # in this case we will be here for zones 1,3,5,... which map into 0,2,4 for the nyquist_zone variable
            pinc = ((nyquist_reminder*1000000)*(2**self.PhaseDepth))/ADC_SAMPLERATE
            poff = (2**self.PhaseDepth - 1)*(bounded_phase/(2*np.pi))
        else:
            # when the nyquist zone is even, the phase has opposite sign and the frequency needs to be calculated 
            # as the distance from the sample rate
            nyquist_reminder = ADC_SAMPLERATE/2 - nyquist_reminder
            bounded_phase = 2*np.pi - bounded_phase
            pinc = ((nyquist_reminder*1000000)*(2**self.PhaseDepth))/ADC_SAMPLERATE
            poff = (2**self.PhaseDepth - 1)*(bounded_phase/(2*np.pi))

        # this masking is due to the fact that the frequency of the dac is double. this prevents the ADC from
        # going out of phase wrt the generator which means that the readout channels will always be at a constant phase
        pinc = round(pinc)&(2**self.PhaseDepth - 2)
        poff = round(poff)&(2**self.PhaseDepth - 2)

        # write registers
        self.SetReadoutIncOff(pinc,poff)

        # write the duration
        self.SetDuration(duration)
        return
    
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
        self.write(self.readout_inc_l*4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self.write(self.readout_inc_h*4, inc >> 32)

        # write off LOW
        self.write(self.readout_off_l*4, off & 0xFFFFFFFF)
        # write off HIGH
        self.write(self.readout_off_h*4, off >> 32)

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
        
        # todo: check if this is correct in terms of the actual size of acquistion
        if (dur < 0 or dur > self.MaximumDuration):
            print("acquistion duration is out of range")
            return -3

        mask = (self.MaximumDuration - 1) << self.TriggerChannels
        cntr = self.mmio.read(0) & (~mask)
        cntr = cntr | (dur << self.TriggerChannels)
        self.mmio.write(0,cntr)
    
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

        andmask = ~(2**self.TriggerChannels-1)
        cntr = self.mmio.read(0) & andmask
        if (trigger == 0):
            ormask = 0
        else:
            ormask = 1 << (trigger-1)
        cntr = cntr | ormask
        self.mmio.write(0,cntr)
        return 0
    
    def SetTimeOfFlight(self, time_of_flight):
        """
        Set time of flight
        
        :param time_of_flight: Time of flight in clock cycles
        :type time_of_flight: uint
        :return: Error code
        :rtype: int
        """
        # todo: check if this is correct in terms of the actual time of flight
        if (time_of_flight < 0 or time_of_flight > self.MaximumDuration):
            print("time of flight is out of range")
            return -3

        mask = (self.TimeOfFlightMax - 1) << (self.TriggerChannels + self.DurationWidth)
        cntr = self.mmio.read(0) & (~mask)
        cntr = cntr | (time_of_flight << (self.TriggerChannels + self.DurationWidth))
        self.mmio.write(0,cntr)

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
        self.TriggerChannels = int(description['parameters']['TriggerWordWidth'])

        # parse the fifo interface depth and create mmio handle
        self.FifoInterfaceMemoryDepth = pow(2,int(description['parameters']['C_S00_AXI_ADDR_WIDTH']))
        self.FifoInterfaceMMIO = MMIO(description["phys_addr"]-self.FifoInterfaceMemoryDepth, self.FifoInterfaceMemoryDepth)

        # fifo depth in number of words 
        self.ChannelFifoDepth = pow(2,int(description['parameters']['FifoAddressWidth']))

        # fifo output width
        self.FifoOutputWidth = int(description['parameters']['FifoOutputWidth'])
        # maximum drive delay
        self.DriveDelayMax = pow(2,self.FifoOutputWidth-1)

        # experiment max
        self.ExperimentTimerMax = pow(2,int(description['parameters']['ExperimentTimerWidth']))

        # parse the size of the repitition counter
        self.MaxHWRepetitions = pow(2,int(description['parameters']['RepetitionWidth']))

        #init fifo pointers
        self.FIFOpointers = [0]*self.TriggerChannels

        self.ctrl = 0
        self.experiment_dur_l = 2
        self.experiment_dur_h = 3
        self.shots_num_l = 1

        # Bit position definition
        self.ManTrigPos = 31

    def WriteDescription(self):
        print("trigger channels: " + str(self.TriggerChannels))
        print("fifo interface axi depth: " + str(self.FifoInterfaceMemoryDepth))
        print("fifo channel depth: " + str(self.ChannelFifoDepth))
        print("maximum number of hardware repetitions: " + str(self.MaxHWRepetitions))

    def SetExperimentDuration(self,duration):
        """
        Set the experiment duration for a single shot
        
        :param duration: Duration in clock cycles
        :type duration: uint
        """

        # write inc LOW
        self.write(self.experiment_dur_l*4, duration & 0xFFFFFFFF)
        # write inc HIGH
        self.write(self.experiment_dur_h*4, duration >> 32)

    def SetNumberOfShots(self, value):
        """
        Set the number of shots to execute in hardware
        
        :param value: number of shots
        :type value: uint
        """
        if(value < 1 or value > self.MaxHWRepetitions):
            print("error: the numer of shots " + str(value) + " is outside of range 1 to " + str(self.MaxHWRepetitions))
            return

        self.mmio.write(self.shots_num_l*4,int(value))

    def StartExperiment(self):
        """
        Start the generation of triggers
        """
        self.mmio.write(0,1 << self.ManTrigPos)

    def IsDone(self):
        """
        Check if the experiment is finished
        
        :return: 1 if the experiment is finished, 0 if still running
        :rtype: Literal[1, 0]
        """
        cntr = self.mmio.read(0)
        if ((cntr&0x40000000) == 0x40000000):
            return 1
        else:
            return 0

    def PushDriveFIFO(self, channel, delay, generate_trigger):
        """
        Push a delay value to the FIFO of a channel. The generate_trigger input is used to 
        tell the trigger generator if a trigger should be generated at the end of the delay
        
        :param channel: Drive channel
        :type channel: uint
        :param delay: Delay in clock cycles
        :type delay: uint
        :param generate_trigger: Generates a trigger if set to 1
        :type generate_trigger: Literal[1, 0]
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
        """
        Resets the cached FIFO pointers. Does not clear the FIFO entries
        """
        self.FIFOpointers = [0 for i in self.FIFOpointers]
        return
    


    