"""Acquisition Node class for FIREQ system node representation."""

from __future__ import annotations

import logging

import numpy as np

from FIREQ_LL_API import AcquisitionDriver

from ._generic_node import _GenericNode
from ._utils import _get_periods_from_clock, _MutableRef

logger = logging.getLogger(__name__)


class AcquisitionNode(_GenericNode):
    """Object representing the acquisition IP.

    Configuration dictionary keys:

    - ``_name`` (str): Name of the acquisition node instance.
    - ``_ll_handler`` (AcquisitionDriver): Low-level driver handler.
    - ``$duration`` (float): Duration of the acquisition, in ns.
    - ``$output_type`` (str): Output type. One of ``"raw"``, ``"decimated"``, or ``"accumulated"``.
    - ``$rfrequency`` (float): Demodulation frequency, in MHz.
    - ``$rphase`` (float): Demodulation initial phase, in radians.
    - ``$rchannel`` (int): Trigger channel. Set to ``0`` for no external trigger.
    - ``$tof`` (float): Time of flight, in ns.
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
        :param parent: Parent node in the system tree
        :type parent: _GenericNode
        :param _ll_handler: Low-level acquisition driver
        :type _ll_handler: AcquisitionDriver
        """
        super().__init__(name=name, parent=parent)
        self._ll_handler = _ll_handler
        # get clocking info from root node
        self._clock_frequency = self.root.get_fabric_frequency()
        self._sampling_frequency = self.root.get_acqisition_sampling_frequency()
        # get interface mapping, to translate payload interface to interface id
        self._interface_map = self.root.get_axi_stream_interface_map(self.name)
        # create payloads dictionary
        self.payload: dict[str, _MutableRef] = {}
        # register payload update functions, one for each output interface
        for output_if in set(self._ll_handler._output_interfaces.values()):
            if_id = self._interface_map[output_if]
            self.payload[output_if] = _MutableRef()
            self.root.register_update_function(f"{if_id}/payload", self._make_payload_updater(output_if))
            self.root.add_reference(f"{if_id}/payload", self.payload[output_if])

    def _reset_all(self) -> None:
        """Call the reset all function to all children."""
        # reset the internal state of the driver
        self._ll_handler.reset_state()
        for _, payload in self.payload.items():
            payload.reset_hash()

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
    def set_output_mode(self, output_type: str) -> int:
        """Set the output type of the acquisition.

        :param output_type: Output type, can be ``"raw"``, ``"decimated"`` or ``"accumulated"``
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

        :param channel: Trigger channel number, set to 0 for no external trigger
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

    def _make_payload_updater(self, output_if: str) -> callable:
        """Create a closure that updates the payload for a specific interface.

        :param output_if: The output interface to bind the updater to
        :type output_if: str
        :return: A callable that updates only the bound interface's payload
        :rtype: callable
        """

        def updater() -> bool:
            return self._update_payload_for_interface(output_if)

        return updater

    def _update_payload_for_interface(self, output_if: str) -> bool:
        """Update the payload for a single interface and notify whether a change happened.

        :param output_if: The output interface to update
        :type output_if: str
        :return: ``True`` if the payload has changed since the last call, ``False`` otherwise
        :rtype: bool
        """
        if self._ll_handler.payload and self._ll_handler.payload["on_interface"] == output_if:
            self.payload[output_if]["size"] = self._ll_handler.payload["size"]
            self.payload[output_if]["source"] = self.name
            self.payload[output_if]["format"] = self._ll_handler.payload["format"]
        else:
            self.payload[output_if].clear()
        return self.payload[output_if].hash_and_compare()
