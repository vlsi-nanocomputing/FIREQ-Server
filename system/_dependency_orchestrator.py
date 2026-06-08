"""Orchestrator for dependencies between variables."""

import logging

import networkx as nx

logger = logging.getLogger(__name__)


class _DependencyOrchestrator:
    """Dependency rapresentation and resolution using DAG.

    Each node in the graph represents a variable, which can be updated with a function.
    Nodes can depend on other nodes, representing the dependency between variables.
    Variables can be then updated in the correct order, respecting the dependencies.
    """

    def __init__(self) -> None:
        """Initialize the dependency DAG."""
        self._dependency_graph = nx.DiGraph()
        self._topological_order = None
        self._start_nodes = None

    def _invalidate_cached_order(self) -> None:
        """Invalidate the cached topological order."""
        self._topological_order = None
        self._start_nodes = None

    def add_node(self, name: str, update_function: callable) -> None:
        """Add a node with a unique name to the graph.

        The name is a placeholder for the variable, the update function is the function that updates the variable.
        The update function must be callable, must not take any arguments and must return a boolean indicating
        if the update has changed the variable.
        A downstram dependency will be updated only if at least one of the upstream dependencies has changed.
        When in doubt, make the function return True.

        :param name: Name of the node
        :type name: str
        :param update_function: Function that updates the variable
        :type update_function: callable
        """
        if name in self._dependency_graph.nodes:
            raise ValueError(f"Node {name} already exists in the graph.")
        self._dependency_graph.add_node(name, update_function=update_function)
        self._invalidate_cached_order()

    def add_dependency(self, node: str, depends_on: str) -> None:
        """Add a dependency between `node` and `depends_on`.

        The update function of `depends_on` will be called before the update function of `node`.

        :param: node: Node that depends on `depends_on`
        :type node: str
        :param depends_on: Dependency of `node`
        :type depends_on: str
        """
        if node not in self._dependency_graph.nodes:
            raise ValueError(f"Node {node} not found in the graph.")
        if depends_on not in self._dependency_graph.nodes:
            raise ValueError(f"Node {depends_on} not found in the graph.")
        # add edge node -> depends_on, encoding "data flow"
        self._dependency_graph.add_edge(depends_on, node)
        # make sure that the graph is still a DAG (no cycles)
        if not nx.is_directed_acyclic_graph(self._dependency_graph):
            # remove the edge
            self._dependency_graph.remove_edge(depends_on, node)
            raise ValueError("Adding this dependency creates a cycle in the graph.")
        self._invalidate_cached_order()

    def update(self) -> None:
        """Update all the nodes in the graph, respecting the dependencies.

        Updates will be called only if at least one of the upstream dependencies has changed (short circuiting/pruning).
        The function will return prematurely if all of the update function within a batch return False
        (e.g. the active frontier is unchanged).
        """
        if self._topological_order is None or self._start_nodes is None:
            self._topological_order = list(nx.topological_sort(self._dependency_graph))
            self._start_nodes = set(
                [node for node in self._topological_order if self._dependency_graph.in_degree(node) == 0]
            )

        # set indicating the nodes that need to update
        to_run = set(self._start_nodes)

        # the number of evaluated nodes
        evaluated_nodes = 0
        to_run_length = len(to_run)
        for node in self._topological_order:
            if node not in to_run:
                continue

            logger.debug("Updating node: %s", node)
            did_change = self._dependency_graph.nodes[node]["update_function"]()
            logger.debug("Node %s has changed: %s", node, did_change)
            evaluated_nodes += 1

            if did_change:
                for successor in self._dependency_graph.successors(node):
                    to_run.add(successor)
                    to_run_length += 1

            if evaluated_nodes == to_run_length:
                # TODO: google gemini thinks that this is a critical bug, I disagree. Nevertheless, make sure this works
                # if the node has been avaluated and no successors have been added to the `to_run` set,
                # if the evaluated nodes count equals the number of nodes to run, no more nodes have been set to run
                # this means that the active front has been exhausted and no more nodes need to be updated
                logger.debug("No more nodes to update.")
                return
