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
        self._dependency_graph = nx.DiGraph()
        self._topological_order: list[str] | None = None
        self._start_nodes: set[str] | None = None

    def _invalidate_cached_order(self) -> None:
        """Invalidate the cached topological order and start nodes."""
        self._topological_order = None
        self._start_nodes = None

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
        if self._topological_order is None or self._start_nodes is None:
            self._topological_order = list(nx.topological_sort(self._dependency_graph))
            self._start_nodes = {
                node for node in self._topological_order if self._dependency_graph.in_degree(node) == 0
            }

        # set indicating the nodes that need to update
        to_run: set[str] = set(self._start_nodes)

        # the number of evaluated nodes
        evaluated_nodes: int = 0
        to_run_length: int = len(to_run)
        for node in self._topological_order:
            if node not in to_run:
                continue

            logger.debug("Updating node: %s", node)
            did_change: bool = self._dependency_graph.nodes[node]["update_function"]()
            logger.debug("Node %s has changed: %s", node, did_change)
            evaluated_nodes += 1

            if did_change:
                for successor in self._dependency_graph.successors(node):
                    if successor not in to_run:
                        to_run.add(successor)
                        to_run_length += 1

            if evaluated_nodes == to_run_length:
                # If all queued nodes have been evaluated and no successors were
                # added, the active frontier is exhausted and we can stop early.
                logger.debug("No more nodes to update.")
                return
