from pynq import MMIO
import numpy as np
from ._Utils import *

__all__ = ['TriggerGeneratorDriver']

class TriggerGeneratorDriver(_FIREQDriver):

    bindto = ['user.org:user:axisTriggerGeneratorIP:1.0']

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

    def write_description(self):
        print("trigger channels: " + str(self.TriggerChannels))
        print("fifo interface axi depth: " + str(self.FifoInterfaceMemoryDepth))
        print("fifo channel depth: " + str(self.ChannelFifoDepth))
        print("maximum number of hardware repetitions: " + str(self.MaxHWRepetitions))
    
    def init_axi_full_interface(self, base_address : int, axi_depth : int):
        super().init_axi_full_interface(base_address, axi_depth)

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
            full_mmio = self.AxiFullInterfaceMMIO.replaces
            del self.AxiLiteInterfaceMMIO
            self.AxiLiteInterfaceMMIO = lite_mmio
            del self.AxiFullInterfaceMMIO
            self.AxiFullInterfaceMMIO = full_mmio
        elif level == 1:
            self.AxiFullInterfaceMMIO = _DebugMMIO(self.AxiFullInterfaceMMIO, 1, file_handler)
            self.AxiLiteInterfaceMMIO = _DebugMMIO(self.AxiLiteInterfaceMMIO, 1, file_handler)
        else:
            return 0
        
        self.DebugLevel = level
        return 0
    
    def set_experiment_duration(self,duration):
        """
        Set the experiment duration for a single shot
        
        :param duration: Duration in clock cycles
        :type duration: uint
        """

        # write inc LOW
        self.AxiLiteInterfaceMMIO.write(self.experiment_dur_l*4, duration & 0xFFFFFFFF)
        # write inc HIGH
        self.AxiLiteInterfaceMMIO.write(self.experiment_dur_h*4, duration >> 32)

    def set_number_of_shots(self, value):
        """
        Set the number of shots to execute in hardware
        
        :param value: number of shots
        :type value: uint
        """
        if(value < 1 or value > self.MaxHWRepetitions):
            print("error: the numer of shots " + str(value) + " is outside of range 1 to " + str(self.MaxHWRepetitions))
            return

        self.AxiLiteInterfaceMMIO.write(self.shots_num_l*4,int(value-1))

    def start_experiment(self):
        """
        Start the generation of triggers
        """
        self.AxiLiteInterfaceMMIO.write(0,1 << self.ManTrigPos)

    def is_done(self):
        """
        Check if the experiment is finished
        
        :return: 1 if the experiment is finished, 0 if still running
        :rtype: Literal[1, 0]
        """
        cntr = self.AxiLiteInterfaceMMIO.read(0)
        if ((cntr&0x40000000) == 0x40000000):
            return 1
        else:
            return 0

    def insert_drive_delay(self, channel, index, delay, generate_trigger):
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
        self.AxiFullInterfaceMMIO.write(real_address*4, int(real_delay))
        return 0
    
    def set_readout_delay(self,delay : int,channel : int):
        """
        Set the experiment duration for a single shot
        
        :param duration: Duration in clock cycles
        :type duration: uint
        """
        if channel < 1 or channel > self.TriggerChannels:
            print("error, channel selection out of range")
            return -3
        # write inc LOW
        self.AxiLiteInterfaceMMIO.write((self.readout_delay_l + (channel-1)*2)*4, delay & 0xFFFFFFFF)
        # write inc HIGH
        self.AxiLiteInterfaceMMIO.write((self.readout_delay_h + (channel-1)*2)*4, delay >> 32)