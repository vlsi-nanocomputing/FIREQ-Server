# file: fireq-utils/server/models/queue_items.py
"""Queue item types for inter-thread communication.

This module defines typed dataclasses for items passed through queue_out
from the main thread to the sender thread. Using dataclasses instead of
plain dicts provides type safety and clear documentation of the protocol.

Streaming commands (run_experiment, run_sweep) use these types.
Simple commands (ping, status, reset_*, abort) continue using plain dicts.

Note: Binary data arrays are already copied in ol_adapter.py to avoid
race conditions with the sender thread.
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class StreamHeader:
    """Header metadata for experiment/sweep - sender calls _send_message().

    :param type: Message type identifier ("experiment_header" or "sweep_header").
    :type type: str
    :param metadata: Full metadata dict to serialize as JSON.
    :type metadata: dict
    """

    type: str
    metadata: dict


@dataclass
class BinaryChunk:
    """Binary data chunk - sender calls _send_binary_frame() for each ADC.

    The binary_data arrays are already copied in ol_adapter.py:2241,2264
    to avoid race conditions between main and sender threads.

    :param type: Message type ("experiment_binary_chunk" or "sweep_binary_point").
    :type type: str
    :param binary_data: Mapping of ADC index to numpy array data.
    :type binary_data: dict[int, np.ndarray]
    :param timing: Optional (fpga_wait_ms, sw_overhead_ms) for experiments.
    :type timing: tuple[float, float] | None
    """

    type: str
    binary_data: dict[int, np.ndarray]
    timing: tuple[float, float] | None = None


@dataclass
class StreamTiming:
    """Final timing/status message - sender calls _send_message().

    :param type: Message type ("experiment_timing" or "sweep_status").
    :type type: str
    :param metadata: Full metadata dict to serialize as JSON.
    :type metadata: dict
    """

    type: str
    metadata: dict


__all__ = ["StreamHeader", "BinaryChunk", "StreamTiming"]
