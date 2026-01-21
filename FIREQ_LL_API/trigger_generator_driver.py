from pynq import MMIO
import numpy as  np
from ._utils import *

__all__ = ['TriggerGeneratorDriver']

class TriggerGeneratorDriver(_FIREQDriver):
    """
    Driver class for the trigger generator IP.
    Provides methods to set the generation time of pulses and acquisition events.
    """

    bindto = ['user.org:user:axisTriggerGeneratorIP:1.0']

    def __init__(self, description):
        """
        Sets class attributes that depend on IP parametrization.
        
        :param description: Dictionary that is passed by PYNQ when initializing the IP.
        """
        super().__init__(description= description)
        # parse the number of channels of the trigger generator
        self.trigger_channels = int(description['parameters']['TriggerWordWidth'])
        # parse the fifo interface depth and create mmio handle
        self.fifo_interface_memory_depth = pow(2,int(description['parameters']['C_S00_AXI_ADDR_WIDTH']))
        # fifo depth in number of words 
        self.channel_fifo_depth = pow(2,int(description['parameters']['FifoAddressWidth']))
        # fifo output width
        self.fifo_output_width = int(description['parameters']['FifoOutputWidth'])
        # maximum drive delay
        self.drive_delay_max = pow(2,self.fifo_output_width-1)
        # experiment max
        self.experiment_timer_max = pow(2,int(description['parameters']['ExperimentTimerWidth']))
        # parse the size of the repitition counter
        self.max_hw_repetitions = pow(2,int(description['parameters']['RepetitionWidth']))

        self.ctrl = 0
        self.experiment_dur_l = 2
        self.experiment_dur_h = 3
        self.readout_delay_l = 4
        self.readout_delay_h = 5
        self.shots_num_l = 1

        # Bit position definition
        self.manual_trigger_pos = 31

    def print_description(self):
        print("trigger_channels: " + str(self.trigger_channels))
        print("fifo_interface_axi_depth: " + str(self.fifo_interface_memory_depth))
        print("fifo_channel_depth: " + str(self.channel_fifo_depth))
        print("maximum_number_of_hardware_repetitions: " + str(self.max_hw_repetitions))
    
    def init_axi_full_interface(self, base_address : int, axi_depth : int):
        super().init_axi_full_interface(base_address, axi_depth)

    def init_axi_lite_interface(self, base_address : int, axi_depth : int):
        super().init_axi_lite_interface(base_address, axi_depth)
        # delete the mmio object created by PYNQ
        del self.mmio
    
    def set_experiment_duration(self,duration):
        """
        Set the experiment duration for a single shot
        
        :param duration: Duration in clock cycles
        :type duration: uint
        """

        # write inc LOW
        self.axi_lite_interface_mmio.write(self.experiment_dur_l*4, duration & 0xFFFFFFFF)
        # write inc HIGH
        self.axi_lite_interface_mmio.write(self.experiment_dur_h*4, duration >> 32)

    def set_number_of_shots(self, value):
        """
        Set the number of shots to execute in hardware
        
        :param value: number of shots
        :type value: uint
        """
        if(value < 1 or value > self.max_hw_repetitions):
            print("error: the numer of shots " + str(value) + " is outside of range 1 to " + str(self.max_hw_repetitions))
            return

        self.axi_lite_interface_mmio.write(self.shots_num_l*4,int(value - 1))

    def start_experiment(self):
        """
        Start the generation of triggers
        """
        self.axi_lite_interface_mmio.write(0,1 << self.manual_trigger_pos)

    def is_done(self):
        """
        Check if the experiment is finished

        :return: 1 if the experiment is finished, 0 if still running
        :rtype: Literal[1, 0]
        """
        control_register = self.axi_lite_interface_mmio.read(0)
        if ((control_register & 0x40000000) == 0x40000000):
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
        if (channel< 1 or channel > self.trigger_channels):
            print("error, channel " + str(channel) + " is outside of range 1 to " + str(self.trigger_channels))
            return -3

        if (index < 1 or index > self.channel_fifo_depth):
            print("error, the index is outside of range")
            return -3

        if (delay < 1 or delay > self.drive_delay_max):
            print("error, the delay is outside of range")
            return -3
        
        real_delay = (delay - 1) | (generate_trigger << 31)
        real_address = (channel - 1)*self.channel_fifo_depth + index - 1
        self.axi_full_interface_mmio.write(real_address*4, int(real_delay))
        return 0
    
    def set_readout_delay(self,delay : int,channel : int):
        """
        Set the experiment duration for a single shot
        
        :param duration: Duration in clock cycles
        :type duration: uint
        """
        if channel < 1 or channel > self.trigger_channels:
            print("error, channel selection out of range")
            return -3
        # write inc LOW
        self.axi_lite_interface_mmio.write((self.readout_delay_l + (channel - 1)*2)*4, delay & 0xFFFFFFFF)
        # write inc HIGH
        self.axi_lite_interface_mmio.write((self.readout_delay_h + (channel - 1)*2)*4, delay >> 32)