# FIREQ_SYSTEM

High-level, tree-structured model of the FIREQ hardware. Each hardware IP
(acquisition, signal generation, trigger generation, DMA, FIFO, AXI-Stream
switch) is wrapped in a dedicated node class derived from `_GenericNode`. The
root of the tree is `FIREQSystemNode`, which loads the bitfile through
`FIREQ_LL_API`, discovers the peripherals, and orchestrates inter-node
dependencies with a DAG.

The package converts physical units (MHz, ns, radians) into the low-level units
expected by the drivers, manages shared hardware resources (envelope memory,
wave memory, hardware shot counts) and streams acquisition data out of the
DMAs.

## Architecture

```text
FIREQ_SYSTEM/
├── fireq_system_node.py        - FIREQSystemNode: root node, tree + DAG orchestration
├── signal_generator_node.py    - SignalGeneratorNode + envelope/pulse/VZ gate items
├── acquisition_node.py         - AcquisitionNode
├── trigger_generator_node.py   - TriggerGeneratorNode + delay items
├── dma_node.py                 - DMANode + DMAPayload
├── switch_node.py              - SwitchNode
├── fifo_node.py                - FIFONode
├── _generic_node.py            - _GenericNode base, callback registry, RegisterNode
├── _dependency_orchestrator.py - _DependencyOrchestrator: update DAG
├── _utils.py                   - _MutableRef, clock conversion helpers
└── __init__.py                 - public exports
```

## Files

| File | Public API | Responsibility |
|---|---|---|
| `fireq_system_node.py` | `FIREQSystemNode` | Root node. Loads the overlay via `FIREQSoC`, instantiates one node per discovered IP, resolves cross-node dependencies, computes the number of hardware shots per software shot, and runs the experiment loop (trigger generator + DMA streaming). |
| `signal_generator_node.py` | `SignalGeneratorNode`, `_GenericEnvelope`, `_Pulse`, `_VZGate`, `_RectangularEnvelope` | Wraps the generator driver; manages envelope memory allocation and WDW slots; exposes drive/readout frequency, phase and trigger channels; builds the drive order in the memory-mapped FIFO; manual triggering. |
| `acquisition_node.py` | `AcquisitionNode` | Wraps the acquisition driver; sets duration, output mode, demodulation frequency/phase, trigger channel, time of flight; publishes the expected payload for each output interface (used by FIFO/DMA nodes). |
| `trigger_generator_node.py` | `TriggerGeneratorNode`, `_DelayItem` | Wraps the trigger generator driver; sets the experiment duration, the hardware shot count, and the per-delay schedule; starts the experiment and exposes completion status. |
| `dma_node.py` | `DMANode`, `DMAPayload` | Wraps a PYNQ DMA receive channel; allocates the receive buffer, starts transfers, and pushes completed payloads into an outgoing queue as `DMAPayload` objects (or a subclass, e.g. the network payload). Supports feeding from a switch with multiple input interfaces. |
| `switch_node.py` | `SwitchNode` | Wraps the AXI-Stream switch; resolves input/output interfaces, aggregates the input payloads and forwards the selected one to the downstream DMA. |
| `fifo_node.py` | `FIFONode` | Wraps an AXI-Stream FIFO (software object); computes the maximum number of hardware shots that fit in the FIFO and the combined output payload (shots × single-shot payload). |
| `_generic_node.py` | `_GenericNode`, `RegisterNode`, `parameter_callback` | Base class for all nodes (anytree `Node` subclass): configuration dictionary traversal, callback registry, child creation, and auto-registration of node classes to the drivers they wrap. |
| `_dependency_orchestrator.py` | `_DependencyOrchestrator` | Directed graph of update functions; recomputes variables in topological order, visiting a node only when at least one upstream dependency has changed (short-circuiting). |
| `_utils.py` | `_MutableRef`, `_get_periods_from_clock` | Mutable reference wrapper used to share values between nodes with change detection, and ns → clock cycles conversion. |

## Node tree and configuration

The tree is built automatically from the discovered IPs: `FIREQSystemNode`
scans `FIREQSoC.ips` and creates one node for each driver type via the
`wraps` registry (`_driver_wrappers`). Node names are the IP instance names
from the `.hwh` design.

Configuration is applied as nested dictionaries:

- Keys starting with `$` are **parameters** applied to the current node. A
  value that is a string starting with `#` marks the parameter as **sweepable**
  and returns a callback instead of applying it immediately.
- Other keys name **sub-systems**: a `dict` value configures an existing child,
  a `list` of dicts creates multiple children of the same type (each dict must
  contain a `_name` key; other `_`-prefixed keys are constructor metadata, and
  `$`-keys are parameters of the new child).

```python
config = {
    "system": {
        "$shots": 100,                        # parameter on the root node
        "my_generator": {                     # existing child (IP instance)
            "$dfrequency": 200.0,             # MHz, sweepable
            "$rchannel": 1,
            "envelope": [                     # create children of type "envelope"
                {"_name": "gauss", "$samples": np.array([0.0 + 0.0j, 0.5 + 0.5j])},
            ],
            "pulse": [
                {"_name": "x90", "_envelope": "gauss", "$duration": 20.0, "$gain": 0.9},
                {"_name": "readout", "_envelope": "gauss", "_readout": True,
                 "$duration": 100.0, "$gain": 0.5},
            ],
            "$drive_order": ["x90"],
        },
    }
}
```

`FIREQSystemNode.apply_configuration(config)` walks the tree and returns the
list of sweepable callbacks as `(bound_callback, expression, cost)` tuples; it
raises `RuntimeError` if any callback fails (non-zero return code).

### Supported parameters

| Node | Parameter | Unit | Notes |
|---|---|---|---|
| `FIREQSystemNode` | `$shots` | int | Shots per experiment, sweepable |
| `SignalGeneratorNode` | `$dfrequency` | MHz | Drive modulation frequency, sweepable |
| | `$rfrequency` | MHz | Readout modulation frequency, sweepable |
| | `$rphase` | radians | Readout initial phase, sweepable |
| | `$rchannel` / `$dchannel` | int | Readout/drive trigger channel (0 = off) |
| | `$lfsr_seed` | int | LFSR seed, sweepable |
| | `$drive_order` | list[str] | Ordered pulse/VZ gate names |
| | `$tmanual_dest` | str | Manual wave destination (`drive`/`readout`) |
| `_Pulse` | `$duration` | ns | Sweepable |
| | `$gain` | −1…1 | Sweepable |
| `_VZGate` | `$vz_rotation` | frac. of 2π | Sweepable |
| `_GenericEnvelope` | `$samples` | complex array | Normalized to ±1, written to envelope memory |
| `AcquisitionNode` | `$duration` | ns | Sweepable |
| | `$output_type` | str | `raw`, `decimated`, `accumulated` |
| | `$rfrequency` | MHz | Demodulation frequency, sweepable |
| | `$rphase` | radians | Demodulation initial phase, sweepable |
| | `$rchannel` | int | Trigger channel (0 = no external trigger) |
| | `$tof` | ns | Time of flight, sweepable |
| `TriggerGeneratorNode` | `$experiment_duration` | ns | Sweepable |
| `_DelayItem` | `$delay` | ns | Sweepable |

## Experiment flow

`FIREQSystemNode.run_experiment(queue)` performs one experiment of `$shots`
shots:

1. The requested shot count is published to the dependency graph, which
   recomputes `max_hw_shots` (bounded by the acquisition FIFO capacities and
   the trigger generator repetition width) and `hw_shots` =
   `min(max_hw_shots, requested)`.
2. While shots remain, the software loop: initializes every DMA receive
   channel (`init_dma`), starts the trigger generator (`start_experiment`),
   snapshots the DMA parameters (`save_variables`), and streams the received
   buffers into the provided queue (`transfer_all`). If fewer shots remain
   than `hw_shots`, the shot count is reduced and the DAG re-updated before
   draining.
3. Each drained chunk is a `DMAPayload` (source name, shots, dtype format,
   raw bytes) — the queue must be drained by the caller (the server's send
   worker does this).

The payload classes are pluggable: `set_dma_payload_interface_class` replaces
the class used to wrap DMA data, which is how `FIREQ_SERVER` injects its
network-serializable payload.

## Dependency DAG

Nodes publish **references** (`_MutableRef`) to the root, and **update
functions** that recompute derived values. `add_dependency(label, depends_on)`
records that a value depends on others; on `update()`, the orchestrator runs
all update functions in topological order, skipping downstream nodes unless an
upstream value actually changed (change detected via `hash_and_compare`).

This is what keeps derived hardware state consistent, e.g.:

- `AcquisitionNode` publishes its single-shot payload per output interface;
- `FIFONode` derives `max_hw_shots` (FIFO size ÷ payload) and the combined
  output payload (payload × `hw_shots`);
- the root derives `hw_shots` from the requested shots and the FIFO limits,
  and `TriggerGeneratorNode` writes that value into the hardware.

## Related documentation

- [`FIREQ_LL_API/README.md`](../FIREQ_LL_API/README.md) — the low-level drivers
  wrapped by these nodes.
- [`FIREQ_SERVER/README.md`](../FIREQ_SERVER/README.md) — the TCP server built
  on top of `FIREQSystemNode`.
- [`FIREQ_SERVER/execution/README.md`](../FIREQ_SERVER/execution/README.md) —
  how sweepable callbacks are driven by `SweepExperiment`.
