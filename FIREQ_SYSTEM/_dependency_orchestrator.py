"""Orchestrator for dependencies between variables."""

from __future__ import annotations

import logging
from collections.abc import Callable

import networkx as nx

logger = logging.getLogger(__name__)


class _DependencyOrchestrator:
    """Dependency representation and resolution using a directed acyclic graph (DAG).

    Each node in the graph represents a variable that can be updated with a
    callable.  Nodes may depend on other nodes, encoding the dependency between
    variables.  Variables are then updated in topological order, and a downstream
    node is only visited when at least one of its upstream dependencies has
    changed (short-circuiting / pruning).
    """

    def __init__(self) -> None:
        """Initialize the dependency DAG."""
        self.log = logging.getLogger(__name__)
        self._dependency_graph = nx.DiGraph()
        self._update_funcs: list[Callable] | None = None
        self._successors_ids: list[list[int]] | None = None
        self._start_node_ids: set[int] | None = None
        self._valid_cached_order: bool = False

    def set_logger(self, new_logger: logging.Logger) -> None:
        """Set the logger for this object.

        :param new_logger: Logger object to use
        :type new_logger: logging.Logger
        """
        self.log = new_logger

    def _invalidate_cached_order(self) -> None:
        """Invalidate the cached topological order and start nodes."""
        self._valid_cached_order = False

    def _compute_cached_order(self) -> None:
        """Create the data structures necessary to determine the order of updates to run the system update."""
        # 1. Topological order of node objects (strings, whatever type)
        topological_order = list(nx.topological_sort(self._dependency_graph))

        # 2. Map each node to its integer ID
        node_to_id = {node: idx for idx, node in enumerate(topological_order)}

        # 3. Pre‑computed lists indexed by integer ID
        self._update_funcs = [self._dependency_graph.nodes[node]["update_function"] for node in topological_order]

        self._successors_ids = [
            [node_to_id[succ] for succ in self._dependency_graph.successors(node)] for node in topological_order
        ]

        # 4. Start‑node set containing integer IDs
        self._start_node_ids = {
            node_to_id[node] for node in topological_order if self._dependency_graph.in_degree(node) == 0
        }
        self._valid_cached_order = True

    def add_node(self, name: str, update_function: Callable[[], bool]) -> None:
        """Add a node with a unique name to the graph.

        The update function must take no arguments and return a ``bool`` indicating
        whether the variable changed.  A downstream dependency is updated only if
        at least one upstream dependency returned ``True``.  When in doubt, make
        the function return ``True``.

        :param name: Unique name of the node
        :type name: str
        :param update_function: Function that updates the variable
        :type update_function: Callable[[], bool]
        :raises ValueError: If a node with the same name already exists
        """
        if name in self._dependency_graph.nodes:
            raise ValueError(f"Node {name} already exists in the graph.")
        self._dependency_graph.add_node(name, update_function=update_function)
        self._invalidate_cached_order()

    def add_dependency(self, node: str, depends_on: str) -> None:
        """Add a dependency between ``node`` and ``depends_on``.

        The update function of ``depends_on`` is called before that of ``node``.

        :param node: Node that depends on ``depends_on``
        :type node: str
        :param depends_on: Upstream dependency of ``node``
        :type depends_on: str
        :raises ValueError: If either node is not in the graph, or if adding the
            dependency would create a cycle
        """
        if node not in self._dependency_graph.nodes:
            raise ValueError(f"Node {node} not found in the graph.")
        if depends_on not in self._dependency_graph.nodes:
            raise ValueError(f"Node {depends_on} not found in the graph.")
        # add edge depends_on -> node, encoding "data flow"
        self._dependency_graph.add_edge(depends_on, node)
        # make sure that the graph is still a DAG (no cycles)
        if not nx.is_directed_acyclic_graph(self._dependency_graph):
            # remove the edge
            self._dependency_graph.remove_edge(depends_on, node)
            raise ValueError("Adding this dependency creates a cycle in the graph.")
        self._invalidate_cached_order()

    def update(self) -> None:
        """Update all nodes in the graph respecting their dependencies.

        Nodes are visited in topological order.  A node is evaluated only if it is
        in the active frontier (i.e. at least one upstream dependency changed).
        Evaluation stops early as soon as the active frontier is exhausted.
        """
        if not self._valid_cached_order:
            self._compute_cached_order()

        # local variable caching
        is_debug = self.log.isEnabledFor(logging.DEBUG)
        to_run = set(self._start_node_ids)
        evaluated_nodes = 0
        to_run_length = len(to_run)

        for node_id, update_func in enumerate(self._update_funcs):
            if node_id not in to_run:
                continue

            did_change = update_func()
            if is_debug:
                self.log.debug("Updated node %s, has changed: %s", node_id, did_change)

            evaluated_nodes += 1

            if did_change:
                for succ_id in self._successors_ids[node_id]:
                    if succ_id not in to_run:
                        to_run.add(succ_id)
                        to_run_length += 1

            if evaluated_nodes == to_run_length:
                self.log.debug("No more nodes to update.")
                return
