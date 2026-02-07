# file: fireq-utils/server/network/__init__.py
"""Network layer for FIREQ server.

This package provides the TCP server that accepts client connections
and routes commands to the experiment execution layer.
"""

from .fireq_server import FIREQServer

__all__ = ["FIREQServer"]
