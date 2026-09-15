# FIREQ_SERVER.execution

Experiment execution components for the FIREQ server. Currently contains only
the sweep orchestration: `SweepExperiment` turns sweepable callbacks (produced
by `FIREQSystemNode.apply_configuration`) plus a variables specification into
a multi-point experiment run.

## Architecture

```text
FIREQ_SERVER/execution/
├── sweep_experiment.py  - SweepExperiment: parsing + nested-loop execution
└── __init__.py          - public exports
```

## Files

| File | Public API | Responsibility |
|---|---|---|
| `sweep_experiment.py` | `SweepExperiment` | Validates sweep expressions, computes variable value arrays (`lin` / `list` modes), orders variables by total callback cost, and executes the sweep as nested loops that evaluate the expressions and call the hardware callbacks at each point. |

## Sweepable expressions

A parameter is sweepable when its configuration value is a string starting
with `#`, e.g. `"$gain": "#gain"` (this is enforced by the system node
callback registry). The rest of the expression may reference sweep variables
and use arithmetic with the `+ -` operators, parentheses and alphanumeric /
underscore names:

```python
"$duration": "#base_duration * (1 + 0.1 * (sweep_index))"
```

`SweepExperiment._parse_callbacks` compiles each expression (`eval`-style) and
extracts the involved variables with a regex. The **cost** of each callback
(declared in the `parameter_callback` decorator) is accumulated per variable,
and variables are iterated from the highest total cost to the lowest, so that
the most expensive hardware updates happen in the outermost loop.

## Variable specification

The `variables` object maps each variable name to a description dict with a
`mode` field:

| Mode | Required fields | Result |
|---|---|---|
| `lin` | `start`, `stop`, `num` | `np.linspace(start, stop, num)` |
| `list` | `values` | Array of the given values |

```python
variables = {
    "gain":  {"mode": "lin", "start": 0.1, "stop": 0.9, "num": 5},
    "phase": {"mode": "list", "values": [0.5]},
    "names": {"mode": "list", "values": ["a", "b", "c"]},
}
```

Missing or invalid fields raise `KeyError` / `TypeError` / `ValueError`
depending on the failure.

## Execution

`SweepExperiment(server, sweep_callbacks, variables)` does all the parsing up
front, so a fully prepared experiment is available as soon as the object is
constructed:

1. Computes the value array of every variable (`_compute_variable_values`) into
   `computed_vars`.
2. Parses and validates the callbacks into `(callback, compiled_expression,
   variables)` tuples and sorts the variable iteration order by total cost
   (`_parse_callbacks`), exposing `sweep_routine` and `vars_order` (ordered from
   the outermost to the innermost loop).

`run()` then takes no arguments and returns the total wall-clock time in ns:

1. Runs a recursive nested loop (`_nested_loop_recursive`): at each level the
   callbacks whose variable dependencies are fully covered by the current loop
   depth are evaluated (with `eval(compiled, {}, current_point)`), and at the
   innermost level the server's experiment runner (`FIREQServer._run_experiment`)
   is invoked, which streams DMA payloads to the client.
2. Returns the elapsed time.

The sweep never writes to the network itself: the enclosing
`FIREQServer._config_and_run` emits the `experiment_header` before `run()` and
the `experiment_footer` (carrying the returned time as `sweep_time`) after it.
Each iteration in between ends with an `iteration_ended` status emitted by
`FIREQServer._run_experiment`.

## Related documentation

- [`../README.md`](../README.md) — `config_and_run` and the command table.
- [`../../FIREQ_SYSTEM/README.md`](../../FIREQ_SYSTEM/README.md) — where
  sweepable callbacks and their costs come from.
