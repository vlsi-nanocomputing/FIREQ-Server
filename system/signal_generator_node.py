from __future__ import annotations

import logging

import numpy as np
from _generic_node import _GenericNode
from _utils import _get_periods_from_clock

from FIREQ_LL_API import GeneratorDriver

logger = logging.getLogger(__name__)


class _GenericEnvelope(_GenericNode):
    """Object representing a pulse envelope.

    Dict definition:
        _name: str, name of the envelope, _RECTANGULAR is a protected keyword
        _for_interpolation: bool (false), uses hardware interpolation
        _is_symmetric: bool (false), only specify in case of interpolation
        _i_even: bool, only specify if symmetric
        _q_even: bool, only specify if symmetric
        $samples: [complex], list of complex values
    """

    nodetype = "envelope"

    def __init__(
        self,
        name: str,
        parent: SignalGeneratorNode,
        _for_interpolation: bool = False,
        _is_symmetric: bool = False,
        _i_even: bool = None,
        _q_even: bool = None,
    ):
        super().__init__(name, parent)
        # validation of arguments
        self._for_interpolation = _for_interpolation
        self._is_symmetric = _is_symmetric
        self._i_even = _i_even
        self._q_even = _q_even
        # check inputs in case of interpolation
        if self._for_interpolation and self._is_symmetric:
            if self._i_even is None or self._q_even is None:
                logger.error("i_even and/or q_even not specified")
                raise ValueError("i_even and/or q_even not specified")
        self.natural_length = None
        # create the envelope part of the wdw
        # FIXME handle the rectanguar waves
        self.envelope_wdw = self.parent._ll_handler.build_envelope_specific_wdw(
            is_symmetric=self._envelope_ref._is_symmetric,
            i_even=self._envelope_ref._i_even,
            q_even=self._envelope_ref._q_even,
            forceone=False,
            interpolate=self._envelope_ref._for_interpolation,
        )

    @_GenericNode.parameter_callback("$samples", sweepable=False, cost=1000)
    def write_samples(self, samples: np.array) -> int:
        """Write the envelope samples to generator memory."""
        address = self.parent.reserve_envelope_segment(len(samples), self._for_interpolation)
        # FIXME
        self.natural_length = len(samples)
        return self.parent._ll_handler.write_envelope_memory(
            start_address=address, envelope=samples, common=self._for_interpolation
        )


class _RectangularEnvelope(_GenericEnvelope):
    """Object representing a rectangular envelope.

    Reserved for rectangular shaped pulses, only created once at init time.
    """

    def __init__(self, name: str, parent: SignalGeneratorNode):
        super().__init__(name, parent)
        self.natural_length = 1
        self.envelope_wdw = self.parent._ll_handler.build_envelope_specific_wdw(
            is_symmetric=False, i_even=False, q_even=False, forceone=True, interpolate=False
        )


class _VZGate(_GenericNode):
    """Object representing a pulse (wave definition word).

    Dictionary definition:
        - _name: str, name of pulse (gate)
        - _readout: bool (false), if set, the pulse is a readout pulse
        - $vz_rotation: float, phase of vz rotation normalized to 2pi
    """

    nodetype = "vzgate"

    def __init__(self, name: str, parent: SignalGeneratorNode, _readout: bool = False):
        super().__init__(name, parent)
        self._readout = _readout

    # TODO: change this system so that sweepable can be modified at run-time
    @_GenericNode.parameter_callback("$vz_rotation", sweepable=True, cost=10)
    def write_pulse(self, normalized_phase: float) -> int:
        """Write the wdw to memory with the specified phase."""
        wdw = self.parent._ll_handler.build_vz_wdw(normalized_phase)
        if self._readout:
            return self.parent._ll_handler.write_readout_wave(wdw)
        else:
            return self.parent._ll_handler.add_wave_in_wave_memory(wdw, self._address)


class _Pulse(_GenericNode):
    """Object representing a pulse (wave definition word).

    Dictionary definition:
        - _name: str, name of pulse (gate)
        - _readout: bool (false), if set, the pulse is a readout pulse
        - _envelope": str, envelope to use for the pulse
        - _switch_iq: bool (false), if set, the IQ values are switched
        - _keep_last: bool (false), if set, the last samples will be placed at the output
        - $duration: float, duration of the pulse in nanoseconds or rotation in radiants
        - $gain: float, between -1 and 1
    """

    nodetype = "pulse"

    def __init__(
        self,
        name: str,
        parent: SignalGeneratorNode,
        _readout: bool = False,
        _envelope: str = None,
        _switch_iq: bool = False,
        _keep_last: bool = False,
    ) -> None:
        super().__init__(name, parent)
        self._readout = _readout
        self._envelope = _envelope
        self._switch_iq = _switch_iq
        self._keep_last = _keep_last
        if _envelope is None:
            logger.error("envelope not specified")
            raise ValueError("envelope not specified")
        # check if envelope exists and get the reference to use it later
        if self._envelope not in [child.name for child in self.parent.children if child.nodetype == "envelope"]:
            logger.error("envelope %s not found", self._envelope)
            raise ValueError("envelope not found")
        self._envelope_ref = next(
            child for child in self.parent.children if child.name == self._envelope and child.nodetype == "envelope"
        )
        if not self._readout:
            # TODO: implement this func
            self._address = self.parent.reserve_wdw()
        else:
            self._address = None
        # starting values for duration and gain
        self._wanted_duration = self._envelope_ref.natural_length
        self._wanted_gain = 1.0

    # TODO: change this system so that sweepable can be modified at run-time
    @_GenericNode.parameter_callback("$duration", sweepable=True, cost=10)
    def set_duration(self, duration: float) -> int:
        self._wanted_duration = _get_periods_from_clock(duration, self.parent._sampling_frequency)
        return self._write_pulse()

    @_GenericNode.parameter_callback("$gain", sweepable=True, cost=10)
    def set_gain(self, value: float) -> int:
        self._wanted_gain = value
        return self._write_pulse()

    def _write_pulse(self) -> int:
        """Write the wdw to memory."""
        # build the wdw
        wdw = self.parent._ll_handler.build_pulse_wdw(
            envelope_wdw=self._envelope_ref.envelope_wdw,
            for_interpolation=self._envelope_ref._for_interpolation,
            start_address=self._envelope_ref._address,
            duration=self._wanted_duration,
            natural_duration=self._envelope_ref.natural_length,
            normalized_gain=self._wanted_gain,
            switch_iq=self._switch_iq,
            keep_last=self._keep_last,
        )
        if self._readout:
            return self.parent._ll_handler.write_readout_wave(wdw)
        else:
            return self.parent._ll_handler.add_wave_in_wave_memory(wdw, self._address)


class SignalGeneratorNode(_GenericNode):
    # TODO: actually handle dependencies, handle the sampling frequencies and so on
    """Object representing the signal generator system.

    Dict definition:
        _name: str, name of the trigger generator node/istance
        _clock_frequency: float, clock frequency of the trigger generator in MHz
        _sampling_frequency: float, sampling frequency of the signal generator in MHz
        _ll_handler: TriggerGeneratorDriver, low level handler for the trigger generator
        $dfrequency: float, drive frequency in MHz
        $rfrequency: float, readout frequency in MHz
        $rphase: float, readout phase in radiants
        $rchannel: int, readout trigger channel, set to 0 to deactivate
        $dchannel: int, drive trigger channel, set to 0 to deactivate
        $lfsr_seed: int, seed for the lfsr
        $drive_order: list, ordered list of pulses to be generated
    """

    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _clock_frequency: float,
        _sampling_frequency: float,
        _ll_handler: GeneratorDriver,
    ) -> None:
        super().__init__(name, parent)
        self._clock_frequency = _clock_frequency
        self._sampling_frequency = _sampling_frequency
        self._ll_handler = _ll_handler
        # envelope and wdw memory caching
        self.init_memory()
        # create the rectangular envelope
        _RectangularEnvelope(name="_RECTANGULAR", parent=self)

    def init_memory(self) -> int:
        """Initialize the memory of the signal generator."""
        self._envelope_next_address = 0
        self._wdw_next_address = 0
        # FIXME
        return self._ll_handler.clear_envelope_memory()

    def reserve_envelope_segment(self, length: int, for_interpolation: float) -> int:
        """Reserve a segment of the envelope memory."""
        address = self._envelope_next_address
        div = 1 if for_interpolation else self._ll_handler.number_of_channels
        self._envelope_next_address += length // div
        return address

    @_GenericNode.parameter_callback("$dfrequency", sweepable=True, cost=1)
    def set_drive_frequency(self, frequency: float) -> int:
        """Set the drive frequency."""
        return self._ll_handler.set_drive_modulation_frequency(frequency / self._sampling_frequency)

    @_GenericNode.parameter_callback("$rfrequency", sweepable=True, cost=1)
    def set_readout_frequency(self, frequency: float) -> int:
        """Set the readout frequency."""
        return self._ll_handler.set_readout_modulation_frequency(frequency / self._sampling_frequency)

    @_GenericNode.parameter_callback("$rphase", sweepable=True, cost=1)
    def set_readout_phase(self, phase: float) -> int:
        """Set the readout phase."""
        return self._ll_handler.set_readout_modulation_initial_phase(phase / (2 * np.pi))

    @_GenericNode.parameter_callback("$rchannel", sweepable=False, cost=1)
    def set_readout_channel(self, channel: int) -> int:
        """Set the readout channel."""
        return self._ll_handler.set_trigger_channel(channel, "readout")

    @_GenericNode.parameter_callback("$dchannel", sweepable=False, cost=1)
    def set_drive_channel(self, channel: int) -> int:
        """Set the drive channel."""
        return self._ll_handler.set_trigger_channel(channel, "drive")

    @_GenericNode.parameter_callback("$lfsr_seed", sweepable=True, cost=1)
    def set_lfsr_seed(self, seed: int) -> int:
        """Set the lfsr seed."""
        return self._ll_handler.set_lfsr_seed(seed)

    @_GenericNode.parameter_callback("$drive_order", sweepable=False, cost=1)
    def set_drive_order(self, order: list[str]) -> int:
        """Set the order of drive generation."""
        # for each element in the list, search the wdw for them and create another list of addresses
        addresses = []
        for pulse_name in order:
            pulse = next(
                (
                    child
                    for child in self.children
                    if child.name == pulse_name and child.nodetype in ("pulse", "vzgate")
                ),
                None,
            )
            if pulse is None:
                logger.error("pulse %s not found in children", pulse_name)
                raise ValueError(f"pulse {pulse_name} not found")
            if pulse._readout:
                logger.error("pulse %s is a readout pulse and cannot be placed in the drive order", pulse_name)
                raise ValueError(f"pulse {pulse_name} is a readout pulse and cannot be placed in the drive order")
            addresses.append(pulse._address)
        # write the addresses to the memory mapped fifo
        for order_index, wdw_index in enumerate(addresses):
            ret = self._ll_handler.add_wave_to_drive_wave_sequence(order_index, wdw_index)
            if ret != 0:
                logger.error("failed to write drive order at index %s", order_index)
                return ret
        return 0

    def create_child(self, name: str, of_type: str, **kwargs: dict[str, Any]) -> _GenericEnvelope | _Pulse | _VZGate:
        """Create a child node of the specified type."""
        # check that the name is not already taken by an existing child
        if any(child.name == name for child in self.children):
            logger.error("child with name %s already exists", name)
            raise ValueError(f"child with name {name} already exists")
        if of_type == "envelope":
            return _GenericEnvelope(name=name, parent=self, **kwargs)
        elif of_type == "pulse":
            return _Pulse(name=name, parent=self, **kwargs)
        elif of_type == "vzgate":
            return _VZGate(name=name, parent=self, **kwargs)
        else:
            logger.error("unsupported child type %s", of_type)
            raise ValueError("unsupported child type")
