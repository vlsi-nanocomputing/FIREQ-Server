# 5. Sweep Experiment Engine

Multi-dimensional parameter sweeps (e.g., sweeping drive frequency vs. pulse duration) are handled by the `SweepExperiment` class. 

Rather than iterating through parameters naively, the engine parses expressions and orders loops by computational cost to maximize performance.

## Public Class: `SweepExperiment(server, queue_out, logger=None)`

* **`run(sweep_callbacks: list, variables: dict) -> int`**
  Executes a multi-point sweep. It computes parameter arrays, orders variables based on update costs, streams a header packet to the client, and executes a recursive nested loop to run the experiment at every point. Returns total execution time in nanoseconds.

## Parameter Sweep Mechanics

When defining sweep variables, parameters can be generated using three modes:
* **Linear Spacing (`mode: "lin"`)**: Generates an array from `start` to `stop` with `num` points via `np.linspace`.
* **Constant (`mode: "const"`)**: Assigns a single fixed scalar `value`.
* **Explicit List (`mode: "list"`)**: Uses a user-provided list of explicit numerical `values`.

### Execution Cost Optimization
Each parameter callback declares an execution `cost` (e.g., writing waveform sample memory has a cost of `1000`, while updating a phase register has a cost of `1`). The sweep engine sorts variables so that high-cost callbacks are evaluated in outer loops, while cheap callbacks run in inner loops, dramatically reducing overall sweep execution time.
