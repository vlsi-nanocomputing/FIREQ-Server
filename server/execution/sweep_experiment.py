import re
import numpy as np


class SweepExperiment:
    """Execute multi-sweep points using high-level callbacks
    
        :param OPERATORS: string containings the operator characters available
        :type OPERATORS: str
    """

    OPERATORS = "+-" # TODO: check if is necessary to constrain the operators

    # def __init__(self, sweep_expr: list[tuple[callable, str, int]], var: list[dict[str: dict]]) -> None:
    def __init__(self, node: object, queue: object) -> None:
        """
        Initialize with the sweeping expressions and variables values.

        :param node:
        :type node:
        :param queue:
        :type queue:
        """
        # Fireq Node
        self.node = node
        # Output queue
        self.queue = queue

        # dict with computed variables
        self.computed_vars = {}

        # dict to store variables and their corresponding sweep costs
        # self.sweep_costs = {}
        # list to store the order of variables based on their costs
        # self.vars_order = []        
        # set to store the expressions that must be apply with this format: [callback, expression, dependency variables]
        # self.sweep_routine = set()
    
    def _check_sweep_expressions(self, expr: str) -> None:
        """Check the sweep expressions for validity.

        Raise ValueError if the expression does not pass the check.
        The first character of the expression must be '#' and the rest of the expression must contain only valid operators.

        :param expr: expression to check
        :type expr: str
        """
        if not expr.startswith("#"):
            raise ValueError(f"Invalid sweep expression: {expr}. Must start with '#'.")
        
        # check if the expression contains only valid operators (TODO: can be removed and method can be static)
        for char in expr[1:]:
            if not char.isalnum() and char != '_' and char != ' ' and char != '(' and char != ')':
                # check only special char
                if char not in self.OPERATORS:
                    raise ValueError(f"Invalid operator used for the expression: '{char}'")
    
    @staticmethod
    def _get_vars(expr: str) -> tuple[str]:
        """Extract the involved varibles from expression

        :param expr: expression
        :type str
        :return tuple with the involved variables
        :rtype tuple[str]
        """
        return tuple(re.findall(r'[a-zA-Z_]\w*', expr))

    def _parse_config(self, config) -> None:
        """Parse the sweep configuration and fill sweep_cost dict and sweep_routine set.

        
        """
        sweep_costs = {}
        sweep_routine = set()

        sweep_expr = self.node.apply_configuration(config)

        # identify the sweeping expressions and fill ...
        for callback, expr, cost in sweep_expr:

            # check expression validity
            self._check_sweep_expressions(expr)

            # get the variables from the expression
            varibles = self._get_vars(expr[1:])  # remove the '#' character and get the variables

            # update sweep_cost dict
            for var in varibles:
                if var not in sweep_costs:
                    sweep_costs[var] = cost
                else:
                    sweep_costs[var] += cost
            
            # update sweep_routine
            sweep_routine.add((callback, expr[1:], varibles))
        
        # order the varibles based on their costs
        vars_order = sorted(sweep_costs, key=lambda x: sweep_costs[x], reverse=True)

        return sweep_routine, vars_order
    
    @staticmethod
    def _compute_variable_values(var: dict[str: dict]) -> dict[str: np.array]:
        """Compute all the values for the variables

        The field 'mode' must be present and indicates how the values are computed
        - 'lin'   mode: linear spacing -> require fields 'start' (first element), 'stop' (last element), 'num' (number of elements) 
        - 'const' mode: constant value -> require field 'value' (constant and unique value)
        - 'list'  list: list of values -> reuire field 'values' (list of elements)

        :param var: dict with variable and correspondent description
        :type var: dict[str: dict]
        :return dict with sweeping values
        :rtype: dict[str: np.array]
        """
        return_dict = {}

        # iterate through variables
        for v, desc in var.items():
            # check if is present the field space
            if "mode" not in desc:
                raise KeyError(f"For the sweeping variable '{v}' the 'mode' field is not present")
            
            # get mode
            if desc["mode"] == 'lin':
                try:
                    return_dict[v] = np.linspace(desc["start"], desc["stop"], desc["num"])
                
                except KeyError:
                    raise KeyError(f"For 'lin' mode the keys 'start', 'stop' and 'num' must be present for '{v}' variable")
                except:
                    raise TypeError(f"Error during computing sweeping values for '{v}' variable")
            
            elif desc["mode"] == 'const':
                try:
                    return_dict[v] = np.array([desc["value"]])
                
                except KeyError:
                    raise KeyError(f"For 'const' mode the key 'value' must be present for '{v}' variable")
                except:
                    raise TypeError(f"Error during computing sweeping values for '{v}' variable")
            
            elif desc["mode"] == 'list':
                try:
                    return_dict[v] = np.array(desc["values"])
                
                except KeyError:
                    raise KeyError(f"For 'list' mode the key 'values' must be present for '{v}' variable")
                except:
                    raise TypeError(f"Error during computing sweeping values for '{v}' variable")
            
            else:
                raise ValueError(f"Sweeping spacing '{desc["mode"]}' not available")

        return return_dict

    def _nested_loop_recursive(self, dict_vars: dict, sweep_routine: set, vars_order: list, iterating_vars) -> None:
        """Iterate recursivelly through variables executing callbacks and finally run the experiment.

        :param dict_vars: dict with the value associated to each varible for the current step
        :type dict_vars: dict
        :param sweep_routine: sweep_routine dict for the current step
        :type sweep_routine: set
        :param vars_order
        :type vars_order
        :param iterating_vars: list of iterating variables for the current step
        :type iterating_vars: list
        """
        if not vars_order:
            # if variables are empty, run the experiment
            self.node.run_experiment(self.queue)
        
        else:
            # pop the first variable and get values of iteration
            var = vars_order[0]
            var_values = self.computed_vars[var]
            del vars_order[0]

            # add the variable to the iterating_vars set
            iterating_vars.add(var)

            # add callbacks to execute in the loop
            callbacks = []
            remaining_routine = set()
            for f, expr, dep_vars in sweep_routine:
                # check if the callback must be executed 
                if set(dep_vars).issubset(iterating_vars):
                    # expression to execute
                    callbacks.append((f, expr))
                else:
                    # expression to not execute
                    remaining_routine.add((f, expr, dep_vars))

            sweep_routine = remaining_routine

            # iterate over the values of the variable
            for value in var_values:
                # update the dict_vars with the current value of the variable
                dict_vars[var] = value

                # execute the callbacks
                for callback in callbacks:
                    callback[0](eval(callback[1], {}, dict_vars))
                
                # recursively call the function to iterate over the next variable
                self._nested_loop_recursive(dict_vars=dict_vars, sweep_routine=sweep_routine.copy(), vars_order=vars_order.copy(), iterating_vars=iterating_vars.copy())

    def run(self, config, var):
        """Run the sweep experiment by parsing the configuration and executing the recursive sweep routine.
        
        :param sweep_expr: A list of tuples containing the sweep expressions.
        :type sweep_expr: list[tuple[callable, str, int]]
        :param var: A list of dictionaries containing the variable parameters for the sweep.
        :type var: list[dict[str: dict]]
        """
        # compute variables values
        self.computed_vars = self._compute_variable_values(var)

        # parse the configuration
        sweep_routine, vars_order = self._parse_config(config)
        
        # execute the experiment changing the sweeping variables
        self._nested_loop_recursive(dict_vars={}, sweep_routine=sweep_routine.copy(), vars_order=vars_order.copy(), iterating_vars=set())