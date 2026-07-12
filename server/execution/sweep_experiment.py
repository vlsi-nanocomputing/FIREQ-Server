"""Sweep experiment module.

This module implements the SweepExperiment class, which is responsible for executing multi-point sweep experiments on
the FIREQ system. It provides methods to parse sweep configurations, compute variable values, and execute the experiment
"""

import logging
import re
import copy
from queue import Queue

import numpy as np
from ..network import FIREQNetworkPacket
import time


class SweepExperiment:
    """Execute multi-sweep points using high-level callbacks.

    :param OPERATORS: string containings the operator characters available
    :type OPERATORS: str
    """

    OPERATORS = "+-"  # TODO: check if is necessary to constrain the operators

    def __init__(self, server, queue_out: Queue, logger: logging.Logger | None = None) -> None:
        """
        Initialize with the sweeping expressions and variables values.

        :param node: object representing the FIREQ system
        :type node: FIREQSystemNode
        :param queue: queue istance
        :type queue: Queue
        """
        # Fireq Node
        self._server = server
        self._queue_out = queue_out
        self.log = logger or logging.getLogger(__name__)

        # dict with all the computed sweeping variables
        self.computed_vars = {}
        self._current_sweep_point = {}

    def _check_sweep_expression(self, expr: str) -> None:
        """Check the sweep expressions for validity.

        Raise ValueError if the expression does not pass the check.
        The first character of the expression must be '#' and the rest of the expression must
        contain only valid operators.

        :param expr: expression to check
        :type expr: str
        """
        if not expr.startswith("#"):
            raise ValueError(f"Invalid sweep expression: {expr}. Must start with '#'.")

        # check if the expression contains only valid operators (TODO: can be removed and method can be static)
        for char in expr[1:]:
            if not char.isalnum() and char not in {"_", " ", "(", ")"}:
                # check only special char
                if char not in self.OPERATORS:
                    raise ValueError(f"Invalid operator used for the expression: '{char}'")

    @staticmethod
    def _get_vars(expr: str) -> tuple[str]:
        """Extract the involved varibles from expression.

        The expression shuold not cointain the first char '#'

        :param expr: expression
        :type str
        :return tuple with the involved variables
        :rtype tuple[str]
        """
        return tuple(re.findall(r"[a-zA-Z_]\w*", expr))

    def _parse_callbacks(self, callbacks: list) -> tuple[set[tuple[callable, str, tuple[str]]], list[str]]:
        """Parse the sweep configuration and fill sweep_cost dict and sweep_routine set.

        :param callbacks: configuration parameters for the experiment
        :type callbacks: dict
        :return: the sweep_routine (the callbacks to execute with expressions and involved variables)
            and vars_order (the variables to change in order of cost)
        :rtype: tuple[set[tuple[callable, str, tuple[str]]], list[str]]
        """
        sweep_costs = {}
        sweep_routine = set()
        self.log.debug(f"{callbacks}")

        # identify the sweeping expressions compute cost and fill the expr_routine dict
        for callback, expr, cost in callbacks:

            # check expression validity
            self._check_sweep_expression(expr)

            # get the variables from the expression
            variables = self._get_vars(expr[1:])  # remove the '#' character and get the variables

            # update sweep_cost dict
            for var in variables:
                if var not in sweep_costs:
                    sweep_costs[var] = cost
                else:
                    sweep_costs[var] += cost

            # update sweep_routine
            sweep_routine.add((callback, compile(expr[1:], "compiled_expression", "eval"), variables))

        # order the varibles based on their costs
        vars_order = sorted(sweep_costs, key=lambda x: sweep_costs[x], reverse=True)
        self.log.debug(f"Variable sweep order: {vars_order}")

        return sweep_routine, vars_order

    def _put_sweep_info_in_network(self, vars_order: list[str]) -> None:
        """Put the header on the queue with the variables order.

        :param vars_order: list of variables ordered by total cost
        :type vars_order: list[str]
        """
        self._queue_out.put(
            FIREQNetworkPacket({"type": "sweep_experiment_header", "variables_order": copy.deepcopy(vars_order)})
        )

    @staticmethod
    def _compute_variable_values(var: dict[str, dict]) -> dict[str : np.array]:
        """Compute all the values for the variables.

        The field 'mode' must be present and indicates how the values are computed
        - 'lin'   mode: linear spacing -> require fields 'start' (first element), 'stop' (last element),
                                          'num' (number of elements)
        - 'const' mode: constant value -> require field 'value' (constant and unique value)
        - 'list'  list: list of values -> reuire field 'values' (list of elements)

        :param var: dict with variable and correspondent description
        :type var: dict[str: dict]
        :return: dict with sweeping values
        :rtype: dict[str: np.array]
        """
        return_dict = {}

        # iterate through variables
        for var_name, var_description in var.items():
            # check that the variable is not in the dict already
            if return_dict.get(var_name) is not None:
                raise KeyError(f"Variable '{var_name}' has already been defined earlier.")

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

            elif mode == "const":
                try:
                    return_dict[var_name] = np.array([var_description["value"]])

                except KeyError as e:
                    raise KeyError(f"For 'const' mode the key 'value' must be present for '{var_name}' variable") from e
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
        self, sweep_routine: set, vars_order: list, depth: int = 0, accounted_callbacks: set = None
    ) -> None:
        """Iterate recursivelly through variables executing callbacks and finally run the experiment.

        :param dict_vars: dict with the value associated to each varible for the current step
        :type dict_vars: dict
        :param sweep_routine: sweep_routine dict for the current step
        :type sweep_routine: set
        :param vars_order: variables ordered by total cost
        :type vars_order: list
        :param iterating_vars: list of iterating variables for the current step
        :type iterating_vars: list
        """
        if accounted_callbacks is None:
            accounted_callbacks = set()
        # pop the first variable and get values of iteration
        iterating_var = vars_order[depth]
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
        var_values = self.computed_vars[iterating_var]
        for value in var_values:
            # update the dict_vars with the current value of the variable
            self._current_sweep_point[iterating_var] = value
            # execute the callbacks
            for callback in callbacks:
                callback[0](eval(callback[1], {}, self._current_sweep_point))
                # self.logger.debug(f"Parameter change -> callback:'{callback[0].__name__}', params: '{callback[1]}', \
                #                 variables: { {k: v.item() for k, v in dict_vars.items()} }")
            # recursively call the function to iterate over the next variable
            if depth == len(vars_order) - 1:
                self._server._run_experiment()
            else:
                self._nested_loop_recursive(
                    sweep_routine=sweep_routine,
                    vars_order=vars_order,
                    depth=depth + 1,
                    accounted_callbacks=sum(accounted_callbacks, callbacks_executing),
                )

    def run(self, sweep_callbacks: list, variables: list) -> int:
        """Run the sweep experiment by parsing the configuration and executing the recursive sweep routine.

        Sweep callbacks is a list of tuples: (callback func, expression, callback cost)

        :param sweep_callbacks: A list of tuples containing the sweep expressions.
        :type sweep_callbacks: list[tuple[callable, str, int]]
        :param variables: A list of dictionaries containing the variable parameters for the sweep.
        :type variables: dict[str: dict]
        """
        # compute variables values
        self.computed_vars = self._compute_variable_values(variables)

        # parse the configuration
        sweep_routine, vars_order = self._parse_callbacks(sweep_callbacks)

        # create and the header on the queue
        self._put_sweep_info_in_network(vars_order)

        # execute the experiment changing the sweeping variables
        start = time.perf_counter_ns()
        self._nested_loop_recursive(
            sweep_routine,
            vars_order,
        )
        stop = time.perf_counter_ns()
        return stop - start
