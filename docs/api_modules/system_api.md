# 2. Top-Level System API

The `FIREQSystemNode` class serves as the core manager of the server state. Inheriting from `_GenericNode`, it acts as the root of the system tree, holding references to all hardware IP wrappers, frequency parameters, and memory boundaries.

## Core Interface Details

* **`FIREQSystemNode(bitfile_name: str)`**
  Loads the target FPGA bitstream using `FIREQSoC`, automatically discovers hardware peripherals (Generators, Acquisitions, DMA, Switches, FIFOs), creates their corresponding wrapper nodes, and initializes the DAG dependency orchestrator.

* **`run_experiment(queue: Queue) -> None`**
  Executes a single experiment run. It evaluates system-wide dependencies, calculates hardware shot limits to prevent buffer overflows, programs the hardware trigger generator, and continuously retrieves DMA buffers, pushing captured `DMAPayload` objects into the outgoing network queue.

* **`reset_all() -> None`**
  Resets all memory-mapped registers across all discovered IPs, clears hardware buffer references, and resets the node tree hash states to factory defaults.

* **`get_reference(ref_name: str)` / `add_reference(ref_name: str, ref: object)`**
  Provides a system-wide lookup dictionary (`_references`) for shared mutable objects (`_MutableRef`), such as calculated FIFO limits or output payload specifications.

* **`register_update_function(func_label: str, update_function: callable)`**
  Registers an internal parameter computation function in the dependency graph under a unique path-based string label.

* **`add_dependency(func_label: str, depends_on: str | list[str])`**
  Defines an explicit execution dependency where `func_label` must be re-evaluated whenever one or more `depends_on` labels change state.

* **System Clock Query Methods**:
  * `get_fabric_frequency()`: Returns the main FPGA fabric clock frequency in MHz.
  * `get_generation_sampling_frequency()`: Returns the DAC sample clock frequency in MHz.
  * `get_acqisition_sampling_frequency()`: Returns the ADC sample clock frequency in MHz.
