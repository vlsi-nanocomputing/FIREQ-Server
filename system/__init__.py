"""Public API exports for the FIREQ system node representation.

This package provides a high-level, tree-structured representation of the FIREQ
hardware system.  Each hardware IP (acquisition, signal generation, trigger
generation, DMA, FIFO, and AXI-Stream switches) is wrapped in a dedicated node
class that derives from :class:`._generic_node._GenericNode`.

The root of the tree is :class:`.FIREQSystemNode`, which loads the bitfile,
discovers peripherals, and orchestrates inter-node dependencies through a
directed acyclic graph (DAG).
"""

from .acquisition_node import AcquisitionNode
from .dma_node import DMANode
from .fifo_node import FIFONode
from .fireq_system_node import FIREQSystemNode
from .signal_generator_node import SignalGeneratorNode
from .switch_node import SwitchNode
from .trigger_generator_node import TriggerGeneratorNode

__all__ = [
    "AcquisitionNode",
    "SignalGeneratorNode",
    "TriggerGeneratorNode",
    "SwitchNode",
    "DMANode",
    "FIFONode",
    "FIREQSystemNode",
]
