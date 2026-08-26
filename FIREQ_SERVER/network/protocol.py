"""FIREQ network protocol specification.

The protocol supports a data packet that is made up of two parts:
- HEADER: a dictionary send over the network as a msgpack, preserving type
- DATA(optional): an optional data packet (bytes) following the header

The data part of the packet is only there if the header contains the keyword "tsize",
which determines the size in bytes of the trailing data.

The whole packet is serialized by setting the first 4 bytes as the size of the header,
followed by the bytes of the header and the optional data bytes.
"""

import copy
import struct
from dataclasses import dataclass

import msgpack

from FIREQ_SYSTEM.dma_node import DMAPayload


@dataclass
class FIREQNetworkPacket:
    """Dataclass that incapsulates a network packet.

    It is defined by a header and a data field.
    The data field is a simple byte array.
    """

    header: dict
    data: bytes | None = None

    def to_buffers(self) -> tuple:
        nheader = copy.deepcopy(self.header)
        if self.data:
            nheader["tdata"] = len(self.data)
        header_bytes = msgpack.packb(nheader)
        header_size_bytes = struct.pack(">I", len(header_bytes))  # 4 bytes, network byte order
        if self.data:
            return (header_size_bytes, header_bytes, self.data)
        return (header_size_bytes, header_bytes)

    def get(self, key: str, default: object = None) -> object:
        """Get key value from the header dict."""
        return self.header.get(key, default)


@dataclass
class NetworkDMAPayload(DMAPayload):
    """Object that holds the payload for a DMA transfer and that can be transferred through the network.

    It can be sent over a socket with the to_buffers method, using sock.sendmsg().
    """

    def to_buffers(self) -> tuple[bytes, bytes, bytes]:
        """Return a sequence of bytes-like objects ready for sendmsg().

        The layout:
          buf[0] : 4 bytes header size (big-endian unsigned int)
          buf[1-N] : msgpack-packed header
          buf[N+1: M] : raw binary payload

        The msgpack header contains a "tsize" key that specifies the size of the trailing binary payload.
        """
        header_bytes = msgpack.packb(
            {
                "type": "dma_package",
                "source": self.source,
                "shots": self.shots,
                "format": self.dtype,
                "tsize": len(self.data),
            }
        )
        header_size_bytes = struct.pack(">I", len(header_bytes))  # 4 bytes, network byte order
        return (header_size_bytes, header_bytes, self.data)


def unpack_header(serial: bytes) -> dict:
    """Unpack bytes to return a dictionary using msgpack."""
    return msgpack.unpackb(serial, raw=False)


def get_command(message: dict) -> str:
    return message.get("cmd", "")


def get_sweep_variables(message: dict) -> dict:
    return message.get("variables", {})
