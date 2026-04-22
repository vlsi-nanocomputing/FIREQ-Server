from typing import Any, TextIO

import numpy as np
from pynq import MMIO, DefaultIP

__all__ = [
    "_FIREQDriver",
    "_DebugMMIO",
    "_get_bit",
    "_get_bits",
    "_set_bit",
    "_set_bits",
    "_compute_pinc_poff",
]


class _FIREQDriver(DefaultIP):
    """Base driver class for FIREQ IP drivers.

    This class provides methods to initialize the AXI Lite and Full interfaces of an IP.
    """

    bindto = []

    def __init__(self, description: dict[str, Any]) -> None:
        """Initialize the FIREQ driver.

        :param description: Dictionary containing IP parameters and configuration
        :type description: dict
        """
        super().__init__(description=description)
        self._axi_full_interface_mmio = None
        self._axi_lite_interface_mmio = None
        self._debug_level = 0

    def print_description(self) -> None:
        """Print the description of the IP.

        This method should be overridden by subclasses.
        """
        pass

    def init_axi_full_interface(self, base_address: int, axi_depth: int) -> None:
        """Initialize the AXI Full interface for this IP.

        :param base_address: Base address of the AXI Full interface
        :type base_address: int
        :param axi_depth: Depth of the AXI interface, in bytes
        :type axi_depth: int
        """
        if self._axi_full_interface_mmio is None:
            self._axi_full_interface_mmio = MMIO(base_address, axi_depth)

    def init_axi_lite_interface(self, base_address: int, axi_depth: int) -> None:
        """Initialize the AXI Lite interface for this IP.

        :param base_address: Base address of the AXI Lite interface
        :type base_address: int
        :param axi_depth: Depth of the AXI interface, in bytes
        :type axi_depth: int
        """
        if self._axi_lite_interface_mmio is None:
            self._axi_lite_interface_mmio = MMIO(base_address, axi_depth)

    def set_debug_level(
        self,
        level: int,
        axi_lite_file_handler: TextIO,
        axi_full_file_handler: TextIO | None,
    ) -> int:
        """Set the level of debugging on the AXI interfaces.

        :param level: 0 for no debugging, 1 for file logging
        :type level: int
        :param axi_lite_file_handler: File where to log the AXI Lite transactions
        :type axi_lite_file_handler: TextIO
        :param axi_full_file_handler: File where to log the AXI Full transactions
        :type axi_full_file_handler: TextIO | None
        :return: Error code (0 on success)
        :rtype: int
        """
        if level == self._debug_level:
            return 0

        if level == 0:
            # no debug
            lite_mmio = self._axi_lite_interface_mmio.replaces
            full_mmio = self._axi_full_interface_mmio.replaces
            del self._axi_lite_interface_mmio
            del self._axi_full_interface_mmio
            self._axi_lite_interface_mmio = lite_mmio
            self._axi_full_interface_mmio = full_mmio
        elif level == 1:
            self._axi_lite_interface_mmio = _DebugMMIO(self._axi_lite_interface_mmio, 1, axi_lite_file_handler)
            self._axi_full_interface_mmio = _DebugMMIO(self._axi_full_interface_mmio, 1, axi_full_file_handler)
        else:
            return 0

        self._debug_level = level
        return 0


class _DebugMMIO:
    """MMIO-like class used for debug purposes.

    The intended use is to (completely or partially) replace an MMIO handler to log AXI
    transactions to a file.
    """

    def __init__(self, replaces: MMIO, debug_level: int, file: TextIO) -> None:
        """Initialize the DebugMMIO wrapper.

        :param replaces: MMIO object that it replaces
        :type replaces: MMIO
        :param debug_level: Debug level for logging
        :type debug_level: int
        :param file: File handler to write AXI transactions
        :type file: TextIO
        """
        self._file_handler = file
        self._replaces = replaces
        self._debug_level = debug_level
        self._memory = {}
        self.base_addr = self._replaces.base_addr

    @property
    def replaces(self) -> MMIO:
        """Return the original MMIO object that this wrapper replaces.

        :return: The original MMIO object
        :rtype: MMIO
        """
        return self._replaces

    def read(self, address: int) -> int:
        """Read a 32-bit unsigned value at a certain address.

        :param address: Byte aligned address
        :type address: int
        :return: Value read from the address, or 0 if not found or misaligned
        :rtype: int
        """
        if address % 4 != 0:
            raise ValueError("MMIO error: read address is not word aligned")
        if address not in self._memory:
            return 0
        return self._memory[address]

    def write(self, address: int, data: int | bytes) -> None:
        """Write a 32-bit unsigned value (data) at a certain address.

        :param address: Byte aligned unsigned address
        :type address: int
        :param data: 32-bit data or bytes
        :type data: int | bytes
        :raises ValueError: If address is not word aligned
        :raises MemoryError: If data bytes length is not multiple of 4
        :raises ValueError: If data type is not int or bytes
        """
        if address % 4 != 0:
            raise ValueError("MMIO error: write address is not word aligned")

        with self._file_handler:
            if isinstance(data, int):
                self._file_handler.write(f"write address {hex(address)} write data {hex(data)} {data}\n")
            elif isinstance(data, bytes):
                length = len(data)
                num_words = length >> 2
                if length % 4:
                    raise MemoryError("Unaligned write: data length must be multiple of 4.")
                buf = np.frombuffer(data, np.uint32, num_words, 0)
                for i in range(len(buf)):
                    self._file_handler.write(f"write address {hex(address)} write data {hex(buf[i])} {buf[i]}\n")
            else:
                raise ValueError("Data type must be int or bytes.")


def _set_bit(value: int, pos: int, setvalue: int) -> int:
    """Set the bit at index pos of value to setvalue.

    :param value: Input value to be manipulated
    :type value: int
    :param pos: Bit position, 0 is LSB
    :type pos: int
    :param setvalue: Value to set the bit, 0 or 1
    :type setvalue: int
    :return: Modified value with the bit set
    :rtype: int
    """
    bitvalue = 1 if setvalue else 0
    return (value & ~(1 << pos)) | (bitvalue << pos)


def _get_bit(value: int, pos: int) -> int:
    """Get the bit at position pos of argument value.

    :param value: Input value to extract bit from
    :type value: int
    :param pos: Bit position
    :type pos: int
    :return: 0 or 1 depending on the bit value
    :rtype: int
    """
    return (value & (1 << pos)) >> pos


def _set_bits(value: int, start: int, length: int, setvalue: int) -> int:
    """Set bits from start for a length equal to length to setvalue.

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
    return (value & ~mask) | safe_setvalue


def _get_bits(value: int, start: int, length: int) -> int:
    """Get a number of sequential bits from argument value.

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


def _compute_pinc_poff(frequency: float, phase: float, samplerate: float, phase_depth: int) -> tuple[int, int]:
    """Compute the phase increment and phase offset.

    Frequency units must be the same for both the frequency and samplerate inputs, but the actual unit is not important.

    :param frequency: Wanted frequency of the DDS
    :type frequency: float
    :param phase: Phase in radians
    :type phase: float
    :param samplerate: Sampling rate of the DDS
    :type samplerate: float
    :param phase_depth: Depth of the phase registers
    :type phase_depth: int
    :return: Phase increment and phase offset tuple
    :rtype: tuple[int, int]
    """
    # get the bounded phase, mapping an unbounded radian into (-2pi:2pi)
    bounded_phase = phase % (2 * np.pi)
    # add 2pi to move the bounds to (0:4pi)
    bounded_phase = bounded_phase + 2 * np.pi
    # now bound the phase to [0:2pi)
    bounded_phase = bounded_phase % (2 * np.pi)
    # get the nyquist zone
    nyquist_zone = frequency // (samplerate / 2)
    # get the remainder, e.g. the distance from the nyquist frequency
    nyquist_remainder = frequency % (samplerate / 2)
    # compute the phase increment and offset

    pinc = 0
    poff = 0
    if nyquist_zone % 2 == 0:
        # checking that we are in the odd nyquist zones, note that nyquist_zone
        # differs from the real nyquist zone by 1 so the odd/even checks on the
        # real nyquist zone are opposite.
        # In this case we will be here for zones 1,3,5,... which map into
        # 0,2,4 for the nyquist_zone variable
        pinc = (nyquist_remainder * (2**phase_depth)) / samplerate
        poff = (2**phase_depth - 1) * (bounded_phase / (2 * np.pi))
    else:
        # when the nyquist zone is even, the phase has opposite sign and
        # the frequency needs to be calculated as the distance from the sample rate
        nyquist_remainder = samplerate / 2 - nyquist_remainder
        bounded_phase = 2 * np.pi - bounded_phase
        pinc = (nyquist_remainder * (2**phase_depth)) / samplerate
        poff = (2**phase_depth - 1) * (bounded_phase / (2 * np.pi))

    return (round(pinc), round(poff))
