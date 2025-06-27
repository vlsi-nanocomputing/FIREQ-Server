from pynq import DefaultIP
from pynq import MMIO
import numpy as np

__all__ = ['_FIREQDriver', '_DebugMMIO', '_GetBit', '_GetBits', '_SetBit', '_SetBits', '_ComputePincPoff']

class _FIREQDriver(DefaultIP):

    bindto = []

    def __init__(self, description):
        super().__init__(description=description)

    def WriteDescription(self):
        """
        Print the description of the IP
        """
        pass

    def InitAxiFullInterface(self, base_address : int, axi_depth : int):
        """
        Initialize the axi full interface for this IP
        
        :param base_address: Base address of the axi full interface
        :type base_address: int
        :param axi_depth: Depth of the axi interface, in bytes
        :type axi_depth: int
        """
        if self.AxiFullInterfaceMMIO is None:
            self.AxiFullInterfaceMMIO = MMIO(base_address, axi_depth)

    def InitAxiLiteInterface(self, base_address : int, axi_depth : int):
        """
        Initialize the axi lite interface for this IP
        
        :param base_address: Base address of the axi lite interface
        :type base_address: int
        :param axi_depth: Depth of the axi interface, in bytes
        :type axi_depth: int
        """
        if self.AxiLiteInterfaceMMIO is None:
            self.AxiLiteInterfaceMMIO = MMIO(base_address, axi_depth)

class _DebugMMIO():

    def __init__(self, replaces, debug_level, file):
        """
        Init the object
        
        :param replaces: mmio object that it replaces
        :type replaces: MMIO
        :param file: file handler, to write axi transactions
        :type file: FILE
        """
        self._file_handler = file
        self.replaces = replaces
        self.debug_level = debug_level
        self.memory = []
        # TODO: check this
        self.base_addr = self.replaces.start_address
    
    def read(self, address):

        if address%4 != 0:
            print("mmio error, read at address not word aligned")
            return 0
        if address not in self.memory.keys():
            return 0
        return self.memory[address]
    
    def write(self, address, data):

        if address%4 != 0:
            print("mmio error, read at address not word aligned")
            return 0
        with self._file_handler:
            if type(data) is int:
                self._file_handler.write("write address " + hex(address) + " write data " + hex(data) + " " + str(data) + "\n")
            elif type(data) is bytes:
                length = len(data)
                num_words = length >> 2
                if length % 4:
                    raise MemoryError("Unaligned write: data length must be multiple of 4.")
                buf = np.frombuffer(data, np.uint32, num_words, 0)
                for i in range(len(buf)):
                    self._file_handler.write("write address " + hex(address) + " write data " + hex(buf[i]) + " " + str(buf[i]) + "\n")
            else:
                raise ValueError("Data type must be int or bytes.")


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


def _DebugAxiInterface(self, address_offset : int, data):

