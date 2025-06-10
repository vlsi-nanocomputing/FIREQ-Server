if not __debug__:
    from pynq import DefaultIP
    from pynq import MMIO
import numpy as np
import math

def _SetBit(value : int, pos : int, setvalue : int):
    """
    Set the bit at index pos of value to setvalue
    
    :param value: Input value to be manipulated
    :type value: int
    :param pos: Bit position, 0 is LSB
    :type pos: int
    :param setvalue: Value to set the bit, 0 or 1
    :type setvalue: int
    """
    bitvalue = 0
    if setvalue:
        bitvalue = 1
    return (value & ~(bitvalue << pos)) | (bitvalue << pos)

def _GetBit(value : int, pos : int):
    """
    Gets the bit at position pos of argument value
    
    :param value: Input value to extract bit from
    :type value: int
    :param pos: Bit position 
    :type pos: int
    :return: 0 or 1 depending on the bit value
    :rtype: int
    """
    return (value & (1 << pos)) >> pos

def _SetBits(value : int, start : int, length : int, setvalue : int):
    """
    Set bits from start for a length equal to length to setvalue
    
    :param value: Input value to set bits
    :type value: int
    :param start: Start bit index, 0 is LSB
    :type start: int
    :param length: Number of bits to modify
    :type length: int
    :param setvalue: Value to set the bits
    :type setvalue: int
    :return: Modified input value
    :rtype: int
    """
    mask = ((1 << length) - 1) << start
    safe_setvalue = (setvalue << start) & mask
    return (value & ~(mask)) | safe_setvalue

def _GetBits(value : int, start : int, length : int):
    """
    Get a number of sequential bits from argument value
    
    :param value: Input to extract bits
    :type value: int
    :param start: Start index of the bits, 0 is LSB
    :type start: int
    :param length: Number of bits to extract
    :type length: int
    :return: Extracted bits from argument value
    :rtype: int
    """
    mask = ((1 << length) - 1) << start
    return (value & mask) >> start

def _ComputePincPoff(frequency : int, phase : int, samplerate : int, phase_depth : int):
    """
    Compute the phase increment and phase offset depending on the input frequency and phase
    
    :param frequency: Frequency in Hz
    :type frequency: int
    :param phase: Phase in radiants
    :type phase: int
    :param samplerate: Sample rate in samples per second
    :type samplerate: int
    :param phase_depth: Depth of the phase registers
    :type phase_depth: int
    :return: Phase increment and phase offset tuple
    :rtype: tuple[Any, Any]
    """
    # get the bounded phase, mapping an unbounded radiant into (-2pi:2pi)
    bounded_phase = phase % (2*np.pi)
    # add 2pi to move the bounds to (0:4pi)
    bounded_phase = bounded_phase + 2*np.pi
    # now bound the phase to [0:2pi)
    bounded_phase = bounded_phase% (2*np.pi)
    # get the nyquist zone
    nyquist_zone = frequency//(samplerate/2)
    # get the reminder, e.g. the distance from the nyquist frequency
    nyquist_reminder = frequency%(samplerate/2)
    # compute the phase increment and offset

    pinc = 0
    poff = 0
    if (nyquist_zone%2 == 0):
        # checking that we are in the odd nyquist zones, note that nyquist_zone differs from the real nyquist zone by 1 so
        # the odd/even checks on the real nyquist zone are opposite
        # in this case we will be here for zones 1,3,5,... which map into 0,2,4 for the nyquist_zone variable
        pinc = (nyquist_reminder*(2**phase_depth))/samplerate
        poff = (2**phase_depth - 1)*(bounded_phase/(2*np.pi))
    else:
        # when the nyquist zone is even, the phase has opposite sign and the frequency needs to be calculated 
        # as the distance from the sample rate
        nyquist_reminder = samplerate/2 - nyquist_reminder
        bounded_phase = 2*np.pi - bounded_phase
        pinc = (nyquist_reminder*(2**phase_depth))/samplerate
        poff = (2**phase_depth - 1)*(bounded_phase/(2*np.pi))
    
    return (round(pinc), round(poff))

if __debug__:
    class MMIO:
        base_address = 0
        depth = 0
        memory = {}

        def __init__(self, base, depth):
            if (type(base) != int or type(depth) != int):
                print("error during mmio init, the or depth is wrong")
            self.base_address = base
            self.depth = depth
        
        def read(self, address):
            if address%4 != 0:
                print("mmio error, read at address not word aligned")
                return 0
            if address not in self.memory.keys():
                self.memory[address] = 0
            return self.memory[address]
        
        def write(self, address, value):
            if address%4 != 0:
                print("mmio error, read at address not word aligned")
                return 0
            with open("./memory_operations_axi.txt","a+") as writefile:
                writefile.write("write address " + hex(address) + " write data " + hex(value) + " " + str(value) + "\n")
            self.memory[address] = value
    
    class DefaultIP:
        
        def __init__(self, description):
            self.mmio = MMIO(int(description["phys_addr"],16), int(description['C_HIGHADDR'],16) - int(description["phys_addr"],16)+1)



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

    bindto = ['user.org:user:axisGeneratorIP:1.0']
    
    # a dictionary that stores useful data about the envelopes that have been written to 
    # the envelope memory
    EnvelopeMemoryDict = {}
    EnvelopeMemoryDictReservedNames = []
    # a dictionary that stores useful data about the wave definition words that have
    # been written to the sequencer's wave memory
    WaveMemoryDict = {}
    WaveMemoryDictReservedNames = []
    # the axi interface 
    AxiFullInterfaceMMIO = None

    def __init__(self, description):
        super().__init__(description=description)
        # address width of the envelope memory, word aligned
        self.SampleMemoryAddressWidth = int(description["parameters"]["SampleMemoryAddressWidth"])
        # size of the envelope memory for every channel, in words
        self.ChannelSampleMemoryDepth = pow(2,self.SampleMemoryAddressWidth)
        # width of the duration 
        self.DurationWidth = int(description["parameters"]["DurationWidth"])
        self.MaximumDuration = pow(2,self.DurationWidth)
        # fractional precision of the interpolator
        self.FractionalPrecision = int(description["parameters"]["IncrementFractionalPrecision"])
        # width of samples
        self.SampleSize = int(description["parameters"]["SampleSize"])
        # number of channels (parallelism of the generator)
        self.LogNumberOfChannels = int(description["parameters"]["LogNsamplesClock"])
        self.NumberOfChannels = pow(2,self.LogNumberOfChannels)
        # width of the phase increment and offset
        self.PhaseDepth = int(description["parameters"]["PhaseDepth"])
        # number of trigger channels
        self.TriggerChannels = int(description['parameters']['TriggerWordWidth'])
        # axi full interface depth in bytes
        self.AxiFullInterfaceDepth = pow(2,int(description['parameters']['C_S00_AXI_ADDR_WIDTH']))
        #self.AxiFullInterfaceMMIO = MMIO(description["phys_addr"]-self.AxiFullInterfaceDepth, self.AxiFullInterfaceDepth)
        # size of axi full segments in bytes
        self.TotalSampleMemorySegmentDepth = int(description['parameters']['TotalSampleMemorySegmentDepth'])
        self.WaveMemorySegmentDepth = int(description['parameters']['WaveMemoryDepth'])
        self.MemoryMappedFifoSegmentDepth = int(description['parameters']['MemoryMappedFifoDepth'])
        # width of the lfsr seed
        self.SeedLfsrWidth = int(description['parameters']['MmFifoAndLfsrOutputWidth'])

        # Reg definition
        self.ctrl = 0
        self.readout_wave_l = 1
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
    
    def InitAxiFullInterface(self, base_address : int):
        """
        Initialize the axi full interface for this IP
        
        :param base_address: Base address of the axi full interface
        :type base_address: int
        """
        if self.AxiFullInterfaceMMIO is None:
            self.AxiFullInterfaceMMIO = MMIO(base_address, self.AxiFullInterfaceDepth)
        self.ResetEnvelopeDict()

    def ResetEnvelopeDict(self):
        """
        Resets the cached information about the envelope memory. The actual memory is not modified by this function. \n
        Since resetting this memory invalidates the wave definition words, the wave memory cache is also cleared 
         
        """
        self.EnvelopeMemoryDict = {}
        # set the memory free space in the envelope memory dictionary
        self.EnvelopeMemoryDict["_FREESPACE"] = {"start" : 0, "depth" : self.ChannelSampleMemoryDepth}
        self.EnvelopeMemoryDictReservedNames.append("_FREESPACE")
        # set an entry for rectangular waves
        self.EnvelopeMemoryDict["_RECTANGULAR"] = {"is_interp" : 0, "size" : any, "is_sym" : 0,
                                                   "i_even" : 0, "q_even" : 0, "start" : 0}
        self.EnvelopeMemoryDictReservedNames.append("_RECTANGULAR")
        self.ResetWaveMemoryDict()

    def ResetWaveMemoryDict(self):
        """
        Resets the cached information about the wave memory and also clears the generator wave memory.

        """
        self.WaveMemoryDict = {}
        # address of next wave in wave memory
        self.WaveMemoryDict["_NEXT"] = 0
        self.WaveMemoryDictReservedNames.append("_NEXT")
        for address in range(self.TotalSampleMemorySegmentDepth,self.TotalSampleMemorySegmentDepth+self.WaveMemorySegmentDepth,4):
            self.AxiFullInterfaceMMIO.write(address, 0)

    def WriteDescription(self):
        """
        Print the description of the IP
        """

        print("SampleMemoryAddressWidth: " + str(self.SampleMemoryAddressWidth) + ", address width of the envelope memory (word/IQSample aligned)")
        print("ChannelSampleMemoryDepth: " + str(self.ChannelSampleMemoryDepth) + ", depth of the envelope memory (words/IQSamples aligned)")
        print("MaximumDuration: " + str(self.MaximumDuration) + ", maximum duration of a wave (samples)")
        print("FractionalPrecision: " + str(self.FractionalPrecision) + ", fractional precision of the interpolator (bits)")
        print("SampleSize: " + str(self.SampleSize) + ", width of samples (bits)")
        print("NumberOfChannels: " + str(self.NumberOfChannels) + ", parallelism of the generator (samples/clock cycle)")
        print("PhaseDepth: " + str(self.PhaseDepth) + ", width of phases (bits)")
        print("TriggerChannels: " + str(self.TriggerChannels) + ", number of trigger channels for readout and drive (bits)")
        print("AxiFullInterfaceDepth: " + str(self.AxiFullInterfaceDepth) + ", axi full interface depth (bytes)")
        print("TotalSampleMemorySegmentDepth: " + str(self.TotalSampleMemorySegmentDepth) + ", envelope memory segment depth (bytes)")
        print("WaveMemorySegmentDepth: " + str(self.WaveMemorySegmentDepth) + ", wave memory segment depth (bytes)")
        print("MemoryMappedFifoSegmentDepth: " + str(self.MemoryMappedFifoSegmentDepth) + ", memory mapped FIFO segment depth (bytes)")
        print("SeedLfsrWidth: " + str(self.SeedLfsrWidth) + ", width of lsfr seed and memory mapped FIFO entries (bits)")

    def SetManualTrigger(self):
        """
        Trigger the generator manually

        :return: Error code
        :rtype: int
        """
        register = self.mmio.read(self.ctrl*4)
        self.mmio.write(self.ctrl*4, _SetBit(register, self.ManTrigPos, 1))
        return 0

    def SetTriggerChannel(self, channel, ttype):
        """
        Set the channel where the generator is listening for a trigger for the readout and drive trigger types.\n
        Set the channel to 0 if you want to disable external triggers

        :param channel: Channel number, 1 to TriggerChannels
        :type channel: int
        :param ttype: trigger type: 0 for drive, 1 for readout
        :type ttype: int
        :return: Error code
        :rtype: int
        """
        if channel < 0 or channel > self.TriggerChannels:
            print("channel choice is out of range")
            return -3
        if ttype != 0 and ttype != 1:
            print("type choice is out of range")
            return -3

        # write to the control register
        trigger_mask = (1 << channel) >> 1
        control = _SetBits(self.mmio.read(self.ctrl*4), ttype*self.TriggerChannels, self.TriggerChannels, trigger_mask)
        self.mmio.write(self.ctrl*4, control)

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

        channel = _GetBits(cntr, ttype*self.TriggerChannels, self.TriggerChannels)
        if ttype == 0:
            print("Trigger Drive mask: " + format(channel, f"0{self.TriggerChannels}b"))
        else:
            print("Trigger Readout mask: " + format(channel, f"0{self.TriggerChannels}b"))

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

        register = _SetBit(value= self.mmio.read(self.ctrl*4), pos= self.SourcePos, setvalue= source)
        self.mmio.write(self.ctrl*4, register)
        return 0

    def GetSource(self):
        """
        get the source for the generator: lfsr or fifo

        :return: Error code
        :rtype: int
        """
        cntr = self.mmio.read(self.ctrl*4)
        if _GetBit(cntr, self.SourcePos) == 0:
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
        cntr = self.mmio.read(self.ctrl*4)
        cntr = _SetBits(cntr, 2*self.TriggerChannels, self.SeedLfsrWidth, seed)
        self.mmio.write(self.ctrl*4, cntr)
        return 0

    def GetLFSRSeed(self):
        """
        get the seed for lfsr

        :return: Error code
        :rtype: int
        """
        cntr = self.mmio.read(self.ctrl)
        cntr = _GetBits(cntr, 2*self.TriggerChannels, self.SeedLfsrWidth)
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
        self.mmio.write(self.readout_inc_l*4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self.mmio.write(self.readout_inc_h*4, inc >> 32)

        # write off LOW
        self.mmio.write(self.readout_off_l*4, off & 0xFFFFFFFF)
        # write off HIGH
        self.mmio.write(self.readout_off_h*4, off >> 32)

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
        Set drive phase increment value, used to generate the 
        modulation carrier for waves on the drive output line

        :param inc: Increment value for readout
        :type inc: int
        :return: Error code
        :rtype: int
        """
        # write inc LOW
        self.mmio.write(self.drive_inc_l*4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self.mmio.write(self.drive_inc_h*4, inc >> 32)

        return 0

    def GetDriveInc(self):
        """
        Get drive phase increment value

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

        register = _SetBit(self.mmio.read(self.ctrl*4), pos= self.ManTrigSel, setvalue= destination)
        self.mmio.write(self.ctrl*4, register)
        return 0
    
    def GetManualTriggerDestination(self):
        """
        Get the destination output line for the generator when triggered from the manual trigger
        
        """
        dest = _GetBit(self.mmio.read(self.ctrl*4), self.ManTrigSel)
        if dest:
            print("Manual trigger destination is readout line")
        else: 
            print("Manual trigger destination is drive line")
        return 0
    
    def SetReadoutDDSParameters(self, frequency : float, phase : float, dac_samplerate : int):
        """
        Set frequency and phase for the readout carrier signal
        
        :param frequency: Frequency of the carrier in MHz
        :type frequency: float
        :param phase: Phase in radiants
        :type phase: float
        :param dac_samplerate: Sample rate of the dac, in samples per second
        :type dac_samplerate: int
        :return: Error code
        :rtype: Literal[-3, 0]
        """
        # check inputs
        if(frequency < 0):
            print("input parameters out of range")
            return -3

        # get poff and pinc
        value_tuple = _ComputePincPoff(frequency*1000000, phase, dac_samplerate, self.PhaseDepth)

        # write registers
        self.SetReadoutIncOff(value_tuple[0],value_tuple[1])
        return 0

    def SetDriveDDSParameters(self, frequency, dac_samplerate):
        """
        Set modulation frequency for the drive output channel
        
        :param frequency: Frequency in MHz
        :type frequency: float
        :param dac_samplerate: Sampling frequency of the dac in samples per second
        :type dac_samplerate: int
        :return: Error code
        :rtype: Literal[-3, 0]
        """
        # check inputs
        if(frequency < 0):
            print("input parameters out of range")
            return -3

        # get poff and pinc
        value_tuple = _ComputePincPoff(frequency*1000000, 0, dac_samplerate, self.PhaseDepth)

        # write registers
        self.SetDriveInc(value_tuple[0])
        return 0
    
    def WriteEnvelopeMemory(self, envelope_samples : np.ndarray, for_interpolation : bool, is_symmetric : bool, i_even : bool, q_even : bool, envelope_name : str):
        """
        Write to the envelope memory (sample memory) a series of samples to be used to generate a wave. \n
        An envelope description is cached and a name is associated to it. \n
        Important note: symmetric waves should have an odd number of samples and only half of the samples
        (including the center sample) should be passed to this function. \n
        Warning: the values in the array should be representable in int16, if not they will be saturated 
        to the maximum or negative value.
        
        :param envelope_samples: complex array of samples, real and imaginary part used as I/Q values
        :type envelope_samples: complex int16 numpy array
        :param for_interpolation: if the envelope is to be used with interpolation
        :type for_interpolation: bool
        :param is_symmetric: if the envelope is symmetric, only valid if it's for interpolation
        :type is_symmetric: bool
        :param i_even: type of symmetry of the in-phase samples
        :type i_even: bool
        :param q_even: type of symmetry of the quadrature samples
        :type q_even: bool
        :param envelope_name: name to attach to envelope description
        :type envelope_name: string
        """
        new_dict_item = {"is_interp" : 0, "size" : 0, "is_sym" : 0,
                         "i_even" : 0, "q_even" : 0}

        # check inputs
        if envelope_name in self.EnvelopeMemoryDict.keys():
            print("error, name '" + envelope_name + "' is already in use")
            return -3
        if (envelope_samples.dtype != complex):
            print("error, the provided samples for the envelope are not complex")
            return -3
        
        envelope_size = envelope_samples.size
        if (envelope_size < 2):
            print("error, envelope samples must be greater or eaqual than 2")
            return -3 

        # check requirement for non interpolation envelope size
        if (envelope_size%self.NumberOfChannels != 0 and not(for_interpolation)):
            print("error, envelopes not marked for interpolation must have a number of sample divisible by the generator parlallism")
            print("the number of samples: " + str(envelope_size) + " is not divisible by " + str(self.NumberOfChannels))
            print("HINT: pad the envelope with zeros")
            return -3
        
        if (for_interpolation):
            new_dict_item["is_interp"] = 1
            new_dict_item["size"] = envelope_size
            new_dict_item["is_sym"] = is_symmetric
            new_dict_item["i_even"] = i_even
            new_dict_item["q_even"] = q_even
        else:
            envelope_size = envelope_size // self.NumberOfChannels
            new_dict_item["is_interp"] = 0
            new_dict_item["size"] = envelope_size
            new_dict_item["is_sym"] = 0
            new_dict_item["i_even"] = 0
            new_dict_item["q_even"] = 0
        
        # check that we have enough space in the sample memory
        free_space = self.EnvelopeMemoryDict["_FREESPACE"]["depth"]
        if (free_space < envelope_size):
            print("error, not enough space in the envelope memory. Required space: " 
                  + str(envelope_size) + ", available space: " + str(free_space) )
            return -4
        
        # finish setup of the dictionary entry
        start_address = self.EnvelopeMemoryDict["_FREESPACE"]["start"]
        new_dict_item["start"] = start_address

        # commit to envelope dictionary
        self.EnvelopeMemoryDict[envelope_name] = new_dict_item
        self.EnvelopeMemoryDict["_FREESPACE"]["start"] = start_address + envelope_size
        self.EnvelopeMemoryDict["_FREESPACE"]["size"] = free_space - envelope_size

        # commit to generator sample memory
        to_write_array = (envelope_samples.real.astype(np.int32) << 16) + envelope_samples.imag.astype(np.int16)
        if(is_symmetric and for_interpolation):
            # write the samples to all channels, there is a specific space in the generator memory to do just that
            write_address_start = start_address + self.ChannelSampleMemoryDepth*self.NumberOfChannels
            self.AxiFullInterfaceMMIO.write(write_address_start*4, to_write_array.tobytes())
        else:
            for channel in range(self.NumberOfChannels):
                write_address_start = start_address + self.ChannelSampleMemoryDepth*channel
                to_write_to_channel = to_write_array[channel::self.NumberOfChannels]
                self.AxiFullInterfaceMMIO.write(write_address_start*4, to_write_to_channel.tobytes())
        return 0
    
    def CreateWaveDefinitionWord(self, envelope_name : str, duration: int, gain: float, switch_iq : bool):
        """
        Function to generate a wave definition word, uses cached envelopes stored in envelope memory to
        correctly generate a wave.\n For envelopes not marked for interpolation, it is advised
        to set the duration input to zero, this way the envelope's natural size is used instead. 
        
        :param envelope_name: Name of the envelope precedently stored in envelope memory
        :type envelope_name: str
        :param duration: Duration of the wave in samples, set to 0 to use the size of the envelope
        :type duration: uint
        :param gain: Gain, values between -1 and 1 included
        :type gain: float
        :param switch_iq: Switch the envelope I and Q values, useful for Y-Gates
        :type switch_iq: bool
        :return: Error code
        :rtype: Literal[-3] | int
        """
        wavedef = 0
        # check input parameters
        if (envelope_name not in self.EnvelopeMemoryDict.keys()):
            print("error, the envelope name: " + envelope_name + " was not found in the envelope memory.")
            print("HINT: use the 'WriteEnvelopeMemory' function to add the envelope to memory")
            return -3
        
        
        if (gain < -1 or gain > 1):
            print("error, gain out of range")
            return -3
        
        # handle duration argument, if set to zero the duration will be the
        # natural duration of the envelope
        if ((duration < 2 or duration > self.MaximumDuration) and duration != 0):
            print("error, duration out of range")
            return -3
        
        envelope_def = self.EnvelopeMemoryDict[envelope_name]
        
        # handle gain
        invert = False
        real_gain = 0
        if (gain < 0):
            invert = True
            real_gain = round(-gain*(2**self.SampleSize-1))
        else: 
            invert = False
            real_gain = round(gain*(2**self.SampleSize-1))

        real_duration = 0
        natural_envelope_duration = 0
        # handle special envelope names
        if (envelope_name == "_RECTANGULAR"):
            # set the force one bit
            wavedef = wavedef | (1 << 121)
            real_duration = duration
            natural_envelope_duration = duration
        else: 
            # TODO: duration check if not interp
            if (envelope_def["is_interp"]):
                natural_envelope_duration = envelope_def["size"]*(1 + envelope_def["is_sym"]) - 1
                real_duration = duration
            else:
                natural_envelope_duration = envelope_def["size"]*self.NumberOfChannels
                real_duration = natural_envelope_duration

        # set the symmetric bit
        if (envelope_def["is_sym"]):
            wavedef = wavedef | (1 << 127)
        # set the i_even bit
        if (envelope_def["i_even"]):
            wavedef = wavedef | (1 << 126)
        # set the q_even bit
        if (envelope_def["q_even"]):
            wavedef = wavedef | (1 << 125)
        # set the interpolation bit
        if (envelope_def["is_interp"]):
            wavedef = wavedef | (1 << 120)
        
        # set the iq switch
        if (switch_iq):
            wavedef = wavedef | (1 << 123)
        # set the invert bit
        if (invert): 
            wavedef = wavedef | (1 << 124)
        # set the gain
        wavedef = wavedef | (real_gain << (2*(self.SampleMemoryAddressWidth + self.FractionalPrecision) + self.DurationWidth))
        # set the duration bits
        wavedef = wavedef | ((real_duration - 1) << 2*(self.SampleMemoryAddressWidth + self.FractionalPrecision))
        # set sample generator offsets
        start_offset = 0
        increment = 0
        if (envelope_def["is_interp"]):
            # TODO: handle the reminder
            start_offset = envelope_def["start"] << self.FractionalPrecision
            increment = ((natural_envelope_duration-1) << self.FractionalPrecision)//(real_duration-1)
        else:
            start_offset = envelope_def["start"] << self.FractionalPrecision
            # set the increment to 1/(number_of_channels), usually 1/16
            increment = 1 << (self.FractionalPrecision - self.LogNumberOfChannels)
        # set the start offset and increment bits
        wavedef = wavedef | (start_offset << (self.SampleMemoryAddressWidth + self.FractionalPrecision))
        wavedef = wavedef | increment
        # return wave definition
        return wavedef
    
    def AddWaveInWaveMemory(self, wave_definition : int, wave_name : str):
        """
        Add a wave definition word in the wave memory, there are no checks on the 
        word so the it should only be generated with provided functions
        
        :param wave_definition: Wave definition word, low level definition of a wave
        :type wave_definition: int
        :param wave_name: Name of the wave to add
        :type wave_name: str
        :return: Error code
        :rtype: Literal[-3, 0]
        """
        if wave_name in self.WaveMemoryDict.keys():
            print("error, a wave was found in the cached wave memory with the same name")
            return -3
        
        # get the address where the wave definition will end up
        address = self.WaveMemoryDict["_NEXT"]
        if address == self.WaveMemorySegmentDepth:
            print("error, the wave memory is full")
            return -3
        
        # write to wave memory
        for i in range(4):
            self.AxiFullInterfaceMMIO.write((self.TotalSampleMemorySegmentDepth + i*4 + address), (wave_definition >> (i*32)) & 0xFFFFFFFF)
        
        # write to wave memory cache
        self.WaveMemoryDict[wave_name] = address
        # add 32 bytes (128/8) to address
        self.WaveMemoryDict["_NEXT"] = address + 32
        
        return 0
    
    def WriteWaveToDriveSequence(self, index : int, wave_name : str):
        """
        Write to memory mapped fifo the address of the wave memory containing the wave definiton defined by 
        wave_name
 
        :param index: Sequence index, the first one is 1
        :type index: int
        :param wave_name: Name of the wave definition previously added to wave memory
        :type wave_name: str
        """

        if (index < 1 or index > self.MemoryMappedFifoSegmentDepth//4):
            print("error, the index is out of range")
            return -3
        if wave_name not in self.WaveMemoryDict.keys():
            print("error, a wave was not found in the cached wave memory with the same name")
            print("HINT: use the 'InsertWaveInWaveMemory' function to insert a wave definition word in memory")
            return -3
        if wave_name in self.WaveMemoryDictReservedNames:
            print("wave name is a reserved keyword")
            return -3
        
        # get wave
        wave_addr = self.WaveMemoryDict[wave_name]
        
        # write to memory mapped fifo
        fifo_start_address = self.TotalSampleMemorySegmentDepth + self.WaveMemorySegmentDepth
        actual_address = fifo_start_address + (index-1)*4
        self.AxiFullInterfaceMMIO.write(actual_address, wave_addr)

    def WriteReadoutWave(self, wave_definition : int):
        if wave_definition < 0:
            print("error, wave def is negative")
            return -3
        for i in range(4):
            self.mmio.write((self.readout_wave_l+i)*4, (wave_definition >> i*32) & 0xFFFFFFFF)

    def ReplaceWaveInWaveMemory(self, wave_definition : int, wave_name : str):
        """
        Replace a certain word definition with another one
        
        :param wave_definition: Wave definition word for the new wave
        :type wave_definition: uint128
        :param wave_name: Name of the wave to replace
        :type wave_name: str
        :return: Error code
        :rtype: Literal[-3, 0]
        """
        if wave_name not in self.WaveMemoryDict.keys():
            print("error, a wave was not found in the cached wave memory with the same name")
            print("HINT: use the 'InsertWaveInWaveMemory' function to insert a wave definition word in memory")
            return -3
        if wave_name in self.WaveMemoryDictReservedNames:
            print("wave name is a reserved keyword")
            return -3
        
        # get the address where the wave definition will end up
        address = self.WaveMemoryDict[wave_name]
        
        # write to wave memory
        for i in range(4):
            self.AxiFullInterfaceMMIO.write((self.TotalSampleMemorySegmentDepth + i*4 + address), (wave_definition >> (i*32)) & 0xFFFFFFFF)
        
        return 0

    
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

    bindto = ['user.org:user:axisTriggerGeneratorIP:1.0']

    FifoInterfaceMMIO = None

    def __init__(self, description):

        super().__init__(description=description)
        # parse the number of channels of the trigger generator
        self.TriggerChannels = int(description['parameters']['TriggerWordWidth'])
        # parse the fifo interface depth and create mmio handle
        self.FifoInterfaceMemoryDepth = pow(2,int(description['parameters']['C_S00_AXI_ADDR_WIDTH']))
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

        self.ctrl = 0
        self.experiment_dur_l = 2
        self.experiment_dur_h = 3
        self.readout_delay_l = 4
        self.readout_delay_h = 5
        self.shots_num_l = 1

        # Bit position definition
        self.ManTrigPos = 31

    def WriteDescription(self):
        print("trigger channels: " + str(self.TriggerChannels))
        print("fifo interface axi depth: " + str(self.FifoInterfaceMemoryDepth))
        print("fifo channel depth: " + str(self.ChannelFifoDepth))
        print("maximum number of hardware repetitions: " + str(self.MaxHWRepetitions))
    
    def InitAxiFullInterface(self, base_address : int):
        """
        Initialize the axi full interface for this IP
        
        :param base_address: Base address of the axi full interface
        :type base_address: int
        """
        if self.FifoInterfaceMMIO is None:
            self.FifoInterfaceMMIO = MMIO(base_address, self.FifoInterfaceMemoryDepth)

    def SetExperimentDuration(self,duration):
        """
        Set the experiment duration for a single shot
        
        :param duration: Duration in clock cycles
        :type duration: uint
        """

        # write inc LOW
        self.mmio.write(self.experiment_dur_l*4, duration & 0xFFFFFFFF)
        # write inc HIGH
        self.mmio.write(self.experiment_dur_h*4, duration >> 32)

    def SetNumberOfShots(self, value):
        """
        Set the number of shots to execute in hardware
        
        :param value: number of shots
        :type value: uint
        """
        if(value < 1 or value > self.MaxHWRepetitions):
            print("error: the numer of shots " + str(value) + " is outside of range 1 to " + str(self.MaxHWRepetitions))
            return

        self.mmio.write(self.shots_num_l*4,int(value-1))

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

    def InsertDelayDriveFifo(self, channel, index, delay, generate_trigger):
        """
        Insert a delay value in the FIFO of a drive channel at index. The generate_trigger input is used to 
        tell the trigger generator if a trigger should be generated at the end of the delay
        
        :param channel: Drive channel
        :type channel: uint
        :param index: FIFO index, 1 is the start
        :type index: uint
        :param delay: Delay in clock cycles
        :type delay: uint
        :param generate_trigger: Generates a trigger if set to 1
        :type generate_trigger: Literal[1, 0]
        """
        if (channel< 1 or channel > self.TriggerChannels):
            print("error, channel " + str(channel) + " is outside of range 1 to " + str(self.TriggerChannels))
            return -3

        if (index < 1 or index > self.ChannelFifoDepth):
            print("error, the index is outside of range")
            return -3

        if (delay < 1 or delay > self.DriveDelayMax):
            print("error, the delay is outside of range")
            return -3
        
        real_delay = (delay - 1) | (generate_trigger << 31)
        real_address = (channel-1)*self.ChannelFifoDepth + index - 1
        self.FifoInterfaceMMIO.write(real_address*4, int(real_delay))
        return 0
    
    def SetReadoutDelay(self,delay : int,channel : int):
        """
        Set the experiment duration for a single shot
        
        :param duration: Duration in clock cycles
        :type duration: uint
        """
        if channel < 1 or channel > self.TriggerChannels:
            print("error, channel selection out of range")
            return -3
        # write inc LOW
        self.mmio.write((self.readout_delay_l + (channel-1)*2)*4, delay & 0xFFFFFFFF)
        # write inc HIGH
        self.mmio.write((self.readout_delay_h + (channel-1)*2)*4, delay >> 32)