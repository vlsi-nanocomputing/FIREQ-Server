"""Low-level FIREQ SoC overlay support and discovery helpers."""

import os
import re
import time
import xrfclk
import xrfdc  # noqa: F401
from pynq import PL, Overlay
from _generic_node import _GenericNode
from FIREQ_LL_API import FIREQSoC
from ._dependency_orchestrator import _DependencyOrchestrator


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
        self._overlay = FIREQSoC(bitfile_name, ignore_version=False, init_clocks=True)
        if not self._overlay.is_loaded():
            raise RuntimeError("Failed to load overlay")

        # create the dependency orchestrator object
        self._dependency_orchestrator = _DependencyOrchestrator()

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
