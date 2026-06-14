"""Low-level FIREQ SoC overlay support and discovery helpers."""

import logging
from queue import Queue
from typing import Any

from FIREQ_LL_API import FIREQSoC

from ._dependency_orchestrator import _DependencyOrchestrator
from ._generic_node import _driver_wrappers, _GenericNode
from ._utils import _MutableRef
from .dma_node import DMANode
from .trigger_generator_node import TriggerGeneratorNode

logger = logging.getLogger(__name__)


class FIREQSystemNode(_GenericNode):
    """Object representing the entire FIREQ system.

    This is the root node of the complete FIREQ system tree. It is responsible for
    loading the bitfile, discovering IPs, building the node tree, and orchestrating
    inter-node dependencies.

    Dictionary definition for configuration:

    .. list-table::
       :header-rows: 1

       * - Key
         - Type
         - Description
       * - ``$shots``
         - ``int``
         - Number of shots for each experiment
    """

    def __init__(self, bitfile_name: str) -> None:
        """Initialize the FIREQ system.

        Creates the system tree and initializes peripherals.

        :param bitfile_name: Path to the ``.bit`` file
        :type bitfile_name: str
        :raises RuntimeError: If overlay creation fails
        """
        # create the node and initialize the overlay
        super().__init__(name="system", parent=None)
        self.bitfile_name = bitfile_name
        self._fireq_soc = FIREQSoC(bitfile_name, ignore_version=False, init_clocks=True)
        if not self._fireq_soc.is_loaded():
            raise RuntimeError("Failed to load overlay")

        # create the dependency orchestrator object and the references dictionary
        self._dependency_orchestrator = _DependencyOrchestrator()
        self._references: dict[str, Any] = {}

        # build nodes from the FIREQSoC object
        self._init_nodes()

        # build refs and other
        self.shots: int | None = None
        self.requested_hw_shots = _MutableRef()
        self.hw_shots = _MutableRef()
        self.max_hw_shots = _MutableRef()
        self.register_update_function(self.make_func_label(self, "max_hw_shots"), self.update_max_hw_shots)
        self.register_update_function(self.make_func_label(self, "requested_hw_shots"), self.update_requested_hw_shots)
        self.register_update_function(self.make_func_label(self, "hw_shots"), self.update_hw_shots)
        self.add_reference(self.make_func_label(self, "hw_shots"), self.hw_shots)

        # input references
        self._max_hw_shot_list: list[_MutableRef] | None = None
        self._hw_supported_hw_shots: _MutableRef | None = None

        # build dependencies, from the FIREQSoC object
        self._build_dependencies()

        # find the dma nodes and trigger generator nodes, save them to lists
        self._dma_nodes: list[DMANode] = [node for node in self.children if node.nodetype == "dma"]
        self._trigger_generator_nodes: list[TriggerGeneratorNode] = [
            node for node in self.children if node.nodetype == "trigger_generator"
        ]

        # check that we only have one trigger generator node, raise not implemented error if not
        if len(self._trigger_generator_nodes) != 1:
            raise NotImplementedError(
                f"Only one trigger generator node is supported, got {len(self._trigger_generator_nodes)}"
            )

    def _init_nodes(self) -> None:
        """Initialize the nodes in the FIREQ system.

        Scans all of the IPs discovered by the low-level SoC and creates the
        corresponding wrapper node for each supported driver type.
        """
        for subsystem_name, (instance, ll_handler, driver_type) in self._fireq_soc.ips.items():
            if driver_type not in _driver_wrappers:
                continue
            logger.debug("Creating node for %s", subsystem_name)
            # create the child node
            _driver_wrappers[driver_type](name=subsystem_name, parent=self, _ll_handler=ll_handler)

    def create_child(self, name: str, of_type: str, **kwargs: dict[str, Any]) -> _GenericNode:
        """Create a child node of the specified type.

        This method is not supported on the root system node; child nodes are
        created automatically during :meth:`_init_nodes`.

        :param name: Name of the node
        :type name: str
        :param of_type: Type of the node
        :type of_type: str
        :param kwargs: Additional arguments to pass to the node constructor
        :type kwargs: dict[str, Any]
        :return: Created node
        :rtype: _GenericNode
        :raises NotImplementedError: Always raised on the root node
        """
        raise NotImplementedError(
            "create_child is not supported on the root system node. "
            "Use configuration dictionaries on child nodes instead."
        )

    @_GenericNode.parameter_callback(key="$shots", sweepable=True, cost=1)
    def set_shots(self, shots: int) -> int:
        """Set the number of shots.

        :param shots: Number of shots (must be positive)
        :type shots: int
        :return: Error code (0 on success)
        :rtype: int
        """
        if shots <= 0:
            self.shots = None
            logger.error("Number of shots must be positive, got %s", shots)
            return -3
        self.shots = shots
        logger.debug("Shots set to %s", shots)
        return 0

    def update_max_hw_shots(self) -> bool:
        """Update the maximum number of hardware shots.

        Computes the maximum number of hardware shots that can be executed without
        overflowing any acquisition FIFO, then sets ``hw_shots`` and ``sw_shots``
        accordingly.

        :return: ``True`` if the number of shots changed, ``False`` otherwise
        :rtype: bool
        """
        # given the alloable hw shots by the fifos and the maximum hw shots supported by the system
        # compute the maximum number of hw shots
        # set the number of max shots to a value that is invalid
        max_hw_shots = None
        for ref in self._max_hw_shot_list:
            if ref:
                if max_hw_shots is None:
                    max_hw_shots = ref["value"]
                else:
                    max_hw_shots = min(max_hw_shots, ref["value"])
        if max_hw_shots is None or max_hw_shots == 0:
            self.max_hw_shots.clear()
        max_hw_shots = min(max_hw_shots, self._hw_supported_hw_shots["value"])
        self.max_hw_shots["value"] = max_hw_shots
        logger.debug("Hardware shots set to %s", self.max_hw_shots["value"])
        return self.max_hw_shots.hash_and_compare()

    def update_requested_hw_shots(self) -> bool:
        """Update the requested number of hardware shots."""
        return self.requested_hw_shots.hash_and_compare()

    def update_hw_shots(self) -> bool:
        """Update the actual number of hardware shots."""
        self.hw_shots["value"] = min(self.max_hw_shots["value"], self.requested_hw_shots["value"])
        logger.debug("Hardware shots set to %s", self.hw_shots["value"])
        return self.hw_shots.hash_and_compare()

    def _build_dependencies(self) -> None:
        """Build the dependencies of the system.

        Calls the ``_build_dependencies`` method of all descendant nodes.
        Nodes that do not implement this method are silently skipped.
        """
        for node in self.descendants:
            if hasattr(node, "_build_dependencies"):
                node._build_dependencies()
        # get references
        fifo_children = [node for node in self.children if node.nodetype == "acquisition_fifo"]
        self._max_hw_shot_list: list[_MutableRef] = []
        for node in fifo_children:
            self._max_hw_shot_list.append(self.get_reference(self.make_func_label(node, "max_hw_shots")))
        self._hw_supported_hw_shots = self.get_reference("hw_supported_hw_shots")
        # register dependency for the hw shot attribute
        self.add_dependency(
            self.make_func_label(self, "max_hw_shots"),
            depends_on=[self.make_func_label(node, "max_hw_shots") for node in fifo_children],
        )
        self.add_dependency(
            self.make_func_label(self, "hw_shots"),
            depends_on=[self.make_func_label(self, "requested_hw_shots"), self.make_func_label(self, "max_hw_shots")],
        )

    def add_reference(self, ref_name: str, ref: object) -> None:
        """Add a reference to a variable.

        :param ref_name: Name of the reference
        :type ref_name: str
        :param ref: Reference to the variable (must be mutable)
        :type ref: Any
        :raises KeyError: If a reference with the same name already exists
        """
        # add the reference to the dictionary but check if it already exists,
        # if it does log a warning and raise
        if ref_name in self._references:
            logger.error("Reference %s already exists, overwriting", ref_name)
            raise KeyError(f"Reference {ref_name} already exists")
        self._references[ref_name] = ref

    def get_reference(self, ref_name: str) -> Any:
        """Get a reference to a variable.

        :param ref_name: Name of the reference
        :type ref_name: str
        :return: Reference to the variable
        :rtype: Any
        :raises KeyError: If the reference is not found
        """
        if ref_name not in self._references:
            logger.error("Reference %s not found", ref_name)
            raise KeyError(f"Reference {ref_name} not found")
        return self._references[ref_name]

    @staticmethod
    def make_func_label(node: _GenericNode, func_name: str) -> str:
        """Create a unique function label for a specific function.

        Uses the path of a node to build a unique identifier for the function.

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

        :param func_label: Label to register the update function under
        :type func_label: str
        :param update_function: Function to call when the node needs to be updated
        :type update_function: callable
        """
        self._dependency_orchestrator.add_node(func_label, update_function)

    def add_dependency(self, func_label: str, depends_on: str | list[str]) -> None:
        """Add a dependency between ``func_label`` and ``depends_on``.

        :param func_label: Function label that depends on ``depends_on``
        :type func_label: str
        :param depends_on: Single label or list of labels that ``func_label``
            depends on
        :type depends_on: str or list[str]
        """
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        for dep in depends_on:
            self._dependency_orchestrator.add_dependency(func_label, dep)

    def run_experiment(self, queue: Queue) -> None:
        """Run the experiment.

        Resolves intra-system dependencies and updates all registered variables
        before the experiment starts.
        """
        # given the max number of hw shots, the number of shots plan the experiment
        # then, set the number of hw shots for each sw shot in the trigger generator
        # run the experiment and extract data from the dma by sending it to the queque
        # TODO: add a series of checks
        executed_shots = 0
        self.requested_hw_shots["value"] = self.shots
        sw_shots = 0
        # update the dependencies
        self._dependency_orchestrator.update()
        # get the actual number of hw shots
        hw_shots = self.hw_shots["value"]
        while executed_shots < self.shots["value"]:
            extra_shots = (executed_shots + hw_shots) - self.shots
            if extra_shots > 0:
                # reduce the number of hw shots and rerun dependency
                logger.debug("Reducing the number of hw shots to properly run the expeirment")
                hw_shots = hw_shots - extra_shots
                self.requested_hw_shots["value"] = hw_shots
                self._dependency_orchestrator.update()
                if self.hw_shots["value"] != hw_shots:
                    logger.error("Failed to set the number of hw shots to the correct amount")
                    raise RuntimeError("Failed to set the number of hw shots to the correct amount")
            # init the dma
            for dma_nodes in self._dma_nodes:
                dma_nodes.init_dma()
            # start the experiment
            self._trigger_generator_nodes[0].start_experiment()
            # wait for the experiment to finish
            while not self._trigger_generator_nodes[0].is_done():
                pass
            # extract data from the dma
            for dma_node in self._dma_nodes:
                dma_node.transfer_all(queue)
            # increment the number of executed shots
            executed_shots += hw_shots
            sw_shots += 1
        logger.debug("Experiment finished, executed %s shots in %s software shots", executed_shots, sw_shots)

    # ------------------------------------------------------------------
    # System properties
    # ------------------------------------------------------------------

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
        """Get the AXI-Stream interface map for a node.

        :param node_name: Name of the node
        :type node_name: str
        :return: Dictionary mapping interface names to interface IDs
        :rtype: dict[str, int]
        """
        return self._fireq_soc._fireq_parser.get_interface_map(node_name, self._fireq_soc._fireq_parser.dataflow_graph)
