"""Acquisition Node class for FIREQ system node representation."""

from __future__ import annotations

import logging

import numpy as np

from FIREQ_LL_API import AcquisitionDriver

from ._generic_node import _GenericNode
from ._utils import _get_dict_hash, _get_periods_from_clock

logger = logging.getLogger(__name__)


class AcquisitionNode(_GenericNode):
    """Object representing the acquisition IPs.

    Dict definition:
        _name: str, name of the trigger generator node/istance
        _ll_handler: AcquisitionDriver, handler to the low level driver
        $duration: float, duration of the acquisition in ns
        $output_type: str, "raw"/"decimated"/"accumulated"
        $rfrequency: float, demodulation frequency in MHz
        $rphase: float, demodulation initial phase in radians
        $rchannel: int, trigger channel, set to 0 for no trigger
        $tof: float, time of flight in ns
    """

    nodetype = "acquisition"
    wraps = [AcquisitionDriver.__name__]

    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _ll_handler: AcquisitionDriver,
    ) -> None:
        """Initialize the acquisition node.

        :param name: Name of the node
        :type name: str
        :param parent: Parent node
        :type parent: _GenericNode
        :param _ll_handler: Low level handler
        :type _ll_handler: AcquisitionDriver
        """
        super().__init__(name=name, parent=parent)
        self._ll_handler = _ll_handler
        # get clocking info from root node
        self._clock_frequency = self.root.get_fabric_frequency()
        self._sampling_frequency = self.root.get_acqisition_sampling_frequency()
        # get interface mapping, to translate payload interface to interface id
        self._interface_map = self.root.get_axi_stream_interface_map(self.name)
        # create payloads
        self.payload = {}
        # hash of the base payload
        self._base_payload_hash = _get_dict_hash(self._ll_handler.payload)
        # register payload update functions, one for each interface
        # TODO: fix ordering of payloads
        for output_if in set(self._ll_handler._output_interfaces.values()):
            if_id = self._interface_map[output_if]
            self.payload[output_if] = {}
            self.root.register_update_function(f"{if_id}/payload", self.update_payload)
            self.root.add_reference(f"{if_id}/payload", self.payload[if_id])

    @_GenericNode.parameter_callback("$duration", sweepable=True, cost=1)
    def set_acquisition_duration(self, duration: float) -> int:
        """Set the acquisition duration.

        :param duration: Duration in nanoseconds of the acquisition window
        :type duration: float
        :return: Error code (0 on success)
        :rtype: int
        """
        clock_cycles = _get_periods_from_clock(duration, self._clock_frequency)
        return self._ll_handler.set_acquisition_duration(int(clock_cycles))

    @_GenericNode.parameter_callback("$output_type", sweepable=False, cost=1)
    def set_decimated_output_type(self, output_type: str) -> int:
        """Set the decimated output type.

        :param output_type: Output type, can be "raw", "decimated" or "accumulated"
        :type output_type: str
        :return: Error code (0 on success)
        :rtype: int
        """
        return self._ll_handler.set_output_mode(output_type)

    @_GenericNode.parameter_callback("$rfrequency", sweepable=True, cost=1)
    def set_demodulation_frequency(self, frequency: float) -> int:
        """Set the demodulation frequency.

        :param frequency: Frequency in MHz
        :type frequency: float
        :return: Error code (0 on success)
        :rtype: int
        """
        normal_frequency = frequency / self._sampling_frequency
        return self._ll_handler.set_demodulation_frequency(normal_frequency)

    @_GenericNode.parameter_callback("$rphase", sweepable=True, cost=1)
    def set_demodulation_initial_phase(self, phase: float) -> int:
        """Set the demodulation initial phase.

        :param phase: Initial phase in radians
        :type phase: float
        :return: Error code (0 on success)
        :rtype: int
        """
        normal_phase = phase / (2 * np.pi)
        return self._ll_handler.set_demodulation_initial_phase(normal_phase)

    @_GenericNode.parameter_callback("$rchannel", sweepable=False, cost=1)
    def set_trigger_channel(self, channel: int) -> int:
        """Set the trigger channel.

        :param channel: Trigger channel number, set to 0 for no trigger
        :type channel: int
        :return: Error code (0 on success)
        :rtype: int
        """
        return self._ll_handler.set_trigger_channel(channel)

    @_GenericNode.parameter_callback("$tof", sweepable=True, cost=1)
    def set_time_of_flight(self, time_of_flight: float) -> int:
        """Set the time of flight.

        :param time_of_flight: Time of flight in nanoseconds
        :type time_of_flight: float
        :return: Error code (0 on success)
        :rtype: int
        """
        clock_cycles = _get_periods_from_clock(time_of_flight, self._clock_frequency)
        return self._ll_handler.set_time_of_flight(int(clock_cycles))

    def update_payload(self) -> bool:
        """Update the payload and returns a boolean to tell the caller if a change happened."""
        # get the hash of the payload and compare it to the last computed hash
        phash = _get_dict_hash(self._ll_handler.payload)
        if phash == self._payload_hash:
            return False
        # a change has been detected
        self._payload_hash = phash
        for output_if in self.payload.keys():
            if self._ll_handler.payload["on_interface"] == output_if:
                self.payload[output_if]["size"] = self._ll_handler.payload["size"]
            else:
                self.payload[output_if] = {}
        logger.debug("Payload changed for acquisition node %s", self.name)
        return True
