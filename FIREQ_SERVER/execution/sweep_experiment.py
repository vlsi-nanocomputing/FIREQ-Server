"""Sweep experiment module.

This module implements the SweepExperiment class, which is responsible for executing multi-point sweep experiments on
the FIREQ system. It provides methods to parse sweep configurations, compute variable values, and execute the experiment
"""

from __future__ import annotations

import ast
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np

from ..utils import SweepAbortedError

if TYPE_CHECKING:
    from ..fireq_server import FIREQServer


# AST nodes allowed in a sweep expression: numbers (int or float), variables, unary and binary + and -.
# Anything else (calls, attribute access, subscripts, other operators) is rejected.
_ALLOWED_EXPRESSION_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.UAdd,
    ast.USub,
    ast.Name,
    ast.Load,
    ast.Constant,
)


class SweepExperiment:
    """Execute multi-sweep points using high-level callbacks."""

    def __init__(
        self,
        server: FIREQServer,
        sweep_callbacks: list,
        variables: dict,
        should_abort: Callable[[], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize with the sweeping expressions and variables values.

        Raises if an expression is invalid or references a variable that is not defined.

        :param server: object representing the FIREQ server
        :type server: FIREQServer
        :param sweep_callbacks: A list of tuples containing the sweep expressions.
        :type sweep_callbacks: list[tuple[callable, str, int]]
        :param variables: A dictionary mapping each variable name to its definition.
        :type variables: dict[str, dict]
        :param should_abort: Optional callable checked right before running each sweep point; when it returns True
            the sweep stops by raising SweepAbortedError.
        :type should_abort: Callable[[], bool] | None
        """
        # Fireq Node
        self._server = server
        self._should_abort = should_abort or (lambda: False)
        self.log = logger or logging.getLogger(__name__)

        # current iteration of variables
        self._current_sweep_point = {}

        # compute variables values
        self.computed_vars = self._compute_variable_values(variables)

        # parse the configuration
        self.sweep_routine, self.vars_order = self._parse_callbacks(sweep_callbacks)

        # every variable used in an expression must be defined, check now instead of failing mid-sweep
        undefined = [var for var in self.vars_order if var not in self.computed_vars]
        if undefined:
            raise KeyError(f"Sweep expressions use undefined variables: {undefined}")

    @staticmethod
    def _check_sweep_expression(expr: str) -> ast.Expression:
        """Check the sweep expression for validity and parse it.

        The first character of the expression must be '#'. The rest may only contain numbers
        (int or float, e.g. 0.5 or 1e9), variable names, the + and - operators and parentheses.

        :param expr: expression to check
        :type expr: str
        :return: the parsed expression, without the leading '#'
        :rtype: ast.Expression
        :raises ValueError: if the expression is not valid
        """
        if not expr.startswith("#"):
            raise ValueError(f"Invalid sweep expression: {expr}. Must start with '#'.")
        try:
            tree = ast.parse(expr[1:].strip(), mode="eval")
        except SyntaxError as e:
            raise ValueError(f"Invalid sweep expression '{expr}': {e.msg}") from e
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_EXPRESSION_NODES):
                raise ValueError(
                    f"Invalid sweep expression '{expr}': {type(node).__name__} is not allowed, "
                    "only numbers, variables, + and - can be used"
                )
            if isinstance(node, ast.Constant) and (
                isinstance(node.value, bool) or not isinstance(node.value, (int, float))
            ):
                raise ValueError(f"Invalid sweep expression '{expr}': constant {node.value!r} is not a real number")
        return tree

    @staticmethod
    def _get_vars(tree: ast.Expression) -> tuple[str]:
        """Extract the involved variables from a parsed expression.

        :param tree: expression parsed by _check_sweep_expression
        :type tree: ast.Expression
        :return: the involved variables, without duplicates, in order of appearance
        :rtype: tuple[str]
        """
        return tuple(dict.fromkeys(node.id for node in ast.walk(tree) if isinstance(node, ast.Name)))

    def _parse_callbacks(self, callbacks: list) -> tuple[list[tuple[callable, str, tuple[str]]], list[str]]:
        """Parse the sweep configuration and compute the sweep_routine and vars_order.

        The vars_order is ordered from outer to inner loop (slow to fast change).

        :param callbacks: configuration parameters for the experiment
        :type callbacks: dict
        :return: the sweep_routine (the callbacks to execute with expressions and involved variables)
            and vars_order (the variables to change in order of cost)
        :rtype: tuple[set[tuple[callable, str, tuple[str]]], list[str]]
        """
        sweep_costs = {}
        sweep_routine = []
        self.log.debug(f"{callbacks}")

        # identify the sweeping expressions compute cost and fill the expr_routine dict
        for callback, expr, cost in callbacks:

            # check expression validity and parse it
            tree = self._check_sweep_expression(expr)

            # get the variables from the expression
            variables = self._get_vars(tree)

            # update sweep_cost dict
            for var in variables:
                if var not in sweep_costs:
                    sweep_costs[var] = cost
                else:
                    sweep_costs[var] += cost

            # update sweep_routine
            sweep_routine.append((callback, compile(tree, "compiled_expression", "eval"), variables))

        # order the varibles based on their costs
        vars_order = sorted(sweep_costs, key=lambda x: sweep_costs[x], reverse=True)
        self.log.debug(f"Variable sweep order: {vars_order}")

        return sweep_routine, vars_order

    @staticmethod
    def _compute_variable_values(var: dict[str, dict]) -> dict[str, np.ndarray]:
        """Compute all the values for the variables.

        The field 'mode' must be present and indicates how the values are computed
        - 'lin'   mode: linear spacing -> require fields 'start' (first element), 'stop' (last element),
                                          'num' (number of elements)
        - 'list'  list: list of values -> reuire field 'values' (list of elements)

        :param var: dict mapping variable name with its defintion
        :type var: dict[str: dict]
        :return: dict mapping variable name with its values
        :rtype: dict[str: np.array]
        """
        return_dict = {}

        # iterate through variables
        for var_name, var_description in var.items():
            # get variable "mode" field, check if it exists
            mode = var_description.get("mode")

            # get mode
            if mode == "lin":
                try:
                    return_dict[var_name] = np.linspace(
                        var_description["start"], var_description["stop"], var_description["num"]
                    )

                except KeyError as e:
                    raise KeyError(
                        f"For 'lin' mode the keys 'start', 'stop' and 'num' must be present for '{var_name}' \
                                   variable."
                    ) from e
                except Exception as e:
                    raise TypeError(f"Error during computing sweeping values for '{var_name}' variable: {e}") from e

            elif mode == "list":
                try:
                    return_dict[var_name] = np.array(var_description["values"])

                except KeyError as e:
                    raise KeyError(f"For 'list' mode the key 'values' must be present for '{var_name}' variable") from e
                except Exception as e:
                    raise TypeError(f"Error during computing sweeping values for '{var_name}' variable: {e}") from e

            elif mode is None:
                raise KeyError(f"For the sweeping variable '{var_name}' the 'mode' field is not present")

            else:
                raise ValueError(f"Sweeping spacing '{ var_description['mode'] }' not available")

        return return_dict

    def _nested_loop_recursive(
        self, sweep_routine: list, vars_order: list, depth: int = 0, accounted_callbacks: set = None
    ) -> None:
        """Iterate recursivelly through variables executing callbacks and finally run the experiment.

        :param sweep_routine: callbacks with their compiled expressions and variable dependencies
        :type sweep_routine: list
        :param vars_order: variables ordered by total cost
        :type vars_order: list
        :param depth: index in vars_order of the variable iterated by this call
        :type depth: int
        :param accounted_callbacks: indices of the callbacks already executed by outer loops
        :type accounted_callbacks: set
        :raises SweepAbortedError: if the abort callable returns True before running a sweep point
        """
        if accounted_callbacks is None:
            accounted_callbacks = set()
        # get the name of the variable that is iterating in this loop
        iterating_var = vars_order[depth]
        # set of callback indices that are executing in this loop
        callbacks_executing = set()
        # add callbacks to execute in the loop
        callbacks = []
        for index, (func, compiled, variable_dependencies) in enumerate(sweep_routine):
            # check if the callback has already been accounted for
            if index in accounted_callbacks:
                continue
            # check if the callback must be executed
            if set(variable_dependencies).issubset(vars_order[: depth + 1]):
                # expression to execute
                callbacks.append((func, compiled))
                callbacks_executing.add(index)
        # iterate over the values of the variable
        for value in self.computed_vars[iterating_var]:
            # update the dict_vars with the current value of the variable
            self._current_sweep_point[iterating_var] = value
            # execute the callbacks
            for ll_callback, compiled_eval in callbacks:
                ret = ll_callback(eval(compiled_eval, {"__builtins__": {}}, self._current_sweep_point))
                if ret != 0:
                    raise RuntimeError(
                        f"Sweep callback {ll_callback.__name__} failed with error code {ret} "
                        f"at sweep point {self._current_sweep_point}"
                    )
            # recursively call the function to iterate over the next variable or call the function to run
            # the experiement this is the inner most loop
            if depth == len(vars_order) - 1:
                # checked once per sweep point, right before the run
                if self._should_abort():
                    raise SweepAbortedError("Sweep aborted")
                self._server._run_experiment()
            else:
                self._nested_loop_recursive(
                    sweep_routine=sweep_routine,
                    vars_order=vars_order,
                    depth=depth + 1,
                    accounted_callbacks=accounted_callbacks | callbacks_executing,
                )

    def run(self) -> int:
        """Run the parsed sweep experiment by executing the recursive sweep routine.

        The sweep callbacks and the sweep variables are parsed at construction time.

        :return: The total execution time in ns.
        :rtype: int
        :raises SweepAbortedError: if the sweep is aborted through the abort callable
        """
        # execute the experiment changing the sweeping variables
        start = time.perf_counter_ns()
        self._nested_loop_recursive(
            self.sweep_routine,
            self.vars_order,
        )
        stop = time.perf_counter_ns()
        return stop - start
