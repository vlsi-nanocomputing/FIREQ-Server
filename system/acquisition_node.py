from __future__ import annotations

import logging

import numpy as np
from _generic_node import _GenericNode
from _utils import _get_periods_from_clock, _safe_float_cast, _safe_integer_cast

from ..FIREQ_LL_API import AcquisitionDriver

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

    # TODO: actually handle dependencies, handle the sampling frequencies and so on

    nodetype = "acquisition"

    def __init__(self, name: str, parent: _GenericNode = None, **kwargs: dict[str, Any]) -> None:
        super().__init__(name=name, parent=parent, **kwargs)
        # verification of init parameters
        if self._clock_frequency is None:
            logger.error("clock_frequency not specified")
            raise ValueError("clock_frequency not specified")
        if kwargs._sampling_frequency is None:
            logger.error("sampling_frequency not specified")
            raise ValueError("sampling_frequency not specified")
        if kwargs._ll_handler is None:
            logger.error("ll_handler not specified")
            raise ValueError("ll_handler not specified")

    @_GenericNode.propagate_dependency("$duration")  # to know length for each shot
    @_GenericNode.parameter_callback("$duration", sweepable=True, cost=1)
    def set_acquisition_duration(self, duration: float) -> int:
        """Set the acquisition duration."""
        clock_cycles = _get_periods_from_clock(duration, self._clock_frequency)
        return self._ll_handler.set_acquisition_duration(int(clock_cycles))

    @_GenericNode.propagate_dependency("$output_type")  # to know length for each shot
    @_GenericNode.parameter_callback("$output_type", sweepable=False, cost=1)
    def set_decimated_output_type(self, output_type: str) -> int:
        """Set the decimated output type."""
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

    @_GenericNode.propagate_dependency("$rchannel")  # to know if active
    @_GenericNode.parameter_callback("$rchannel", sweepable=False, cost=1)
    def set_trigger_channel(self, channel: int) -> int:
        """Set the trigger channel."""
        return self._ll_handler.set_trigger_channel(channel)

    @_GenericNode.parameter_callback("$tof", sweepable=True, cost=1)
    def set_time_of_flight(self, time_of_flight: float) -> int:
        """Set the time of flight."""
        clock_cycles = _get_periods_from_clock(time_of_flight, self.clock_frequency)
        return self._ll_handler.set_time_of_flight(int(clock_cycles))
