"""Signal Generator Node class for FIREQ system node representation."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from FIREQ_LL_API import GeneratorDriver

from ._generic_node import _GenericNode
from ._utils import _get_periods_from_clock

logger = logging.getLogger(__name__)


class _GenericEnvelope(_GenericNode):
    """Object representing a pulse envelope.

    Configuration dictionary keys:

    - ``_name`` (str): Name of the envelope (``_RECTANGULAR`` is a protected keyword).
    - ``_for_interpolation`` (bool): Uses hardware interpolation. Defaults to ``False``.
    - ``_is_symmetric`` (bool): Only specify in case of interpolation. Defaults to ``False``.
    - ``_i_even`` (bool): Only specify if symmetric.
    - ``_q_even`` (bool): Only specify if symmetric.
    - ``$samples`` (list[complex]): List of complex sample values.
    """

    nodetype = "envelope"

    def __init__(
        self,
        name: str,
        parent: SignalGeneratorNode,
        _for_interpolation: bool = False,
        _is_symmetric: bool = False,
        _i_even: bool | None = None,
        _q_even: bool | None = None,
    ) -> None:
        """Initialize the envelope item.

        :param name: Name of the envelope
        :type name: str
        :param parent: Parent signal generator node
        :type parent: SignalGeneratorNode
        :param _for_interpolation: If the envelope is for interpolation, defaults to ``False``
        :type _for_interpolation: bool
        :param _is_symmetric: If the envelope is symmetric, defaults to ``False``;
            ignored if interpolation is disabled
        :type _is_symmetric: bool
        :param _i_even: If the envelope is even for I, defaults to ``None``
        :type _i_even: bool or None
        :param _q_even: If the envelope is even for Q, defaults to ``None``
        :type _q_even: bool or None
        :raises ValueError: If ``_i_even`` and/or ``_q_even`` are not specified
            when both interpolation and symmetry are enabled
        """
        super().__init__(name, parent)
        # validation of arguments
        self._for_interpolation = _for_interpolation
        self._is_symmetric = _is_symmetric
        self._i_even = _i_even
        self._q_even = _q_even
        # check inputs in case of interpolation
        if self._for_interpolation and self._is_symmetric:
            if self._i_even is None or self._q_even is None:
                self.log.error("i_even and/or q_even not specified")
                raise ValueError("i_even and/or q_even not specified")
        self.natural_length: int | None = None
        self._address: int = 0
        # create the envelope part of the wdw
        self.envelope_wdw = self.parent._ll_handler.build_envelope_specific_wdw(
            is_symmetric=self._is_symmetric,
            i_even=self._i_even if self._i_even is not None else False,
            q_even=self._q_even if self._q_even is not None else False,
            forceone=False,
            for_interpolation=self._for_interpolation,
        )

    @_GenericNode.parameter_callback("$samples", sweepable=False, cost=1000)
    def write_samples(self, samples: np.ndarray) -> int:
        """Write the envelope samples to generator memory.

        :param samples: Array of complex samples of the envelope, normalized to 1
            (values between -1 and 1)
        :type samples: np.ndarray
        :return: Error code (0 on success)
        :rtype: int
        """
        div = 1 if self._for_interpolation else self.parent._ll_handler.number_of_channels
        memory_depth = len(samples) // div
        self._address = self.parent.reserve_envelope_segment(memory_depth)
        self.natural_length = len(samples)
        return self.parent._ll_handler.write_envelope_memory(
            start_address=self._address, envelope=samples, common=self._for_interpolation
        )


class _RectangularEnvelope(_GenericNode):
    """Object representing a rectangular envelope.

    Reserved for rectangular shaped pulses; only created once at initialization time
    with the reserved name ``_RECTANGULAR``.
    """

    nodetype = "envelope"

    def __init__(self, name: str, parent: SignalGeneratorNode) -> None:
        """Initialize the rectangular envelope.

        :param name: Name of the envelope (should be ``_RECTANGULAR``)
        :type name: str
        :param parent: Parent signal generator node
        :type parent: SignalGeneratorNode
        """
        super().__init__(name, parent)
        self.natural_length = 1
        self._address = 0
        self.envelope_wdw = self.parent._ll_handler.build_envelope_specific_wdw(
            is_symmetric=False, i_even=False, q_even=False, forceone=True, for_interpolation=False
        )


class _VZGate(_GenericNode):
    """Object representing a virtual Z rotation gate.

    Configuration dictionary keys:

    - ``_name`` (str): Name of the gate.
    - ``_readout`` (bool): If set, the pulse is a readout pulse. Defaults to ``False``.
    - ``$vz_rotation`` (float): Phase of the VZ rotation, normalized to 2π.
    """

    nodetype = "vzgate"

    def __init__(self, name: str, parent: SignalGeneratorNode, _readout: bool = False) -> None:
        """Initialize the virtual Z gate.

        :param name: Name of the gate
        :type name: str
        :param parent: Parent signal generator node
        :type parent: SignalGeneratorNode
        :param _readout: Whether this is a readout pulse, defaults to ``False``
        :type _readout: bool
        """
        super().__init__(name, parent)
        self._readout = _readout
        self._address: int | None = None
        if not self._readout:
            self._address = self.parent.reserve_wdw()

    @_GenericNode.parameter_callback("$vz_rotation", sweepable=True, cost=10)
    def write_pulse(self, normalized_phase: float) -> int:
        """Write the wave definition word to memory with the specified phase.

        :param normalized_phase: Phase of VZ rotation normalized to 2π
        :type normalized_phase: float
        :return: Error code (0 on success)
        :rtype: int
        """
        wdw = self.parent._ll_handler.build_vz_wdw(normalized_phase)
        if self._readout:
            return self.parent._ll_handler.write_readout_wave(wdw)
        else:
            return self.parent._ll_handler.add_wave_in_wave_memory(wdw, self._address)


class _Pulse(_GenericNode):
    """Object representing a pulse (wave definition word).

    Configuration dictionary keys:

    - ``_name`` (str): Name of the pulse (gate).
    - ``_readout`` (bool): If set, the pulse is a readout pulse. Defaults to ``False``.
    - ``_envelope`` (str): Name of the envelope to use for the pulse.
    - ``_switch_iq`` (bool): If set, I and Q values are switched. Defaults to ``False``.
    - ``_keep_last`` (bool): If set, the last sample will be placed at the output. Defaults to ``False``.
    - ``$duration`` (float): Duration of the pulse, in nanoseconds.
    - ``$gain`` (float): Gain, between -1 and 1.
    """

    nodetype = "pulse"

    def __init__(
        self,
        name: str,
        parent: SignalGeneratorNode,
        _readout: bool = False,
        _envelope: str | None = None,
        _switch_iq: bool = False,
        _keep_last: bool = False,
        _dac_target: int = 1,
    ) -> None:
        """Initialize the pulse.

        The ''_dac_target'' is used in combination with the crossbar, to send the pulse to a certain set of dacs.

        :param name: Name of the pulse
        :type name: str
        :param parent: Parent signal generator node
        :type parent: SignalGeneratorNode
        :param _readout: Whether this is a readout pulse, defaults to ``False``
        :type _readout: bool
        :param _envelope: Name of the envelope to use for the pulse
        :type _envelope: str or None
        :param _switch_iq: Whether to switch I and Q, defaults to ``False``
        :type _switch_iq: bool
        :param _keep_last: Whether to keep the last sample, defaults to ``False``
        :type _keep_last: bool
        :param _dac_target: Mask, sets where the pulse is sent, defaults to 1
        :type _dac_target: int
        :raises ValueError: If the envelope name is ``None`` or not found
        """
        super().__init__(name, parent)
        self._readout = _readout
        self._envelope = _envelope
        self._switch_iq = _switch_iq
        self._keep_last = _keep_last
        self._dac_target = _dac_target
        if _envelope is None:
            self.log.error("envelope not specified")
            raise ValueError("envelope not specified")
        # check if envelope exists and get the reference to use it later
        envelope_children = [child for child in self.parent.children if child.nodetype == "envelope"]
        if self._envelope not in [child.name for child in envelope_children]:
            self.log.error("envelope %s not found", self._envelope)
            raise ValueError(f"envelope {self._envelope} not found")
        self._envelope_ref = next(child for child in envelope_children if child.name == self._envelope)
        if not self._readout:
            self._address = self.parent.reserve_wdw()
        else:
            self._address = None
        # starting values for duration and gain
        self._wanted_duration = self._envelope_ref.natural_length
        self._wanted_gain = 0.0

    @_GenericNode.parameter_callback("$duration", sweepable=True, cost=10)
    def set_duration(self, duration: float) -> int:
        """Set the pulse duration.

        :param duration: Duration in nanoseconds
        :type duration: float
        :return: Error code (0 on success)
        :rtype: int
        """
        self._wanted_duration = _get_periods_from_clock(duration, self.parent._sampling_frequency)
        return self._write_pulse()

    @_GenericNode.parameter_callback("$gain", sweepable=True, cost=10)
    def set_gain(self, value: float) -> int:
        """Set the pulse gain.

        :param value: Gain value between -1 and 1
        :type value: float
        :return: Error code (0 on success)
        :rtype: int
        """
        self._wanted_gain = value
        return self._write_pulse()

    def _write_pulse(self) -> int:
        """Write the wave definition word to memory.

        :return: Error code (0 on success)
        :rtype: int
        """
        # build the wdw
        wdw = self.parent._ll_handler.build_pulse_wdw(
            envelope_wdw=self._envelope_ref.envelope_wdw,
            start_address=self._envelope_ref._address,
            duration=self._wanted_duration,
            natural_duration=self._envelope_ref.natural_length,
            normalized_gain=self._wanted_gain,
            switch_iq=self._switch_iq,
            keep_last=self._keep_last,
            dac_target_mask=self._dac_target,
        )
        if self._readout:
            return self.parent._ll_handler.write_readout_wave(wdw)
        else:
            return self.parent._ll_handler.add_wave_in_wave_memory(wdw, self._address)


class SignalGeneratorNode(_GenericNode):
    """Object representing the signal generator system.

    Configuration dictionary keys:

    - ``_name`` (str): Name of the signal generator node instance.
    - ``_ll_handler`` (GeneratorDriver): Low-level handler for the signal generator.
    - ``$dfrequency`` (float): Drive frequency, in MHz.
    - ``$rfrequency`` (float): Readout frequency, in MHz.
    - ``$rphase`` (float): Readout phase, in radians.
    - ``$rchannel`` (int): Readout trigger channel. Set to ``0`` to deactivate.
    - ``$dchannel`` (int): Drive trigger channel. Set to ``0`` to deactivate.
    - ``$lfsr_seed`` (int): Seed for the LFSR.
    - ``$drive_order`` (list[str]): Ordered list of pulse names to be generated.
    """

    nodetype = "signal_generation"
    wraps = [GeneratorDriver.__name__]

    def __init__(
        self,
        name: str,
        parent: _GenericNode,
        _ll_handler: GeneratorDriver,
    ) -> None:
        """Initialize the signal generator node.

        :param name: Name of the signal generator node
        :type name: str
        :param parent: Parent node in the system tree
        :type parent: _GenericNode
        :param _ll_handler: Low-level handler for the signal generator
        :type _ll_handler: GeneratorDriver
        """
        super().__init__(name, parent)
        self._clock_frequency = self.root.get_fabric_frequency()
        self._sampling_frequency = self.root.get_generation_sampling_frequency()
        self._ll_handler = _ll_handler
        # envelope and wdw memory caching
        self.init_memory()
        # create the rectangular envelope
        _RectangularEnvelope(name="_RECTANGULAR", parent=self)

    def init_memory(self) -> int:
        """Initialize the memory of the signal generator.

        :return: Error code (0 on success)
        :rtype: int
        """
        self._envelope_next_address: int = 0
        self._wdw_next_address: int = 0
        return self._ll_handler.clear_envelope_memory()

    def _reset_all(self) -> None:
        for child in self.children:
            child.parent = None
        # recreate the rect envelope
        _RectangularEnvelope(name="_RECTANGULAR", parent=self)
        # envelope and wdw memory caching
        self.init_memory()

    def reserve_envelope_segment(self, sample_depth: int) -> int:
        """Reserve a segment of the envelope memory.

        This function does not check if the segment is actually valid; it always
        returns an address and advances the internal pointer.

        :param sample_depth: Length of the segment in samples
        :type sample_depth: int
        :return: The starting address of the reserved segment
        :rtype: int
        """
        address = self._envelope_next_address
        self._envelope_next_address += sample_depth
        return address

    def reserve_wdw(self) -> int:
        """Reserve a wave definition word slot in memory.

        :return: The index of the reserved WDW slot
        :rtype: int
        """
        address = self._wdw_next_address
        self._wdw_next_address += 1
        return address

    @_GenericNode.parameter_callback("$dfrequency", sweepable=True, cost=1)
    def set_drive_frequency(self, frequency: float) -> int:
        """Set the drive frequency.

        :param frequency: Frequency in MHz
        :type frequency: float
        :return: Error code (0 on success)
        :rtype: int
        """
        return self._ll_handler.set_drive_modulation_frequency(frequency / self._sampling_frequency)

    @_GenericNode.parameter_callback("$rfrequency", sweepable=True, cost=1)
    def set_readout_frequency(self, frequency: float) -> int:
        """Set the readout frequency.

        :param frequency: Frequency in MHz
        :type frequency: float
        :return: Error code (0 on success)
        :rtype: int
        """
        return self._ll_handler.set_readout_modulation_frequency(frequency / self._sampling_frequency)

    @_GenericNode.parameter_callback("$rphase", sweepable=True, cost=1)
    def set_readout_phase(self, phase: float) -> int:
        """Set the readout phase.

        :param phase: Phase in radians
        :type phase: float
        :return: Error code (0 on success)
        :rtype: int
        """
        return self._ll_handler.set_readout_modulation_initial_phase(phase / (2 * np.pi))

    @_GenericNode.parameter_callback("$rchannel", sweepable=False, cost=1)
    def set_readout_channel(self, channel: int) -> int:
        """Set the readout trigger channel.

        :param channel: Channel number, set to 0 to deactivate
        :type channel: int
        :return: Error code (0 on success)
        :rtype: int
        """
        return self._ll_handler.set_trigger_channel(channel, "readout")

    @_GenericNode.parameter_callback("$dchannel", sweepable=False, cost=1)
    def set_drive_channel(self, channel: int) -> int:
        """Set the drive trigger channel.

        :param channel: Channel number, set to 0 to deactivate
        :type channel: int
        :return: Error code (0 on success)
        :rtype: int
        """
        return self._ll_handler.set_trigger_channel(channel, "drive")

    @_GenericNode.parameter_callback("$lfsr_seed", sweepable=True, cost=1)
    def set_lfsr_seed(self, seed: int) -> int:
        """Set the LFSR seed.

        :param seed: Seed for the LFSR
        :type seed: int
        :return: Error code (0 on success)
        :rtype: int
        """
        return self._ll_handler.set_lfsr_seed(seed)

    @_GenericNode.parameter_callback("$drive_order", sweepable=False, cost=1)
    def set_drive_order(self, order: list[str]) -> int:
        """Set the order of drive generation.

        :param order: Ordered list of pulse names to be generated
        :type order: list[str]
        :return: Error code (0 on success)
        :rtype: int
        """
        # for each element in the list, search the children for the matching
        # pulse or vzgate and build a list of addresses
        addresses: list[int] = []
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
                self.log.error("pulse %s not found in children", pulse_name)
                return -3
            if pulse._readout:
                self.log.error(
                    "pulse %s is a readout pulse and cannot be placed in the drive order",
                    pulse_name,
                )
                return -3
            addresses.append(pulse._address)
        # write the addresses to the memory mapped FIFO
        for order_index, wdw_index in enumerate(addresses):
            ret = self._ll_handler.add_wave_to_drive_wave_sequence(order_index, wdw_index)
            if ret != 0:
                self.log.error("failed to write drive order at index %s", order_index)
                return ret
        self.log.debug("drive order set to %s", order)
        return 0

    @_GenericNode.parameter_callback("$tmanual_dest", sweepable=False, cost=1)
    def set_manual_wave_destination(self, destination: str) -> int:
        """Set the destination where manual waves should be sent.

        :param destination: 'readout' or 'drive'
        :type destination: str
        :return: Error code (0 on success)
        :rtype: int
        """
        return self._ll_handler.set_manual_wave_destination(destination=destination)

    def manual_trigger(self) -> int:
        """Trigger the generator manually."""
        return self._ll_handler.trigger_manually()

    def create_child(self, name: str, of_type: str, **kwargs: dict[str, Any]) -> _GenericEnvelope | _Pulse | _VZGate:
        """Create a child node of the specified type.

        :param name: Name of the child node
        :type name: str
        :param of_type: Type of child node — ``"envelope"``, ``"pulse"`` or ``"vzgate"``
        :type of_type: str
        :param kwargs: Additional arguments to pass to the child node
        :type kwargs: dict[str, Any]
        :return: The created child node
        :rtype: _GenericEnvelope or _Pulse or _VZGate
        :raises ValueError: If the name is already taken or the type is unsupported
        """
        # check that the name is not already taken by an existing child
        if any(child.name == name for child in self.children):
            self.log.error("child with name %s already exists", name)
            raise ValueError(f"child with name {name} already exists")
        if of_type == "envelope":
            return _GenericEnvelope(name=name, parent=self, **kwargs)
        elif of_type == "pulse":
            return _Pulse(name=name, parent=self, **kwargs)
        elif of_type == "vzgate":
            return _VZGate(name=name, parent=self, **kwargs)
        else:
            self.log.error("unsupported child type %s", of_type)
            raise ValueError(f"unsupported child type {of_type}")
