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
| `sweep_experiment.py` | `SweepExperiment` | Validates sweep expressions, computes variable value arrays (`lin` / `const` / `list` modes), orders variables by total callback cost, and executes the sweep as nested loops that evaluate the expressions and call the hardware callbacks at each point. |

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
| `const` | `value` | Single value array |
| `list` | `values` | Array of the given values |

```python
variables = {
    "gain":  {"mode": "lin", "start": 0.1, "stop": 0.9, "num": 5},
    "phase": {"mode": "const", "value": 0.5},
    "names": {"mode": "list", "values": ["a", "b", "c"]},
}
```

Missing or invalid fields raise `KeyError` / `TypeError` / `ValueError`
depending on the failure.

## Execution

`run(sweep_callbacks, variables)`:

1. Computes the value array of every variable (`_compute_variable_values`).
2. Parses and validates the callbacks into `(callback, compiled_expression,
   variables)` tuples and sorts the variable iteration order by total cost
   (`_parse_callbacks`).
3. Emits a `sweep_experiment_header` message
   (`{"type": "sweep_experiment_header", "variables_order": [...]}`) on the
   output queue, so the client knows in which order the variables iterate.
4. Runs a recursive nested loop (`_nested_loop_recursive`): at each level the
   callbacks whose variable dependencies are fully covered by the current loop
   depth are evaluated (with `eval(compiled, {}, current_point)`), and at the
   innermost level the server's experiment runner (`FIREQServer._run_experiment`)
   is invoked, which streams DMA payloads to the client.
5. Returns the total wall-clock time in ns.

The caller (the server) appends a final `status` message `"sweep ended"` with
the returned time.

## Related documentation

- [`../README.md`](../README.md) — `config_and_run` and the command table.
- [`../../FIREQ_SYSTEM/README.md`](../../FIREQ_SYSTEM/README.md) — where
  sweepable callbacks and their costs come from.
