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
import copy
import msgpack

import numpy as np
import struct


@dataclass
class SimpleMessage:
    """Simple message type, will be sent over the socket as a json.

    :param type: Message type identifier ("experiment_header" or "sweep_header").
    :type type: str
    :param metadata: Full metadata dict to serialize as JSON.
    :type metadata: dict
    """

    header: dict
    data: bytes

    def to_buffers(self) -> tuple:
        nheader = copy.deepcopy(self.header)
        if self.data:
            nheader["tdata"] = len(self.data)
        header_bytes = msgpack.packb(nheader)
        header_size_bytes = struct.pack(">I", len(header_bytes))  # 4 bytes, network byte order
        if self.data:
            return (header_size_bytes, header_bytes, self.data)
        return (header_size_bytes, header_bytes)


@dataclass
class BinaryChunk:
    """Chunk of data to send over the socket.

    Will send a json header followed by a binary payload.
    The header will always append a "size" key to determine the size of the binary payload at the receiver.

    :param type: Message type ("experiment_binary_chunk" or "sweep_binary_point").
    :type type: str
    :param binary_data: Mapping of Acq IP index to numpy array data.
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


__all__ = ["SimpleMessage", "BinaryChunk", "StreamTiming"]
