"""Low-level driver for the FIREQ generator IP."""

import numpy as np

from ._utils import (
    _compute_pinc_poff,
    _FIREQDriver,
    _get_bit,
    _get_bits,
    _set_bit,
    _set_bits,
)

__all__ = ["GeneratorDriver"]


class GeneratorDriver(_FIREQDriver):
    """Driver class for the generator IP.

    Provides methods to set up envelopes, pulse sequences, readout pulse, and
    modulation frequency and phase.
    """

    bindto = ["user.org:user:axisGeneratorIP:2.0"]

    def __init__(self, description: dict[str, object]) -> None:
        """Initialize the GeneratorDriver.

        :param description: Dictionary containing IP parameters and configuration
        :type description: dict
        """
        super().__init__(description=description)

        # a dictionary that stores useful data about the envelopes that have been written to
        # the envelope memory
        self.envelope_memory_dict = {}
        self.envelope_memory_dict_reserved_names = []
        # a dictionary that stores useful data about the wave definition words that have
        # been written to the sequencer's wave memory
        self.wave_memory_dict = {}
        self.wave_memory_dict_reserved_names = []
        # the axi interface
        self._axi_full_interface_mmio = None

        # address width of the envelope memory, word aligned
        self.sample_memory_address_width = int(description["parameters"]["SampleMemoryAddressWidth"])
        # size of the envelope memory for every channel, in words
        self.channel_sample_memory_depth = pow(2, self.sample_memory_address_width)
        # width of the duration
        self.duration_width = int(description["parameters"]["DurationWidth"])
        self.maximum_duration = pow(2, self.duration_width)
        # fractional precision of the interpolator
        self.fractional_precision = int(description["parameters"]["IncrementFractionalPrecision"])
        # width of samples
        self.sample_size = int(description["parameters"]["SampleSize"])
        # number of channels (parallelism of the generator)
        self.log_number_of_channels = int(description["parameters"]["LogNsamplesClock"])
        self.number_of_channels = pow(2, self.log_number_of_channels)
        # width of the phase increment and offset
        self.phase_depth = int(description["parameters"]["PhaseDepth"])
        # number of trigger channels
        self.trigger_channels = int(description["parameters"]["TriggerWordWidth"])
        # axi full interface depth in bytes
        self.axi_full_interface_depth = pow(2, int(description["parameters"]["C_S00_AXI_ADDR_WIDTH"]))
        # size of axi full segments in bytes
        self.total_sample_memory_segment_depth = int(description["parameters"]["TotalSampleMemorySegmentDepth"])
        self.wave_memory_segment_depth = int(description["parameters"]["WaveMemoryDepth"])
        self.memory_mapped_fifo_segment_depth = int(description["parameters"]["MemoryMappedFifoDepth"])
        # width of the lfsr seed
        self.seed_lfsr_width = int(description["parameters"]["MmFifoAndLfsrOutputWidth"])
        # set debug level
        self.debug_level = 0

        # Offset definition
        self.ctrl = 0
        self.readout_wave_l = 1
        self.readout_inc_l = 5
        self.readout_inc_h = 6
        self.readout_off_l = 9
        self.readout_off_h = 10
        self.drive_inc_l = 7
        self.drive_inc_h = 8

        # Bit position definition
        self.source_pos = 27
        self.man_trig_sel = 28
        self.manual_trigger_pos = 31

    def init_axi_full_interface(self, base_address: int, axi_depth: int) -> None:
        """Initialize the AXI Full interface and reset the envelope dictionary.

        :param base_address: Base address of the AXI Full interface
        :type base_address: int
        :param axi_depth: Depth of the AXI interface in bytes
        :type axi_depth: int
        """
        super().init_axi_full_interface(base_address, axi_depth)
        # reset envelope dictionary and memory
        self.reset_envelope_dict()

    def init_axi_lite_interface(self, base_address: int, axi_depth: int) -> None:
        """Initialize the AXI Lite interface and delete the PYNQ-created MMIO object.

        :param base_address: Base address of the AXI Lite interface
        :type base_address: int
        :param axi_depth: Depth of the AXI interface in bytes
        :type axi_depth: int
        """
        super().init_axi_lite_interface(base_address, axi_depth)
        # delete the mmio object created by PYNQ
        del self.mmio

    def print_description(self) -> None:
        """Print a detailed description of the generator IP configuration parameters."""
        print(
            "sample_memory_address_width: "
            + str(self.sample_memory_address_width)
            + ", address width of the envelope memory (word/IQSample aligned)"
        )
        print(
            "channel_sample_memory_depth: "
            + str(self.channel_sample_memory_depth)
            + ", depth of the envelope memory (words/IQSamples aligned)"
        )
        print("maximum_duration: " + str(self.maximum_duration) + ", maximum duration of a wave (samples)")
        print(
            "fractional_precision: "
            + str(self.fractional_precision)
            + ", fractional precision of the interpolator (bits)"
        )
        print("sample_size: " + str(self.sample_size) + ", width of samples (bits)")
        print(
            "number_of_channels: "
            + str(self.number_of_channels)
            + ", parallelism of the generator (samples/clock cycle)"
        )
        print("phase_depth: " + str(self.phase_depth) + ", width of phases (bits)")
        print(
            "trigger_channels: "
            + str(self.trigger_channels)
            + ", number of trigger channels for readout and drive (bits)"
        )
        print("axi_full_interface_depth: " + str(self.axi_full_interface_depth) + ", axi full interface depth (bytes)")
        print(
            "total_sample_memory_segment_depth: "
            + str(self.total_sample_memory_segment_depth)
            + ", envelope memory segment depth (bytes)"
        )
        print(
            "wave_memory_segment_depth: " + str(self.wave_memory_segment_depth) + ", wave memory segment depth (bytes)"
        )
        print(
            "memory_mapped_fifo_segment_depth: "
            + str(self.memory_mapped_fifo_segment_depth)
            + ", memory mapped FIFO segment depth (bytes)"
        )
        print(
            "seed_lfsr_width: "
            + str(self.seed_lfsr_width)
            + ", width of lsfr seed and memory mapped FIFO entries (bits)"
        )

    def reset_envelope_dict(self) -> None:
        """Reset the cached information about the envelope memory.

        The actual memory is not modified by this function. Since resetting this
        memory invalidates the wave definition words, the wave memory cache is also
        cleared.
        """
        self.envelope_memory_dict = {}
        self.envelope_memory_dict_reserved_names = []

        # set the memory free space in the envelope memory dictionary
        self.envelope_memory_dict["_FREESPACE"] = {"start": 0, "depth": self.channel_sample_memory_depth}
        self.envelope_memory_dict_reserved_names.append("_FREESPACE")
        # set an entry for rectangular waves
        self.envelope_memory_dict["_RECTANGULAR"] = {
            "is_interp": 0,
            "size": any,
            "is_sym": 0,
            "i_even": 0,
            "q_even": 0,
            "start": 0,
        }
        self.envelope_memory_dict_reserved_names.append("_RECTANGULAR")
        self.reset_wave_memory_dict()

    def reset_wave_memory_dict(self) -> None:
        """Reset the cached information about the wave memory.

        This also clears the generator wave memory.
        """
        self.wave_memory_dict = {}
        self.wave_memory_dict_reserved_names = []

        # address of next wave in wave memory
        self.wave_memory_dict["_NEXT"] = 0
        self.wave_memory_dict_reserved_names.append("_NEXT")
        for address in range(
            self.total_sample_memory_segment_depth,
            self.total_sample_memory_segment_depth + self.wave_memory_segment_depth,
            4,
        ):
            self._axi_full_interface_mmio.write(address, 0)

    def trigger_manually(self) -> int:
        """Trigger the generator manually.

        :return: Error code
        :rtype: int
        """
        control_register = self._axi_lite_interface_mmio.read(self.ctrl * 4)
        self._axi_lite_interface_mmio.write(self.ctrl * 4, _set_bit(control_register, self.manual_trigger_pos, 1))
        return 0

    def set_trigger_channel(self, channel: int, ttype: str) -> int:
        """Set the channel where the generator listens for triggers.

        Set the channel to 0 if you want to disable external triggers.

        :param channel: Channel number, 1 to trigger_channels
        :type channel: int
        :param ttype: trigger type: 'drive' or 'readout'
        :type ttype: str
        :return: Error code
        :rtype: int
        """
        if channel < 0 or channel > self.trigger_channels:
            print("channel choice is out of range")
            return -3

        if ttype == "drive":
            selector = 0
        elif ttype == "readout":
            selector = 1
        else:
            print("type choice is out of range")
            return -3
        # write to the control register
        trigger_mask = (1 << channel) >> 1
        control_register = _set_bits(
            self._axi_lite_interface_mmio.read(self.ctrl * 4),
            selector * self.trigger_channels,
            self.trigger_channels,
            trigger_mask,
        )
        self._axi_lite_interface_mmio.write(self.ctrl * 4, control_register)

        return 0

    def get_trigger_channel(self, ttype: str) -> int:
        """Get the trigger channel for the generator where triggers are received.

        :param ttype: trigger type: 'drive' or 'readout'
        :type ttype: str
        :return: Error code
        :rtype: int
        """
        if ttype == "drive":
            selector = 0
        elif ttype == "readout":
            selector = 1
        else:
            print("type choice is out of range")
            return -3

        control_register = self._axi_lite_interface_mmio.read(self.ctrl)

        channel = _get_bits(control_register, selector * self.trigger_channels, self.trigger_channels)

        print("Trigger " + ttype + " mask: " + format(channel, f"0{self.trigger_channels}b"))

        return 0

    def set_drive_order_source(self, source: int) -> int:
        """Set the source for the generator: lfsr or fifo.

        :param source: Source for wave, 1 for LFSR 0 for FIFO
        :type source: int
        :return: Error code
        :rtype: int
        """
        if source < 0 or source > 1:
            print("source choice is out of range")
            return -3

        control_register = _set_bit(
            value=self._axi_lite_interface_mmio.read(self.ctrl * 4), pos=self.source_pos, setvalue=source
        )
        self._axi_lite_interface_mmio.write(self.ctrl * 4, control_register)
        return 0

    def get_drive_order_source(self) -> int:
        """Get the source for the generator: lfsr or fifo.

        :return: Error code
        :rtype: int
        """
        control_register = self._axi_lite_interface_mmio.read(self.ctrl * 4)
        if _get_bit(control_register, self.source_pos) == 0:
            print("Source: FIFO")
        else:
            print("Source: LFSR")

        return 0

    def set_lfsr_seed(self, seed: int) -> int:
        """Set the seed for lfsr.

        :param seed: Seed for LFSR
        :type seed: int
        :return: Error code
        :rtype: int
        """
        if seed < 0 or seed > (2**self.seed_lfsr_width - 1):
            print("source choice is out of range")
            return -3
        control_register = self._axi_lite_interface_mmio.read(self.ctrl * 4)
        control_register = _set_bits(control_register, 2 * self.trigger_channels, self.seed_lfsr_width, seed)
        self._axi_lite_interface_mmio.write(self.ctrl * 4, control_register)
        return 0

    def get_lfsr_seed(self) -> int:
        """Get the seed for lfsr.

        :return: Error code
        :rtype: int
        """
        control_register = self._axi_lite_interface_mmio.read(self.ctrl)
        seed_value = _get_bits(control_register, 2 * self.trigger_channels, self.seed_lfsr_width)
        print(f"LFSR seed: {seed_value}")

        return 0

    def _set_readout_pinc_poff(self, inc: int, off: int) -> int:
        """Set readout phase increment and phase offset values.

        These values generate the modulation carrier for waves on the readout output
        line.

        :param inc: Increment value for readout
        :type inc: int
        :param off: Offset value for readout
        :type off: int
        :return: Error code
        :rtype: int
        """
        # write inc LOW
        self._axi_lite_interface_mmio.write(self.readout_inc_l * 4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self._axi_lite_interface_mmio.write(self.readout_inc_h * 4, inc >> 32)

        # write off LOW
        self._axi_lite_interface_mmio.write(self.readout_off_l * 4, off & 0xFFFFFFFF)
        # write off HIGH
        self._axi_lite_interface_mmio.write(self.readout_off_h * 4, off >> 32)

        return 0

    def _get_readout_pinc_poff(self) -> int:
        """Get readout phase increment and phase offset values.

        :return: Error code
        :rtype: int
        """
        # read inc LOW
        inc = self._axi_lite_interface_mmio.read(self.readout_inc_l * 4)
        # read inc HIGH
        inc += self._axi_lite_interface_mmio.read(self.readout_inc_h * 4) << 32

        # read off LOW
        off = self._axi_lite_interface_mmio.read(self.readout_off_l * 4)
        # read off HIGH
        off += self._axi_lite_interface_mmio.read(self.readout_off_h * 4) << 32

        print(f"readout phase increment: {inc}, phase offset: {off}")

        return 0

    def _set_drive_pinc(self, inc: int) -> int:
        """Set drive phase increment value.

        This value generates the modulation carrier for waves on the drive output line.

        :param inc: Increment value for readout
        :type inc: int
        :return: Error code
        :rtype: int
        """
        # write inc LOW
        self._axi_lite_interface_mmio.write(self.drive_inc_l * 4, inc & 0xFFFFFFFF)
        # write inc HIGH
        self._axi_lite_interface_mmio.write(self.drive_inc_h * 4, inc >> 32)

        return 0

    def _get_drive_pinc(self) -> int:
        """Get drive phase increment value.

        :return: Error code
        :rtype: int
        """
        # read inc LOW
        inc = self._axi_lite_interface_mmio.read(self.drive_inc_l * 4)
        # read inc HIGH
        inc += self._axi_lite_interface_mmio.read(self.drive_inc_h * 4) << 32

        print(f"drive phase increment: {inc}")

        return 0

    def set_manual_wave_destination_output_channel(self, destination: str) -> int:
        """Set the destination of a manually generated wave.

        The wave to be generated is the one set for readout but you can select if the
        manually generated wave should output on the readout or drive line.

        :param destination: 'readout' or 'drive'
        :type destination: str
        :return: Error code
        :rtype: int
        """
        if destination == "drive":
            selector = 0
        elif destination == "readout":
            selector = 1
        else:
            print("type choice is out of range")
            return -3

        control_register = _set_bit(
            self._axi_lite_interface_mmio.read(self.ctrl * 4), pos=self.man_trig_sel, setvalue=selector
        )
        self._axi_lite_interface_mmio.write(self.ctrl * 4, control_register)
        return 0

    def get_manual_wave_destination_output_channel(self) -> int:
        """Get the destination output line for manual trigger."""
        dest = _get_bit(self._axi_lite_interface_mmio.read(self.ctrl * 4), self.man_trig_sel)
        if dest:
            print("Manual trigger destination is readout line")
        else:
            print("Manual trigger destination is drive line")
        return 0

    def set_readout_dds_parameters(self, frequency: float, phase: float, dac_samplerate: int) -> int:
        """Set frequency and phase for the readout carrier signal.

        :param frequency: Frequency of the carrier in MHz
        :type frequency: float
        :param phase: Phase in radians
        :type phase: float
        :param dac_samplerate: Sample rate of the dac, in samples per second
        :type dac_samplerate: int
        :return: Error code
        :rtype: Literal[-3, 0]
        """
        # check inputs
        if frequency < 0:
            print("input parameters out of range")
            return -3

        # get poff and pinc
        phase_parameters = _compute_pinc_poff(frequency * 1000000, phase, dac_samplerate, self.phase_depth)

        # write registers
        self._set_readout_pinc_poff(phase_parameters[0], phase_parameters[1])
        return 0

    def set_drive_dds_parameters(self, frequency: float, dac_samplerate: int) -> int:
        """Set modulation frequency for the drive output channel.

        :param frequency: Frequency in MHz
        :type frequency: float
        :param dac_samplerate: Sampling frequency of the dac in samples per second
        :type dac_samplerate: int
        :return: Error code
        :rtype: Literal[-3, 0]
        """
        # check inputs
        if frequency < 0:
            print("input parameters out of range")
            return -3

        # get poff and pinc
        phase_parameters = _compute_pinc_poff(frequency * 1000000, 0, dac_samplerate, self.phase_depth)

        # write registers
        self._set_drive_pinc(phase_parameters[0])
        return 0

    def add_envelope_to_envelope_memory(
        self,
        envelope_samples: np.ndarray,
        for_interpolation: bool,
        is_symmetric: bool,
        i_even: bool,
        q_even: bool,
        envelope_name: str,
    ) -> int:
        """Write samples into envelope memory for wave generation.

        An envelope description is cached and a name is associated to it. Symmetric
        waves should have an odd number of samples and only half of the samples
        (including the center sample) should be passed to this function.

        Warning: values should be representable in int16, otherwise they will be
        saturated to the maximum or minimum value (values must be within -2^15 and
        +2^15-1).

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
        new_dict_item = {"is_interp": 0, "size": 0, "is_sym": 0, "i_even": 0, "q_even": 0}

        # check inputs
        if envelope_name in self.envelope_memory_dict.keys():
            print("error, name '" + envelope_name + "' is already in use")
            return -3
        if not np.iscomplexobj(envelope_samples):
            # NOTE: better than if (envelope_samples.dtype != complex) -> recover in case of any problem
            print("error, the provided samples for the envelope are not complex")
            return -3

        envelope_size = envelope_samples.size
        if envelope_size < 2:
            print("error, envelope samples must be greater or eaqual than 2")
            return -3

        # check requirement for non interpolation envelope size
        if envelope_size % self.number_of_channels != 0 and not for_interpolation:
            print(
                "error, envelopes not marked for interpolation must have a number of "
                "sample divisible by the generator parallelism"
            )
            print(
                "the number of samples: " + str(envelope_size) + " is not divisible by " + str(self.number_of_channels)
            )
            print("HINT: pad the envelope with zeros")
            return -3

        if for_interpolation:
            new_dict_item["is_interp"] = 1
            new_dict_item["size"] = envelope_size
            new_dict_item["is_sym"] = is_symmetric
            new_dict_item["i_even"] = i_even
            new_dict_item["q_even"] = q_even
        else:
            envelope_size = envelope_size // self.number_of_channels
            new_dict_item["is_interp"] = 0
            new_dict_item["size"] = envelope_size
            new_dict_item["is_sym"] = 0
            new_dict_item["i_even"] = 0
            new_dict_item["q_even"] = 0

        # check that we have enough space in the sample memory
        free_space = self.envelope_memory_dict["_FREESPACE"]["depth"]
        if free_space < envelope_size:
            print(
                "error, not enough space in the envelope memory. Required space: "
                + str(envelope_size)
                + ", available space: "
                + str(free_space)
            )
            return -4

        # finish setup of the dictionary entry
        start_address = self.envelope_memory_dict["_FREESPACE"]["start"]
        new_dict_item["start"] = start_address

        # commit to envelope dictionary
        self.envelope_memory_dict[envelope_name] = new_dict_item
        self.envelope_memory_dict["_FREESPACE"]["start"] = start_address + envelope_size
        self.envelope_memory_dict["_FREESPACE"]["depth"] = free_space - envelope_size

        # commit to generator sample memory
        to_write_array = (envelope_samples.real.astype(np.int32) << 16) + envelope_samples.imag.astype(np.int16)
        if is_symmetric and for_interpolation:
            # write the samples to all channels, there is a specific space in the generator memory to do just that
            write_address_start = start_address + self.channel_sample_memory_depth * self.number_of_channels
            self._axi_full_interface_mmio.write(write_address_start * 4, to_write_array.tobytes())
        else:
            for channel in range(self.number_of_channels):
                write_address_start = start_address + self.channel_sample_memory_depth * channel
                to_write_to_channel = to_write_array[channel :: self.number_of_channels]
                self._axi_full_interface_mmio.write(write_address_start * 4, to_write_to_channel.tobytes())
        return 0

    def create_wave_definition_word(
        self, envelope_name: str, duration: int, gain: float, switch_iq: bool, keep_last: bool = False
    ) -> int:
        """Generate a wave definition word using cached envelopes.

        For envelopes not marked for interpolation, it is advised to set the duration
        input to zero, this way the envelope's natural size is used instead.

        :param envelope_name: Name of the envelope precedently stored in envelope memory
        :type envelope_name: str
        :param duration: Duration of the wave in samples, set to 0 to use the size of
            the envelope
        :type duration: uint
        :param gain: Gain, values between -1 and 1 included
        :type gain: float
        :param switch_iq: Switch the envelope I and Q values, useful for Y-Gates
        :type switch_iq: bool
        :param keep_last: If True, holds the last value of the envelope indefinitely (CW
            Mode)
        :type keep_last: bool
        :return: Error code
        :rtype: Literal[-3] | int
        """
        wavedef = 0
        # check input parameters
        if envelope_name not in self.envelope_memory_dict.keys():
            print("error, the envelope name: " + envelope_name + " was not found in the envelope memory.")
            print("HINT: use the 'add_envelope_to_envelope_memory' function to add the envelope to memory")
            return -3

        if gain < -1 or gain > 1:
            print("error, gain out of range")
            return -3

        # handle duration argument, if set to zero the duration will be the
        # natural duration of the envelope
        if (duration < 2 or duration > self.maximum_duration) and duration != 0:
            print("error, duration out of range")
            return -3

        envelope_def = self.envelope_memory_dict[envelope_name]

        # handle gain
        invert = False
        real_gain = 0
        if gain < 0:
            invert = True
            real_gain = round(-gain * (2**self.sample_size - 1))
        else:
            invert = False
            real_gain = round(gain * (2**self.sample_size - 1))

        real_duration = 0
        natural_envelope_duration = 0
        # handle special envelope names
        if envelope_name == "_RECTANGULAR":
            if duration == 0:
                print("error, rectangular wave requires a non-zero duration")
                return -3
            # set the force one bit
            wavedef = wavedef | (1 << 121)
            real_duration = duration
            natural_envelope_duration = duration
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

        elif envelope_def["is_interp"]:
            natural_envelope_duration = envelope_def["size"] * (1 + envelope_def["is_sym"]) - 1
            real_duration = natural_envelope_duration if (duration == 0) else duration
        else:
            natural_envelope_duration = envelope_def["size"] * self.number_of_channels
            if duration == 0:
                real_duration = natural_envelope_duration
            elif duration > natural_envelope_duration:
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
        if envelope_def["is_sym"]:
            wavedef = wavedef | (1 << 127)
        # set the i_even bit
        if envelope_def["i_even"]:
            wavedef = wavedef | (1 << 126)
        # set the q_even bit
        if envelope_def["q_even"]:
            wavedef = wavedef | (1 << 125)
        # set the interpolation bit
        if envelope_def["is_interp"]:
            wavedef = wavedef | (1 << 120)

        # set the iq switch
        if switch_iq:
            wavedef = wavedef | (1 << 123)
        # set the invert bit
        if invert:
            wavedef = wavedef | (1 << 124)
        # set the gain
        wavedef = wavedef | (
            real_gain << (2 * (self.sample_memory_address_width + self.fractional_precision) + self.duration_width)
        )
        # set the duration bits
        wavedef = wavedef | ((real_duration - 1) << 2 * (self.sample_memory_address_width + self.fractional_precision))
        # set sample generator offsets
        start_offset = 0
        increment = 0
        if envelope_def["is_interp"]:
            # NOTE (fixed-point interpolation fix):
            # We compute the fractional address increment as num/den in Q(FractionalPrecision).
            # Using integer division (//) truncates the ideal increment, introducing a small
            # quantization error that accumulates along the envelope and biases the last samples.
            # The remainder (num % den) tells us how far we were from the ideal ratio; by adding
            # half of it to start_offset we "center" the quantization error, reducing the peak
            # error at the end without changing the hardware behavior.
            start_offset = envelope_def["start"] << self.fractional_precision
            num = (natural_envelope_duration - 1) << self.fractional_precision
            den = real_duration - 1

            increment = num // den
            reminder = num % den

            # shift by half remainder to "center" the error (reduces peak error at the end)
            start_offset = start_offset + (reminder // 2)
        else:
            start_offset = envelope_def["start"] << self.fractional_precision
            # set the increment to 1/(number_of_channels), usually 1/16
            increment = 1 << (self.fractional_precision - self.log_number_of_channels)
        # set the start offset and increment bits
        wavedef = wavedef | (start_offset << (self.sample_memory_address_width + self.fractional_precision))
        wavedef = wavedef | increment
        # return wave definition
        return wavedef

    def create_vz_gate_definition_word(self, delta_phase_rad: float) -> int:
        """Create a Wave Definition Word (WDW) for a Virtual-Z gate.

        WDW VZ format:
        - Bit 119: IS_VZ_GATE = 1
        - Bits [47:0]: PHASE_OFFSET (48-bit, two's complement)

        Hardware meaning:
        - During the experiment, for one cycle the carrier phase accumulator adds PHASE_OFFSET,
          effectively implementing an instantaneous Z rotation (virtual frame update).

        :param delta_phase_rad: Desired Z rotation in radians (can be any real number).
        :return: 128-bit WDW packed into Python int
        """
        IS_VZ_GATE_BIT = 119
        PHASE_W = 48  # phase offset width required by the IP (DSP accumulator is 48-bit)

        # Map radians -> signed int48 phase word.
        # Natural convention for an N-bit phase accumulator:
        #   2π rad  <->  2^N counts
        full_scale = 1 << PHASE_W
        phase_word = int(np.round(delta_phase_rad * full_scale / (2.0 * np.pi)))

        # Wrap into signed range [-2^(W-1), 2^(W-1)-1]
        half = 1 << (PHASE_W - 1)
        phase_word = ((phase_word + half) % (1 << PHASE_W)) - half

        # Convert to unsigned two's complement for packing in bits [47:0]
        phase_word_u = phase_word & ((1 << PHASE_W) - 1)

        wavedef = 0
        wavedef |= 1 << IS_VZ_GATE_BIT
        wavedef |= phase_word_u  # occupies bits [47:0]

        return wavedef

    def add_wave_in_wave_memory(self, wave_definition: int, wave_name: str) -> int:
        """Add a wave definition word to the wave memory.

        There are no checks on the word so it should only be generated with provided
        functions.

        :param wave_definition: Wave definition word, low level definition of a wave
        :type wave_definition: int
        :param wave_name: Name of the wave to add
        :type wave_name: str
        :return: Error code
        :rtype: Literal[-3, 0]
        """
        if wave_name in self.wave_memory_dict.keys():
            print("error, a wave was found in the cached wave memory with the same name")
            return -3

        # get the address where the wave definition will end up
        address = self.wave_memory_dict["_NEXT"]
        if address == self.wave_memory_segment_depth:
            print("error, the wave memory is full")
            return -3

        # write to wave memory
        for i in range(4):
            self._axi_full_interface_mmio.write(
                (self.total_sample_memory_segment_depth + i * 4 + address), (wave_definition >> (i * 32)) & 0xFFFFFFFF
            )

        # write to wave memory cache
        self.wave_memory_dict[wave_name] = address
        # add 16 bytes (128/8) to address
        self.wave_memory_dict["_NEXT"] = address + (128 // 8)

        return 0

    def add_wave_to_drive_wave_sequence(self, index: int, wave_name: str) -> int:
        """Write to memory mapped FIFO the address of a wave.

        :param index: Sequence index, the first one is 1
        :type index: int
        :param wave_name: Name of the wave definition previously added to wave memory
        :type wave_name: str
        """
        if index < 1 or index > self.memory_mapped_fifo_segment_depth // 4:
            print("error, the index is out of range")
            return -3
        if wave_name not in self.wave_memory_dict.keys():
            print("error, a wave was not found in the cached wave memory with the same name")
            print("HINT: use the 'InsertWaveInWaveMemory' function to insert a wave definition word in memory")
            return -3
        if wave_name in self.wave_memory_dict_reserved_names:
            print("wave name is a reserved keyword")
            return -3

        # get wave
        wave_addr = self.wave_memory_dict[wave_name]
        # this address is byte aligned but we need it 128-bit aligned
        wave_addr = wave_addr // (128 // 8)
        # write to memory mapped fifo
        fifo_start_address = self.total_sample_memory_segment_depth + self.wave_memory_segment_depth
        actual_address = fifo_start_address + (index - 1) * 4
        self._axi_full_interface_mmio.write(actual_address, wave_addr)
        return 0

    def write_readout_wave(self, wave_definition: int) -> int | None:
        """Write the readout wave definition to the IP.

        This is the wave definition that will be used for manual and readout waves.
        HINT: for manual waves, you can set the output DAC (readout or drive channel)
        with the "trigger_manuallyDestination" function.

        :param wave_definition: 128-bit wave defintion
        :type wave_definition: int
        :return: Error code
        :rtype: Literal[-3] | None
        """
        if wave_definition < 0:
            print("error, wave def is negative")
            return -3
        for i in range(4):
            self._axi_lite_interface_mmio.write((self.readout_wave_l + i) * 4, (wave_definition >> i * 32) & 0xFFFFFFFF)

    def replace_wave_in_wave_memory(
        self, wave_definition: int, old_wave_name: str, new_wave_name: str | None = None
    ) -> int:
        """Replace a wave definition word with another one.

        Optionally rename the wave key in the local cache dictionary.

        :param wave_definition: Wave definition word for the new wave (uint128)
        :param old_wave_name: Existing wave name to replace
        :param new_wave_name: New wave name (optional). If None or same as old, no
            rename.
        :return: Error code
        :rtype: Literal[-3, 0]
        """
        if old_wave_name not in self.wave_memory_dict:
            print("error, a wave was not found in the cached wave memory with the same name")
            print("HINT: use the 'InsertWaveInWaveMemory' function to insert a wave definition word in memory")
            return -3

        if old_wave_name in self.wave_memory_dict_reserved_names:
            print("old wave name is a reserved keyword")
            return -3

        # default: no rename
        if new_wave_name is None:
            new_wave_name = old_wave_name

        # validate rename
        if new_wave_name != old_wave_name:
            if new_wave_name in self.wave_memory_dict_reserved_names:
                print("new wave name is a reserved keyword")
                return -3
            if new_wave_name in self.wave_memory_dict:
                print("new wave name already exists in cached wave memory")
                return -3

        # get the address where the wave definition will end up
        address = self.wave_memory_dict[old_wave_name]

        # write to wave memory (same address)
        for i in range(4):
            self._axi_full_interface_mmio.write(
                (self.total_sample_memory_segment_depth + i * 4 + address), (wave_definition >> (i * 32)) & 0xFFFFFFFF
            )

        # update dictionary key if rename requested
        if new_wave_name != old_wave_name:
            self.wave_memory_dict[new_wave_name] = address
            del self.wave_memory_dict[old_wave_name]

        return 0
