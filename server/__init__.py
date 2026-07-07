"""FIREQ Server Module.

This package exposes the main adapter, data structures, exceptions, and the TCP server
class for the FIREQ system.
"""

from .network.fireq_server import FIREQServer

__all__ = [
    # Main Server Class
    "FIREQServer",
]
