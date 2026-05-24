from __future__ import annotations

import logging

import numpy as np
from _generic_node import _GenericNode
from _utils import _get_periods_from_clock

logger = logging.getLogger(__name__)


class _GenericEnvelopeItem(_GenericNode):
    """Object representing a pulse envelope.

    Dict definition:
        _name: str, name of the envelope, _RECTANGULAR is a protected keyword
        _for_interpolation: true/false, uses hardware interpolation
        _is_symmetric: true/false, only specify in case of interpolation
        _i_even: true/false, only specify if symmetric
        _q_even: true/false
        $samples: [complex], list of complex values
    """

    nodetype = "envelope"

    def __init__(self, name: str, parent: SignalGeneratorNode = None, **kwargs: dict[str, Any]):
        super().__init__(name, parent, **kwargs)
        # validation of arguments
        if self._for_interpolation is None:
            logger.error("for_interpolation not specified")
            raise ValueError("for_interpolation not specified")
        if self._for_interpolation:
            if self._is_symmetric is None:
                logger.error("is_symmetric not specified")
                raise ValueError("is_symmetric not specified")
            if self._is_symmetric:
                if self._i_even is None or self._q_even is None:
                    logger.error("i_even and/or q_even not specified")
                    raise ValueError("i_even and/or q_even not specified")

    @_GenericNode.parameter_callback("$samples", sweepable=False, cost=1000)
    def write_samples(self, samples: np.array) -> int:
        """Write samples to envelope memory"""
        address = self.parent.reserve_envelope_segment(len(samples), self._for_interpolation)
        # FIXME
        self._natural_length = len(samples)
        return self.parent._ll_handler.write_envelope_memory(
            start_address=address, envelope=samples, common=self._for_interpolation
        )


class _Pulse(_GenericNode):
    """Object representing a pulse (wave definition word).

    Dictionary definition:
        - _name: str, name of pulse (gate)
        - _vz: bool, if set, the pulse is a virtual z gate
        - _readout: bool, if set, the pulse is a readout pulse
        - _envelope": str, envelope to use for the pulse
        - _gain: float, gain of pulse, between -1 and 1
        - _switch_iq: bool, if set, the IQ values are switched
        - _keep_last: bool, if set, the last samples will be placed at the output
        - $value: float, duration of the pulse in nanoseconds or rotation in radiants
    """

    nodetype = "pulse"

    def __init__(self, name, parent, _vz, _readout=False, _envelope=None, _gain=None, _switch_iq=None, _keep_last=None):
        super().__init__(name, parent)
        self._vz = _vz
        self._readout = _readout
        self._envelope = _envelope
        self._gain = _gain
        self._switch_iq = _switch_iq
        self._keep_last = _keep_last
        if not _vz:
            if _envelope is None:
                logger.error("envelope not specified")
                raise ValueError("envelope not specified")
            if _gain is None:
                logger.error("gain not specified")
                raise ValueError("gain not specified")
            if _switch_iq is None:
                logger.error("switch_iq not specified")
                raise ValueError("switch_iq not specified")
            if _keep_last is None:
                logger.error("keep_last not specified")
                raise ValueError("keep_last not specified")
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

    # TODO: change this system so that sweepable can be modified at run-time
    @_GenericNode.parameter_callback("$value", sweepable=True, cost=10)
    def write_pulse(self, value: int) -> int:
        """write the wdw to memory"""
        wdw = self._build_wdw(value)
        if self._readout:
            return self.parent.ll_handler.write_readout_wave(wdw)
        else:
            return self.parent._ll_handler.add_wave_in_wave_memory(wdw, self._address)

    def _build_wdw(self, value: float) -> int:
        """Build the wave definition word."""
        if self._vz:
            normal_phase = value/(2*np.pi)
            # FIXME
        else:
            


class _VZGateItem(_GenericPulseItem):
    """Object representing a pulse (wave definition word).

    TYPE 2: Virtual Z Gate (phase rotation)
        - "_name": str, name of the virtual z gate
        - "_readout": bool, if set, the pulse is a readout pulse
        - "$vz_rotation": float, phase of vz rotation
    """

    nodetype = "vzgate"

    def __init__(self, name, parent=None, **kwargs):
        super().__init__(name, parent, **kwargs)
        self._address = self.parent.reserve_wdw()

    @_GenericNode.parameter_callback("$vz_rotation", sweepable=True, cost=10)
    def write_pulse(self, duration: int) -> int:
        """write the wdw to memory"""


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
    """

    def __init__(self, name: str, parent: _GenericNode = None, **kwargs: dict[str, Any]) -> None:
        super().__init__(name, parent, **kwargs)
        # attribute validation
        if self._clock_frequency is None:
            logger.error("clock_frequency not specified")
            raise ValueError("clock_frequency not specified")
        if self._sampling_frequency is None:
            logger.error("sampling_frequency not specified")
            raise ValueError("sampling_requency not specified")
        if self._ll_handler is None:
            logger.error("ll_handler not specified")
            raise ValueError("ll_handler not specified")
        # envelope and wdw memory caching
        self._envelope_next_address = 0
        self._wdw_next_address = 0

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

    def create_child(
        self, name: str, of_type: str, **kwargs: dict[str, Any]
    ) -> _GenericEnvelopeItem | _PulseItem | _VZGateItem:
        """Create a child node of the specified type."""
        if of_type == "envelope":
            if name == "_RECTANGULAR":
                logger.error("envelope name %s is reserved", name)
                raise ValueError("envelope name is reserved")
            return _GenericEnvelopeItem(name=name, parent=self, **kwargs)
        elif of_type == "pulse":
            return _PulseItem(name=name, parent=self, **kwargs)
        elif of_type == "vzgate":
            return _VZGateItem(name=name, parent=self, **kwargs)
        else:
            logger.error("unsupported child type %s", of_type)
            raise ValueError("unsupported child type")
