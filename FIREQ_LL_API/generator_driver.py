from pynq import MMIO
import numpy as np
from ._utils import *

__all__ = ['GeneratorDriver']

class GeneratorDriver(_FIREQDriver):
    """
    Driver class for the generator IP.\n
    Provides methods to set up envelopes, pulse sequences, readout pulse and modulation frequency and phase.
    """

    bindto = ['user.org:user:axisGeneratorIP:2.0']
    
   
    def __init__(self, description):

        super().__init__(description= description)

        # a dictionary that stores useful data about the envelopes that have been written to 
        # the envelope memory
        self.EnvelopeMemoryDict = {}
        self.EnvelopeMemoryDictReservedNames = []
        # a dictionary that stores useful data about the wave definition words that have
        # been written to the sequencer's wave memory
        self.WaveMemoryDict = {}
        self.WaveMemoryDictReservedNames = []
        # the axi interface 
        self.AxiFullInterfaceMMIO = None

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
        # set debug level
        self.DebugLevel = 0

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
    
    def init_axi_full_interface(self, base_address : int, axi_depth : int):
        super().init_axi_full_interface(base_address, axi_depth)
        # reset envelope dictionary and memory
        self.reset_envelope_dict()

    def init_axi_lite_interface(self, base_address : int, axi_depth : int):
        super().init_axi_lite_interface(base_address, axi_depth)
        # delete the mmio object created by PYNQ
        del self.mmio
            

    def print_description(self):
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

    def reset_envelope_dict(self):
        """
        Resets the cached information about the envelope memory. The actual memory is not modified by this function. \n
        Since resetting this memory invalidates the wave definition words, the wave memory cache is also cleared 
         
        """
        self.EnvelopeMemoryDict = {}
        self.EnvelopeMemoryDictReservedNames = []
        
        # set the memory free space in the envelope memory dictionary
        self.EnvelopeMemoryDict["_FREESPACE"] = {"start" : 0, "depth" : self.ChannelSampleMemoryDepth}
        self.EnvelopeMemoryDictReservedNames.append("_FREESPACE")
        # set an entry for rectangular waves
        self.EnvelopeMemoryDict["_RECTANGULAR"] = {"is_interp" : 0, "size" : any, "is_sym" : 0,
                                                   "i_even" : 0, "q_even" : 0, "start" : 0}
        self.EnvelopeMemoryDictReservedNames.append("_RECTANGULAR")
        self.reset_wave_memory_dict()

    def reset_wave_memory_dict(self):
        """
        Resets the cached information about the wave memory and also clears the generator wave memory.

        """
        self.WaveMemoryDict = {}
        self.WaveMemoryDictReservedNames = []
        
        # address of next wave in wave memory
        self.WaveMemoryDict["_NEXT"] = 0
        self.WaveMemoryDictReservedNames.append("_NEXT")
        for address in range(self.TotalSampleMemorySegmentDepth,self.TotalSampleMemorySegmentDepth+self.WaveMemorySegmentDepth,4):
            self.AxiFullInterfaceMMIO.write(address, 0)

    def trigger_manually(self):
        """
        Trigger the generator manually

        :return: Error code
        :rtype: int
        """
        register = self.AxiLiteInterfaceMMIO.read(self.ctrl*4)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, _set_bit(register, self.ManTrigPos, 1))
        return 0

    def set_trigger_channel(self, channel, ttype):
        """
        Set the channel where the generator is listening for a trigger for the readout and drive trigger types.\n
        Set the channel to 0 if you want to disable external triggers

        :param channel: Channel number, 1 to TriggerChannels
        :type channel: int
        :param ttype: trigger type: 'drive' or 'readout'
        :type ttype: int
        :return: Error code
        :rtype: int
        """
        if channel < 0 or channel > self.TriggerChannels:
            print("channel choice is out of range")
            return -3
            
        if ttype == 'drive':
            selector = 0
        elif ttype == 'readout':
            selector = 1
        else:
            print("type choice is out of range")
            return -3
        # write to the control register
        trigger_mask = (1 << channel) >> 1
        control = _set_bits(self.AxiLiteInterfaceMMIO.read(self.ctrl*4), selector*self.TriggerChannels, self.TriggerChannels, trigger_mask)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, control)

        return 0

    def get_trigger_channel(self, ttype):
        """
        get the trigger channel for the generator where triggers are received

        :param ttype: trigger type: 0 for drive, 1 for readout
        :type ttype: int
        :return: Error code
        :rtype: int
        """ 

        if ttype == 'drive':
            selector = 0
        elif ttype == 'readout':
            selector = 1
        else:
            print("type choice is out of range")
            return -3

        cntr = self.AxiLiteInterfaceMMIO.read(self.ctrl)

        channel = _get_bits(cntr, selector*self.TriggerChannels, self.TriggerChannels)

        print("Trigger " + ttype + " mask: " + format(channel, f"0{self.TriggerChannels}b"))

        return 0

    def set_drive_order_source(self, source):
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

        register = _set_bit(value= self.AxiLiteInterfaceMMIO.read(self.ctrl*4), pos= self.SourcePos, setvalue= source)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, register)
        return 0

    def get_drive_order_source(self):
        """
        get the source for the generator: lfsr or fifo

        :return: Error code
        :rtype: int
        """
        cntr = self.AxiLiteInterfaceMMIO.read(self.ctrl*4)
        if _get_bit(cntr, self.SourcePos) == 0:
            print("Source: FIFO")
        else:
            print("Source: LFSR")

        return 0

    def set_lfsr_seed(self, seed):
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
        cntr = self.AxiLiteInterfaceMMIO.read(self.ctrl*4)
        cntr = _set_bits(cntr, 2*self.TriggerChannels, self.SeedLfsrWidth, seed)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, cntr)
        return 0

    def get_lfsr_seed(self):
        """
        get the seed for lfsr

        :return: Error code
        :rtype: int
        """
        cntr = self.AxiLiteInterfaceMMIO.read(self.ctrl)
        cntr = _get_bits(cntr, 2*self.TriggerChannels, self.SeedLfsrWidth)
        print(f"LFSR seed: {cntr}")

        return 0

    def _set_readout_pinc_poff(self, inc, off):
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
        self.AxiLiteInterfaceMMIO.write(self.readout_inc_l*4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self.AxiLiteInterfaceMMIO.write(self.readout_inc_h*4, inc >> 32)

        # write off LOW
        self.AxiLiteInterfaceMMIO.write(self.readout_off_l*4, off & 0xFFFFFFFF)
        # write off HIGH
        self.AxiLiteInterfaceMMIO.write(self.readout_off_h*4, off >> 32)

        return 0

    def _get_readout_pinc_poff(self):
        """
        Get readout phase increment and phase offset values

        :return: Error code
        :rtype: int
        """
        # read inc LOW
        inc = self.AxiLiteInterfaceMMIO.read(self.readout_inc_l*4)
        # read inc HIGH
        inc += self.AxiLiteInterfaceMMIO.read(self.readout_inc_h*4) << 32

        # read off LOW
        off = self.AxiLiteInterfaceMMIO.read(self.readout_off_l*4)
        # read off HIGH
        off += self.AxiLiteInterfaceMMIO.read(self.readout_off_h*4) << 32

        print(f"readout phase increment: {inc}, phase offset: {off}")

        return 0

    def _set_drive_pinc(self, inc):
        """
        Set drive phase increment value, used to generate the 
        modulation carrier for waves on the drive output line

        :param inc: Increment value for readout
        :type inc: int
        :return: Error code
        :rtype: int
        """
        # write inc LOW
        self.AxiLiteInterfaceMMIO.write(self.drive_inc_l*4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self.AxiLiteInterfaceMMIO.write(self.drive_inc_h*4, inc >> 32)

        return 0

    def _get_drive_pinc(self):
        """
        Get drive phase increment value

        :return: Error code
        :rtype: int
        """
        # read inc LOW
        inc = self.AxiLiteInterfaceMMIO.read(self.drive_inc_l*4)
        # read inc HIGH
        inc += self.AxiLiteInterfaceMMIO.read(self.drive_inc_h*4) << 32

        print(f"drive phase increment: {inc}")

        return 0

    def set_manual_wave_destination_output_channel(self, destination):
        """
        Set the destination of a manually generated wave.
        The wave to be generated is the one set for readout but you can select 
        if the manually generated wave should output on the readout or drive line.

        :param destination: 'readout' or 'drive'
        :type destination: int
        :return: Error code
        :rtype: int
        """
        if destination == 'drive':
            selector = 0
        elif destination == 'readout':
            selector = 1
        else:
            print("type choice is out of range")
            return -3

        register = _set_bit(self.AxiLiteInterfaceMMIO.read(self.ctrl*4), pos= self.ManTrigSel, setvalue= selector)
        self.AxiLiteInterfaceMMIO.write(self.ctrl*4, register)
        return 0
    
    def get_manual_wave_destination_output_channel(self):
        """
        Get the destination output line for the generator when triggered from the manual trigger
        
        """
        dest = _get_bit(self.AxiLiteInterfaceMMIO.read(self.ctrl*4), self.ManTrigSel)
        if dest:
            print("Manual trigger destination is readout line")
        else: 
            print("Manual trigger destination is drive line")
        return 0
    
    def set_readout_dds_parameters(self, frequency : float, phase : float, dac_samplerate : int):
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
        value_tuple = _compute_pinc_poff(frequency*1000000, phase, dac_samplerate, self.PhaseDepth)

        # write registers
        self._set_readout_pinc_poff(value_tuple[0],value_tuple[1])
        return 0

    def set_drive_dds_parameters(self, frequency, dac_samplerate):
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
        value_tuple = _compute_pinc_poff(frequency*1000000, 0, dac_samplerate, self.PhaseDepth)

        # write registers
        self._set_drive_pinc(value_tuple[0])
        return 0
    
    def add_envelope_to_envelope_memory(self, envelope_samples : np.ndarray, for_interpolation : bool, is_symmetric : bool, i_even : bool, q_even : bool, envelope_name : str):
        """
        Write to the envelope memory (sample memory) a series of samples to be used to generate a wave. \n
        An envelope description is cached and a name is associated to it. \n
        Important note: symmetric waves should have an odd number of samples and only half of the samples
        (including the center sample) should be passed to this function. \n
        Warning: the values in the array should be representable in int16, if not they will be saturated 
        to the maximum or negative value (values must be within -2^15 and +2^15-1).
        
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
        if not np.iscomplexobj(envelope_samples):
            #NOTE: better than if (envelope_samples.dtype != complex) -> recover in case of any problem
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
        self.EnvelopeMemoryDict["_FREESPACE"]["depth"] = free_space - envelope_size

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
    
    def create_wave_definition_word(self, envelope_name : str, duration: int, gain: float, switch_iq : bool, keep_last: bool = False):
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
        :param keep_last: If True, holds the last value of the envelope indefinitely (CW Mode)
        :type keep_last: bool
        :return: Error code
        :rtype: Literal[-3] | int
        """
        wavedef = 0
        # check input parameters
        if (envelope_name not in self.EnvelopeMemoryDict.keys()):
            print("error, the envelope name: " + envelope_name + " was not found in the envelope memory.")
            print("HINT: use the 'add_envelope_to_envelope_memory' function to add the envelope to memory")
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
            real_gain = round(-gain*(2**self.SampleSize - 1))
        else: 
            invert = False
            real_gain = round(gain*(2**self.SampleSize - 1))

        real_duration = 0
        natural_envelope_duration = 0
        # handle special envelope names
        if (envelope_name == "_RECTANGULAR"):
            if (duration == 0):
                print("error, rectangular wave requires a non-zero duration")
                return -3
            # set the force one bit
            wavedef = wavedef | (1 << 121)
            real_duration = duration
            natural_envelope_duration = duration
        else: 
            # NOTE (non-interpolated envelopes):
            # In non-interpolated mode the read address increment is fixed (typically 1/NumberOfChannels),
            # so the LUT is read sequentially and the waveform length is effectively bounded by the amount
            # of samples stored in memory (natural_envelope_duration).
            #
            # Policy:
            # - duration == 0  -> use natural_envelope_duration (recommended default)
            # - duration < natural_envelope_duration -> allowed (truncates the envelope)
            # - duration > natural_envelope_duration -> NOT allowed because it would read past the loaded data
            #   (undefined samples). If keep_last is enabled, we clamp to natural_envelope_duration and rely on
            #   KEEP_LAST for CW behavior instead of reading out-of-range data.

            if (envelope_def["is_interp"]):
                natural_envelope_duration = envelope_def["size"]*(1 + envelope_def["is_sym"]) - 1
                real_duration = natural_envelope_duration if (duration == 0) else duration
            else:
                natural_envelope_duration = envelope_def["size"]*self.NumberOfChannels
                if (duration == 0):
                    real_duration = natural_envelope_duration
                else:
                    if (duration > natural_envelope_duration):
                        if keep_last:
                            real_duration = natural_envelope_duration
                        else:
                            print("error, duration exceeds envelope length for non-interpolated envelope")
                            return -3
                    else:
                        real_duration = duration

        # set the keep_last bit (Bit 122)
        if keep_last:
            wavedef = wavedef | (1 << 122)

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
            # NOTE (fixed-point interpolation fix):
            # We compute the fractional address increment as num/den in Q(FractionalPrecision).
            # Using integer division (//) truncates the ideal increment, introducing a small
            # quantization error that accumulates along the envelope and biases the last samples.
            # The remainder (num % den) tells us how far we were from the ideal ratio; by adding
            # half of it to start_offset we "center" the quantization error, reducing the peak
            # error at the end without changing the hardware behavior.
            start_offset = envelope_def["start"] << self.FractionalPrecision
            num = ((natural_envelope_duration - 1) << self.FractionalPrecision)
            den = (real_duration - 1)

            increment = num // den
            reminder  = num % den

            # shift di mezzo resto per “centrare” l’errore (riduce il picco finale)
            start_offset = start_offset + (reminder // 2)
        else:
            start_offset = envelope_def["start"] << self.FractionalPrecision
            # set the increment to 1/(number_of_channels), usually 1/16
            increment = 1 << (self.FractionalPrecision - self.LogNumberOfChannels)
        # set the start offset and increment bits
        wavedef = wavedef | (start_offset << (self.SampleMemoryAddressWidth + self.FractionalPrecision))
        wavedef = wavedef | increment
        # return wave definition
        return wavedef
    
    def add_wave_in_wave_memory(self, wave_definition : int, wave_name : str):
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
        # add 16 bytes (128/8) to address
        self.WaveMemoryDict["_NEXT"] = address + (128//8)
        
        return 0
    
    def add_wave_to_drive_wave_sequence(self, index : int, wave_name : str):
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
        # this address is byte aligned but we need it 128-bit aligned
        wave_addr = wave_addr//(128//8)
        # write to memory mapped fifo
        fifo_start_address = self.TotalSampleMemorySegmentDepth + self.WaveMemorySegmentDepth
        actual_address = fifo_start_address + (index-1)*4
        self.AxiFullInterfaceMMIO.write(actual_address, wave_addr)
        return 0

    def write_readout_wave(self, wave_definition : int):
        """
        Write the readout wave definition to the IP.\n
        This is the wave definition that will be used for manual and readout waves.\n
        HINT: for manual waves, you can set the output DAC (readout or drive channel) 
        with the "trigger_manuallyDestination" function.
         
        :param wave_definition: 128-bit wave defintion
        :type wave_definition: intprint("CACHE ENTRY:", soc.envelope_cache(gen_index).get(ENV_NAME))
        :return: Error code
        :rtype: Literal[-3] | None
        """
        if wave_definition < 0:
            print("error, wave def is negative")
            return -3
        for i in range(4):
            self.AxiLiteInterfaceMMIO.write((self.readout_wave_l+i)*4, (wave_definition >> i*32) & 0xFFFFFFFF)

    def replace_wave_in_wave_memory(self, wave_definition: int, old_wave_name: str, new_wave_name: str = None):
        """
        Replace a certain wave definition word with another one.
        Optionally rename the wave key in the local cache dictionary.

        :param wave_definition: Wave definition word for the new wave (uint128)
        :param old_wave_name: Existing wave name to replace
        :param new_wave_name: New wave name (optional). If None or same as old, no rename.
        :return: Error code
        :rtype: Literal[-3, 0]
        """
        if old_wave_name not in self.WaveMemoryDict:
            print("error, a wave was not found in the cached wave memory with the same name")
            print("HINT: use the 'InsertWaveInWaveMemory' function to insert a wave definition word in memory")
            return -3

        if old_wave_name in self.WaveMemoryDictReservedNames:
            print("old wave name is a reserved keyword")
            return -3

        # default: no rename
        if new_wave_name is None:
            new_wave_name = old_wave_name

        # validate rename
        if new_wave_name != old_wave_name:
            if new_wave_name in self.WaveMemoryDictReservedNames:
                print("new wave name is a reserved keyword")
                return -3
            if new_wave_name in self.WaveMemoryDict:
                print("new wave name already exists in cached wave memory")
                return -3

        # get the address where the wave definition will end up
        address = self.WaveMemoryDict[old_wave_name]

        # write to wave memory (same address)
        for i in range(4):
            self.AxiFullInterfaceMMIO.write(
                (self.TotalSampleMemorySegmentDepth + i*4 + address),
                (wave_definition >> (i*32)) & 0xFFFFFFFF
            )

        # update dictionary key if rename requested
        if new_wave_name != old_wave_name:
            self.WaveMemoryDict[new_wave_name] = address
            del self.WaveMemoryDict[old_wave_name]

        return 0
