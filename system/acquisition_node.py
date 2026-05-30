from __future__ import annotations

import logging

import numpy as np
from _generic_node import _GenericNode

from ._utils import _get_dict_hash
from FIREQ_LL_API import AcquisitionDriver

from ._utils import _get_periods_from_clock

logger = logging.getLogger(__name__)


class AcquisitionNode(_GenericNode):
    """Object representing the acquisition IPs.

    Dict definition:
        _name: str, name of the trigger generator node/istance
        _clock_frequency: float, clock frequency in MHz
        _sampling_frequency: float, sampling frequency in MHz
        _ll_handler: AcquisitionDriver, handler to the low level driver
        $duration: float, duration of the acquisition
        $output_type: str, "raw"/"decimated"/"accumulated"
        $rfrequency: float, demodulation frequency in MHz
        $rphase: float, demodulation initial phase in radians
        $rchannel: int, trigger channel, set to 0 for no trigger
        $tof: float, time of flight in ns
    """

    # TODO: associate a number of fifos to the acquisition to account for a call stack like:
    #       1) param callback changes the single shot payload
    #       2) single shot payload triggers the change in max hw shots and also payload size
    #       1) hw_shots change
    #       2) payload size change triggers the experiment payload change

    nodetype = "acquisition"

    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _clock_frequency: float,
        _sampling_frequency: float,
        _ll_handler: AcquisitionDriver,
    ) -> None:
        super().__init__(name=name, parent=parent)
        self._clock_frequency = _clock_frequency
        self._sampling_frequency = _sampling_frequency
        self._ll_handler = _ll_handler
        self._fifo_sizes = {}
        # link payload to ll handler one
        # payload is either empty ({}) or has "size" and "on_inteface" keys
        self.payload = self._ll_handler.payload
        self._payload_hash = _get_dict_hash(self.payload)
        # register update functions
        self.root.register_update_function(self, self.update_payload)

    @_GenericNode.parameter_callback("$duration", sweepable=True, cost=1)
    def set_acquisition_duration(self, duration: float) -> int:
        """Set the acquisition duration.

        Also calls the "on_max_hw_shots_change" callback if the single shot payload size has changed.
        """
        clock_cycles = _get_periods_from_clock(duration, self._clock_frequency)
        return self._ll_handler.set_acquisition_duration(int(clock_cycles))

    @_GenericNode.parameter_callback("$output_type", sweepable=False, cost=1)
    def set_decimated_output_type(self, output_type: str) -> int:
        """Set the decimated output type.

        Also calls the "on_max_hw_shots_change" callback if the single shot payload size has changed.
        """
        return self._ll_handler.set_decimated_output_type(output_type)

    @_GenericNode.parameter_callback("$rfrequency", sweepable=True, cost=1)
    def set_demodulation_frequency(self, frequency: float) -> int:
        """Set the demodulation frequency."""
        normal_frequency = frequency / self._sampling_frequency
        return self._ll_handler.set_demodulation_frequency(normal_frequency)

    @_GenericNode.parameter_callback("$rphase", sweepable=True, cost=1)
    def set_demodulation_initial_phase(self, phase: float) -> int:
        """Set the demodulation initial phase."""
        normal_phase = phase / (2 * np.pi)
        return self._ll_handler.set_demodulation_initial_phase(normal_phase)

    @_GenericNode.parameter_callback("$rchannel", sweepable=False, cost=1)
    def set_trigger_channel(self, channel: int) -> int:
        """Set the trigger channel.

        Also calls the "on_max_hw_shots_change" callback if the acquisition becomes active.
        """
        return self._ll_handler.set_trigger_channel(channel)

    @_GenericNode.parameter_callback("$tof", sweepable=True, cost=1)
    def set_time_of_flight(self, time_of_flight: float) -> int:
        """Set the time of flight."""
        clock_cycles = _get_periods_from_clock(time_of_flight, self.clock_frequency)
        return self._ll_handler.set_time_of_flight(int(clock_cycles))

    def update_payload(self) -> bool:
        """Update the payload and returns a boolean to tell the caller if a change happened."""
        # get the hash of the payload and compare it to the last computed hash
        phash = _get_dict_hash(self.payload)
        if phash == self._payload_hash:
            return False
        # a change has been detected
        logger.debug("Payload changed for acquisition node %s", self.name)
        return True
