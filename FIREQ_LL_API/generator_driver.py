"""Low-level driver for the FIREQ generator IP."""

import logging

import numpy as np
from pynq import MMIO

from ._utils import (
    _FIREQDriver,
    _set_bit,
    _set_bits,
)

__all__ = ["GeneratorDriver"]


class GeneratorDriver(_FIREQDriver):
    """Driver class for the generator IP.

    Provides low-level methods to control the generator IP.
    """

    bindto = ["user.org:user:axisGeneratorIP:2.0"]

    # Offset definition
    _ctrl = 0
    _readout_wave_l = 1
    _readout_inc_l = 5
    _readout_inc_h = 6
    _readout_off_l = 9
    _readout_off_h = 10
    _drive_inc_l = 7
    _drive_inc_h = 8
    _trigger_mask = 11

    # Bit position definition - control register
    man_trig_sel = 28
    source_pos = 27
    manual_trigger_pos = 31
    seed_lfsr_pos = 0

    # Bit position definition - dac_mask in wdw
    dac_mask_msb = 118

    # Port name of the fabric clock
    fabric_clock_port = "HS_axi_clock"

    def __init__(self, description: dict[str, object]) -> None:
        """Initialize the GeneratorDriver.

        :param description: Dictionary containing IP parameters and configuration
        :type description: dict
        """
        super().__init__(description=description)

        # set the axi full interface
        self.axi_full_initialized = False
        self._envelope_memory_interface = None
        self._common_envelope_memory_interface = None
        self._memory_mapped_fifo_interface = None
        self._wdw_memory_interface = None

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
        # number of dac
        self.num_dacs = int(description["parameters"]["NumDacs"])
        # set debug level
        self.debug_level = 0

    def init_axi_full_interface(self, base_address: int, axi_depth: int) -> None:
        """Initialize the AXI Full interface segments and clear memory.

        :param base_address: Base address of the AXI Full interface
        :type base_address: int
        :param axi_depth: Depth of the AXI interface in bytes
        :type axi_depth: int
        """

        def _next_segment(mmio: MMIO) -> int:
            return mmio.base_addr + mmio.length

        self._envelope_memory_interface = MMIO(
            base_address, self.channel_sample_memory_depth * self.number_of_channels * 4
        )
        self._common_envelope_memory_interface = MMIO(
            _next_segment(self._envelope_memory_interface), self.channel_sample_memory_depth * 4
        )
        self._wdw_memory_interface = MMIO(
            _next_segment(self._common_envelope_memory_interface), self.wave_memory_segment_depth
        )
        self._memory_mapped_fifo_interface = MMIO(
            _next_segment(self._wdw_memory_interface), self.memory_mapped_fifo_segment_depth
        )
        self.axi_full_initialized = True
        self.log.debug("AXI Full interface initialized")
        # reset envelope dictionary and memory
        self.clear_envelope_memory()

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

    def write_envelope_memory(self, start_address: int, envelope: np.ndarray, common: bool = False) -> int:
        """Write samples to the envelope memory starting at the given address.

        :param start_address: Start address in the envelope memory (word aligned)
        :type start_address: int
        :param envelope: Array of complex samples to write
        :type envelope: np.ndarray
        :param common: If True, it write the envelope in all parallelization sub-channels (envelope for interpolation)
        :type common: bool
        :return: Error code
        :rtype: int
        """
        if not self.axi_full_initialized:
            self.log.error("AXI Full interface not initialized")
            return -3

        if common:
            size = len(envelope)
        else:
            size = int(np.ceil(len(envelope) / self.number_of_channels))

        if start_address < 0:
            self.log.error("start_address %s cannot be negative", start_address)
            return -3

        if start_address + size > self.channel_sample_memory_depth:
            self.log.error("data exceeds memory bounds")
            return -3

        to_write_array = (envelope.real.astype(np.int32) << 16) + envelope.imag.astype(np.int16)
        if common:
            # write the samples to all sub-channels, there is a specific space in the generator memory to do just that
            self._common_envelope_memory_interface.write(start_address * 4, to_write_array.tobytes())
        else:
            for channel in range(self.number_of_channels):
                write_address_start = start_address + self.channel_sample_memory_depth * channel
                to_write_to_channel = to_write_array[channel :: self.number_of_channels]
                self._envelope_memory_interface.write(write_address_start * 4, to_write_to_channel.tobytes())

        self.log.debug(
            "wrote %s samples to envelope memory at address %s (common: %s)", len(envelope), start_address, common
        )

        return 0

    def clear_envelope_memory(self) -> int:
        """Clear the envelope memory by writing all zeros to it."""
        rval = self.write_envelope_memory(0, np.zeros(self.channel_sample_memory_depth, dtype=complex), common=True)
        if rval != 0:
            self.log.error("failed to clear envelope memory")
            return rval

        self.log.debug("cleared envelope memory")
        return 0

    def trigger_manually(self) -> int:
        """Trigger the generator manually.

        :return: Error code
        :rtype: int
        """
        control_register = self._axi_lite_interface_mmio.read(self._ctrl * 4)
        self._axi_lite_interface_mmio.write(self._ctrl * 4, _set_bit(control_register, self.manual_trigger_pos, 1))

        self.log.debug("generation triggered manually")

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
            self.log.error("channel choice %s is out of range", channel)
            return -3

        if ttype == "drive":
            start_bit = 0
        elif ttype == "readout":
            start_bit = self.trigger_channels
        else:
            self.log.error("trigger type %s is out of range", ttype)
            return -3

        # write to the control register
        trigger_mask = (1 << channel) >> 1

        reg_value = self._axi_lite_interface_mmio.read(self._trigger_mask * 4)

        control_register = _set_bits(
            reg_value,
            start_bit,
            self.trigger_channels,
            trigger_mask,
        )

        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)

        self.log.debug("set the trigger channel to %s for %s", channel, ttype)

        return 0

    def set_drive_order_source(self, source: str) -> int:
        """Set the source for the generator: lfsr or fifo.

        :param source: Source for wave, 'fifo' or 'lfsr'
        :type source: str
        :return: Error code
        :rtype: int
        """
        if source == "fifo":
            selector = 0
        elif source == "lfsr":
            selector = 1
        else:
            self.log.error("source type %s is out of range", source)
            return -3

        control_register = _set_bit(
            value=self._axi_lite_interface_mmio.read(self._ctrl * 4),
            pos=self.source_pos,
            setvalue=selector,
        )
        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)

        self.log.debug("set generation order source to %s(%s)", source, selector)

        return 0

    def set_lfsr_seed(self, seed: int) -> int:
        """Set the seed for lfsr.

        :param seed: Seed for LFSR
        :type seed: int
        :return: Error code
        :rtype: int
        """
        if seed < 0 or seed > (2**self.seed_lfsr_width - 1):
            self.log.error("seed choice %s is out of range", seed)
            return -3

        control_register = self._axi_lite_interface_mmio.read(self._ctrl * 4)
        control_register = _set_bits(control_register, self.seed_lfsr_pos, self.seed_lfsr_width, seed)
        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)

        self.log.debug("set the lfsr seed to %s", seed)

        return 0

    def set_readout_modulation_frequency(self, frequency: float) -> int:
        """Set modulation carrier of readout wave.

        :param frequency: Frequency of the modulation signal, normalized to the DAC sampling frequency
        :type frequency: float
        :return: Error code (0 on success)
        :rtype: int
        """
        normalized_frequency = frequency % 1.0

        # compute the phase increment
        pinc = int(2**self.phase_depth * normalized_frequency)

        # write inc LOW
        self._axi_lite_interface_mmio.write(self._drive_inc_l * 4, pinc & 0xFFFFFFFF)
        # write inc HIGH
        self._axi_lite_interface_mmio.write(self._drive_inc_l * 4, pinc >> 32)

        # log with deferred formatting string arguments to avoid eager evaluation
        self.log.debug("set frequency to %s and phase increment to %s", frequency, pinc)

        return 0

    def set_readout_modulation_initial_phase(self, phase: float) -> int:
        """Set readout modulation initial phase.

        :param phase: Phase offset of the modulation signal, normalized to pi
        :type frequency: float
        :return: Error code (0 on success)
        :rtype: int
        """
        # TODO: check that this is ok even when moving away from the first nyquist zone.
        #       I am unsure if I have to invert the phase or not when in the second nyquist zone.
        poff = int(2**self.phase_depth * phase)

        # write inc LOW
        self._axi_lite_interface_mmio.write(self._readout_off_l * 4, poff & 0xFFFFFFFF)
        # write inc HIGH
        self._axi_lite_interface_mmio.write(self._readout_off_h * 4, poff >> 32)

        self.log.debug("set initial phase to %s and phase offset to %s", phase, poff)

        return 0

    def set_drive_modulation_frequency(self, frequency: float) -> int:
        """Set the modulation frequency for the drive pulses.

        :param frequency: Frequency of the modulation signal, normalized to the DAC sampling frequency
        :type frequency: float
        :return: Error code (0 on success)
        :rtype: int
        """
        normalized_frequency = frequency % 1.0

        # compute the phase increment
        pinc = int(2**self.phase_depth * normalized_frequency)

        # write inc LOW
        self._axi_lite_interface_mmio.write(self._readout_inc_l * 4, pinc & 0xFFFFFFFF)
        # write inc HIGH
        self._axi_lite_interface_mmio.write(self._readout_inc_h * 4, pinc >> 32)

        # log with deferred formatting string arguments to avoid eager evaluation
        self.log.debug("set frequency to %s and phase increment to %s", frequency, pinc)

        return 0

    def set_manual_wave_destination(self, destination: str) -> int:
        """Set the destination of a manually generated wave.

        When manually triggered, the readout wdw is generated but can be sent to
        either the drive or readout output channel. This function sets the destination.

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
            self.log.error("destination %s is out of range", destination)
            return -3

        control_register = _set_bit(
            self._axi_lite_interface_mmio.read(self._ctrl * 4),
            pos=self.man_trig_sel,
            setvalue=selector,
        )
        self._axi_lite_interface_mmio.write(self._ctrl * 4, control_register)

        self.log.debug("set manual wave destination to %s(%s)", destination, selector)

        return 0

    def add_wave_in_wave_memory(self, wdw: int, wdw_index: int) -> int:
        """Add a wave definition word to the wave memory.

        No checks are performed on the wave definition word.
        The index is the address aligned to a 128 bit boundary.

        :param wdw: Wave definition word, low level definition of a wave
        :type wdw: int
        :param wdw_index: Wave address/index, aligned to 128 bits
        :type wdw_index: int
        :return: Error code
        :rtype: Literal[-3, 0]
        """
        if wdw_index < 0 or wdw_index >= self.wave_memory_segment_depth // (128 // 8):
            self.log.error("index %s is out of range", wdw_index)
            return -3

        # write to wave memory
        self._wdw_memory_interface.write(wdw_index * (128 // 8), wdw.to_bytes(128 // 8, "little"))

        self.log.debug("added wave definition %s to wave memory at index %s", wdw, wdw_index)

        return 0

    def add_wave_to_drive_wave_sequence(self, order_index: int, wdw_index: int) -> int:
        """Write to memory mapped FIFO the address of a wdw at a certain point in the generation order.

        No checks are performed ensurring that the wdw actually exists at that index.

        :param order_index: 0 refers to the first wdw generated.
        :type order_index: int
        :param wdw_index: Index of the wdw in the wave memory.
        :type wdw_index: int
        """
        if order_index < 0 or order_index >= self.memory_mapped_fifo_segment_depth // 4:
            self.log.error("order index %s is out of range", order_index)
            return -3
        if wdw_index < 0 or wdw_index >= self.wave_memory_segment_depth // (128 // 8):
            self.log.error("wdw index %s is out of range", wdw_index)
            return -3

        self._memory_mapped_fifo_interface.write(order_index * 4, wdw_index)
        return 0

    def write_readout_wave(self, wave_definition: int) -> int | None:
        """Write the readout wave definition to the IP.

        This is the wave definition that will be used for manual and readout waves.
        HINT: for manual waves, you can set the output channel (readout or drive channel)
        with the "trigger_manuallyDestination" function.

        :param wave_definition: 128-bit wave defintion
        :type wave_definition: int
        :return: Error code
        :rtype: Literal[-3] | None
        """
        if wave_definition < 0:
            self.log.error("wave definition %s is out of range", wave_definition)
            return -3
        for i in range(4):
            self._axi_lite_interface_mmio.write(
                (self._readout_wave_l + i) * 4, (wave_definition >> i * 32) & 0xFFFFFFFF
            )

        self.log.debug("wrote the readout wave definition to the IP %s", wave_definition)

        return 0

    def build_envelope_specific_wdw(
        self,
        is_symmetric: bool,
        i_even: bool,
        q_even: bool,
        forceone: bool,
        interpolate: bool,
    ) -> int:
        """Build the envelope specific part of the wdw.

        :param is_symmetric: If True, the envelope is symmetric
        :type is_symmetric: bool
        :param i_even: If True, the I sample symmetry is even
        :type i_even: bool
        :param q_even: If True, the Q sample symmetry is even
        :type q_even: bool
        :param switch_iq: If True, the I and Q channels are switched
        :type switch_iq: bool
        :param forceone: If True, the envelope is forced to be one
        :type forceone: bool
        :param interpolate: If True, the envelope is interpolated
        :type interpolate: bool
        :return: Envelope specific part of the wdw
        :rtype: int
        """
        wdw = int(is_symmetric) << 127
        wdw |= int(i_even) << 126
        wdw |= int(q_even) << 125
        wdw |= int(forceone) << 121
        wdw |= int(interpolate) << 120
        return wdw

    def build_vz_wdw(self, normalized_phase: float) -> int:
        """Build the wave definition word for vz gates.

        :param normalized_phase: Phase of the vz gate normalized to 2pi
        :type normalized_phase: float
        :return: Wave definition word
        :rtype: int
        """
        wdw = 1 << 119
        phase = int(normalized_phase * 2 ** (self.phase_depth))
        wdw |= phase & (2**self.phase_depth - 1)
        return wdw

    def build_pulse_wdw(
        self,
        envelope_wdw: int,
        for_interpolation: bool,
        start_address: int,
        duration: int,
        natural_duration: int,
        normalized_gain: float,
        switch_iq: bool,
        keep_last: bool,
    ) -> int:
        """Build the wave definition word for pulses.

        :param envelope_wdw: Envelope specific part of the wdw
        :type envelope_wdw: int
        :param start_address: Start address of the envelope in the envelope memory
        :type start_address: int
        :param duration: Duration of the pulse in samples
        :type duration: int
        :param natural_duration: Natural duration of the pulse in samples
        :type natural_duration: int
        :param normalized_gain: Normalized gain of the pulse
        :type normalized_gain: float
        :param switch_iq: If True, the I and Q channels are switched
        :type switch_iq: bool
        :param keep_last: If True, the last sample is kept
        :type keep_last: bool
        :return: Wave definition word
        :rtype: int
        """
        wdw = envelope_wdw
        if normalized_gain < 0:
            wdw |= 1 << 124
            gain = -normalized_gain
        else:
            gain = normalized_gain
        wdw |= int(switch_iq) << 123
        wdw |= int(keep_last) << 122
        if gain >= 1:
            gain = 2**self.sample_size - 1
        else:
            gain = int(2**self.sample_size * gain)
        wdw |= (gain & (2**self.sample_size - 1)) << 90
        # get the duration
        if duration > self.maximum_duration:
            self.log.warning("duration %s is out of range", duration)
            real_duration = self.maximum_duration
        else:
            real_duration = duration
        wdw |= (real_duration - 1) << 2 * (self.sample_memory_address_width + self.fractional_precision)
        # set sample generator offsets
        start_offset = 0
        increment = 0
        if for_interpolation:
            # NOTE (fixed-point interpolation fix):
            # We compute the fractional address increment as num/den in Q(FractionalPrecision).
            # Using integer division (//) truncates the ideal increment, introducing a small
            # quantization error that accumulates along the envelope and biases the last samples.
            # The remainder (num % den) tells us how far we were from the ideal ratio; by adding
            # half of it to start_offset we "center" the quantization error, reducing the peak
            # error at the end without changing the hardware behavior.
            start_offset = start_address << self.fractional_precision
            num = (natural_duration - 1) << self.fractional_precision
            den = real_duration - 1

            increment = num // den
            reminder = num % den

            # shift by half remainder to "center" the error (reduces peak error at the end)
            start_offset = start_offset + (reminder // 2)
        else:
            start_offset = start_address << self.fractional_precision
            # set the increment to 1/(number_of_channels), usually 1/16
            increment = 1 << (self.fractional_precision - self.log_number_of_channels)
        # set the start offset and increment bits
        wdw |= start_offset << (self.sample_memory_address_width + self.fractional_precision)
        wdw |= increment
        return wdw

    def set_dac_mask(self, envelope_wdw: int, mask: int) -> int:
        """Set the DAC mask for drive or readout outputs.
        For example, if you have 4 DACs and want to activate the first and third, you should set the mask to 0b0101 (5 in decimal)
        :param envelope_wdw: Wave definition word
        :type envelope_wdw: int
        :param mask: Bitmask of the DACs to activate, from the least significant bit.
        :type mask: int
        :return: Error code
        :rtype: int
        """

        wdw = envelope_wdw

        if mask < 0 or mask >= pow(2, self.num_dacs) - 1:
            self.log.error("DAC mask %s is out of range", mask)
            return -3

        # this should never happen, but just in case
        if self.num_dacs < 0 or self.num_dacs > 15:
            self.log.error("Invalid number of DACs: %s", self.num_dacs)
            return -3

        lsb = self.dac_mask_msb - self.num_dacs + 1
        wdw = _set_bits(wdw, lsb, self.num_dacs, mask)

        self.log.debug("set the DAC mask to %s", mask)

        return wdw

    def print_description(self, printer_func: callable) -> None:
        """Print a detailed description of the generator IP configuration parameters.

        :param printer_func: Function to use to print the description
        :type printer_func: callable
        """
        string = (
            f"sample_memory_address_width: {self.sample_memory_address_width}, address width of the envelope "
            "memory (word/IQSample aligned)"
        )
        string += (
            f"\nchannel_sample_memory_depth: {self.channel_sample_memory_depth}, depth of the envelope "
            "memory (words/IQSamples aligned)"
        )
        string += f"\nmaximum_duration: {self.maximum_duration}, maximum duration of a wave (samples)"
        string += (
            f"\nfractional_precision: {self.fractional_precision}, fractional precision of the interpolator (bits)"
        )
        string += f"\nsample_size: {self.sample_size}, width of samples (bits)"
        string += f"\nnumber_of_channels: {self.number_of_channels}, parallelism of the generator (samples/clock cycle)"
        string += f"\nphase_depth: {self.phase_depth}, width of phases (bits)"
        string += (
            f"\ntrigger_channels: {self.trigger_channels}, number of trigger channels for readout and drive (bits)"
        )
        string += f"\naxi_full_interface_depth: {self.axi_full_interface_depth}, axi full interface depth (bytes)"
        string += (
            f"\ntotal_sample_memory_segment_depth: {self.total_sample_memory_segment_depth}, envelope memory "
            "segment depth (bytes)"
        )
        string += f"\nwave_memory_segment_depth: {self.wave_memory_segment_depth}, wave memory segment depth (bytes)"
        string += (
            f"\nmemory_mapped_fifo_segment_depth: {self.memory_mapped_fifo_segment_depth}, memory mapped FIFO "
            "segment depth (bytes)"
        )
        string += f"\nseed_lfsr_width: {self.seed_lfsr_width}, width of lsfr seed and memory mapped FIFO entries (bits)"
        printer_func(string)
