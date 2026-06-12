"""Low-level FIREQ SoC overlay support and discovery helpers."""

import logging
import os
import re
import time
from typing import Any

import networkx as nx
import xrfclk
import xrfdc  # noqa: F401
from ._generic_node import _GenericNode
from pynq import PL, Overlay

from FIREQ_LL_API import FIREQSoC

from ._dependency_orchestrator import _DependencyOrchestrator
from ._generic_node import _driver_wrappers

logger = logging.getLogger(__name__)


class FIREQSystemNode(_GenericNode):
    """Object representing the entire FIREQ system.

    The name of this node is "system" and it is the root node of the complete FIREQ system tree.

    Dict definition:
        $shots : int, number of shots for each experiment
    """

    def __init__(
        self,
        bitfile_name: str,
    ) -> None:
        """Initialize the FIREQ system.

        Creates the system tree and initializes peripherals.

        :param bitfile_name: Path to the .bit file
        :type bitfile_name: str
        :raises RuntimeError: If overlay creation fails
        """
        # create the node and initialize the overlay
        super().__init__(name="system", parent=None)
        self.bitfile_name = bitfile_name
        self._fireq_soc = FIREQSoC(bitfile_name, ignore_version=False, init_clocks=True)
        if not self._fireq_soc.is_loaded():
            raise RuntimeError("Failed to load overlay")

        # create the dependency orchestrator object
        self._dependency_orchestrator = _DependencyOrchestrator()

        # build nodes, from the fireqsoc object
        self._init_nodes()
        self.register_update_function(self.make_func_label(self, "hw_shots"), self.update_hw_shots)
        self.shots = None
        self.hw_shots = None
        self.sw_shots = None

        # dictionary of references, which can be updated by nodes
        self._references = {}

        # build dependencies, from the fireqsoc object
        self._build_dependencies()

    def _init_nodes(self) -> None:
        """Initialize the nodes in the fireq system.

        Scans all of the ips to find the related node object class that wraps the driver.
        """
        for subsystem_name, (instance, ll_handler, driver_type) in self._fireq_soc.ips.items():
            if driver_type not in _driver_wrappers:
                continue
            logger.debug("Creating node for %s", subsystem_name)
            # create the child node
            _driver_wrappers[driver_type](name=instance, parent=self, _ll_handler=ll_handler)

    def create_child(self, name: str, of_type: str, **kwargs: dict[str, Any]) -> _GenericNode:
        """Create a child node of the specified type.

        :param name: Name of the node
        :type name: str
        :param of_type: Type of the node
        :type of_type: str
        :param kwargs: Additional arguments to pass to the node constructor
        :type kwargs: dict[str, Any]
        :return: Created node
        :rtype: _GenericNode
        """
        # depending on of_type, build the positional arguments needed for each node init

    @_GenericNode.parameter_callback(key="$shots", sweepable=True, cost=1)
    def set_shots(self, shots: int) -> int:
        """Set the number of shots.

        :param shots: Number of shots
        :type shots: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if shots <= 0:
            logger.error("Number of shots must be positive, got %s", shots)
            return -3
        self.shots = shots
        logger.debug("Shots set to %s", shots)
        return 0

    # TODO: this function depends on the shots set, so make it so the orchestrator knowns about it
    def update_hw_shots(self) -> bool:
        """Update the number of hardware shots."""
        if self.shots is None:
            self.hw_shots = None
            self.sw_shots = None
            return False
        # get the maximum number of hw shots by iterating over all fifo children and finding the minimum max_hw_shots across all fifo children
        fifo_children = [node for node in self.descendants if node.nodetype == "acquisition_fifo"]
        max_hw_shots_list = [node.max_hw_shots for node in fifo_children if node.max_hw_shots is not None]
        if not max_hw_shots_list:
            self.max_hw_shots = None
            self.hw_shots = None
            self.sw_shots = None
            return False
        self.max_hw_shots = min(max_hw_shots_list)

        if self.shots > self.max_hw_shots:
            self.hw_shots = self.max_hw_shots
            self.sw_shots = self.shots // self.max_hw_shots
        else:
            self.hw_shots = self.shots
            self.sw_shots = 1
        logger.debug("Hardware shots set to %s", self.hw_shots)
        return True

    def _build_dependencies(self) -> None:
        """Build the dependencies of the system.

        Calls the _build_dependencies method of all the nodes in the system.
        """
        for node in self.descendants:
            node._build_dependencies()

    def add_reference(self, ref_name: str, ref: Any) -> None:
        """Add a reference to a variable.

        :param ref_name: Name of the reference
        :type ref_name: str
        :param ref: Reference to the variable, must be mutable
        :type ref: Any
        """
        # add the reference to the dictionary but check if it already exists, if it does log a warning
        if ref_name in self._references:
            logger.error("Reference %s already exists, overwriting", ref_name)
            raise KeyError("Reference already exists")
        self._references[ref_name] = ref

    def get_reference(self, ref_name: str) -> Any:
        """Get a reference to a variable.

        :param ref_name: Name of the reference
        :type ref_name: str
        :return: Reference to the variable
        :rtype: Any
        """
        if ref_name not in self._references:
            logger.error("Reference %s not found", ref_name)
            raise KeyError("Reference %s not found", ref_name)
        return self._references[ref_name]

    @staticmethod
    def make_func_label(node: _GenericNode, func_name: str) -> str:
        """Create a unique function label for this specific function.

        It uses the path of a node to get an unique identifier for the func.

        :param node: Node where the function is defined
        :type node: _GenericNode
        :param func_name: Name of the function
        :type func_name: str
        :return: Unique function label
        :rtype: str
        """
        # get the path from the root node of "node"
        path = node.separator.join([""] + [n.name for n in node.path])
        return f"{path}/{func_name}"

    def register_update_function(self, func_label: str, update_function: callable) -> None:
        """Register an update function for a node.

        :param func_label: Lable to register the update function
        :type func_label: str
        :param update_function: Function to call when the node needs to be updated
        :type update_function: callable
        """
        self._dependency_orchestrator.add_node(func_label, update_function)

    def add_dependency(self, func_label: str, depends_on: str | list[str]) -> None:
        """Add a dependency between `func_label` and `depends_on`.

        :param func_label: Function label that depends on `depends_on`
        :type func_label: str
        :param depends_on: Single or many function labels defining the dependency
        :type depends_on: str
        """
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        for dep in depends_on:
            self._dependency_orchestrator.add_dependency(func_label, dep)

    def run_experiment(self) -> None:
        """Run the experiment.

        Will resolve intra-system dependencies before running.
        """
        # update the dependencies
        self._dependency_orchestrator.update()

    # Sytem func
    def get_acqisition_sampling_frequency(self) -> float:
        """Get the acquisition sampling frequency.

        :return: Acquisition sampling frequency in MHz
        :rtype: float
        """
        return self._fireq_soc.adc_samplerate

    def get_fabric_frequency(self) -> float:
        """Get the fabric frequency.

        :return: Fabric frequency in MHz
        :rtype: float
        """
        return self._fireq_soc.fabric_frequency

    def get_generation_sampling_frequency(self) -> float:
        """Get the generation sampling frequency.

        :return: Generation sampling frequency in MHz
        :rtype: float
        """
        return self._fireq_soc.dac_samplerate

    def get_axi_stream_interface_map(self, node_name: str) -> dict[str, int]:
        """Get the axi stream interface map for a node.

        :param node_name: Name of the node
        :type node_name: str
        :return: Dictionary mapping the interface names to the interface ids
        :rtype: dict[str, int]
        """
        return self._fireq_soc._fireq_parser.get_interface_map(node_name, self._fireq_soc._fireq_parser.dataflow_graph)
