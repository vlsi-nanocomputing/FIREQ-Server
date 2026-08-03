# 3. Dynamic System Tree & Sub-System Nodes

The FIREQ Server models the hardware setup as an object tree powered by `anytree`. When the root system node initializes, it inspects the FPGA layout and dynamically builds child nodes representing individual hardware blocks. 

Configurations are sent to these nodes via nested dictionaries using the `apply_configuration()` method.

## The Base Node Interface (`_GenericNode`)

Every hardware component subclasses `_GenericNode`. This class establishes a uniform mechanism for parameter registration and configuration handling:

* **`apply_configuration(configuration: dict)`**: 
  Parses nested configuration dictionaries recursively:
  * **Parameter Keys (`$` prefix)**: Invokes the corresponding parameter callback method bound to that key (e.g., `$duration`).
  * **Sweep Expressions (`#` prefix)**: If a string begins with `#`, the node tags it as a dynamic sweep expression instead of executing it immediately, returning a tuple `(bound_callback, expression, cost)`.
  * **Sub-dictionaries / Lists**: Instantiates or updates child nodes dynamically (such as custom pulse envelopes or trigger delay items).
* **`parameter_callback(key: str, sweepable: bool = False, cost: int = 1)`**: 
  A python decorator used on node methods to expose them to configuration dictionaries, indicating whether the parameter can be swept at runtime and its relative computational cost.

---

## Node Type Specifications & Exposed Callbacks

### `SignalGeneratorNode` (Type: `"signal_generation"`)
Wraps `GeneratorDriver` to synthesize RF control pulses and readout waves.
* **`$dfrequency`** (*float*): Carrier frequency for drive pulse modulation in MHz.
* **`$rfrequency`** (*float*): Carrier frequency for readout pulse modulation in MHz.
* **`$rphase`** (*float*): Initial readout phase offset in radians.
* **`$dchannel`** / **`$rchannel`** (*int*): Enables or disables drive/readout trigger channels (set to `0` to deactivate).
* **`$lfsr_seed`** (*int*): Sets the seed integer for pseudo-random noise generation.
* **`$drive_order`** (*list[str]*): Configures the sequence of programmed pulse names to be played back by the hardware.
* **Child Nodes**: Supports child creation of `_GenericEnvelope` (custom sample arrays), `_Pulse` (wave definition words with gain and duration), and `_VZGate` (virtual Z rotation phase gates).

### `AcquisitionNode` (Type: `"acquisition"`)
Wraps `AcquisitionDriver` for high-speed signal capture and IQ demodulation.
* **`$duration`** (*float*): Demodulation integration window length in nanoseconds.
* **`$output_type`** (*str*): Selects data reduction mode (`"raw"`, `"decimated"`, or `"accumulated"`).
* **`$rfrequency`** (*float*): Demodulation carrier frequency in MHz.
* **`$rphase`** (*float*): Initial phase adjustment for demodulation in radians.
* **`$tof`** (*float*): Time-of-Flight delay offset in nanoseconds before integration starts.
* **`$rchannel`** (*int*): Trigger channel mapping.

### `TriggerGeneratorNode` (Type: `"trigger_generator"`)
Wraps `TriggerGeneratorDriver` to manage master experiment timing and repetitions.
* **`$experiment_duration`** (*float*): Sets total single-shot experiment cycle period in nanoseconds.
* **`start_experiment()`**: Initiates the pulse firing sequence in hardware.
* **`is_done()`** (*bool*): Polls hardware to check if all programmed repetitions have finished.
* **Child Nodes**: Supports child creation of `_DelayItem` to insert precise timing delays between drive and readout triggers.

### `DMANode` (Type: `"dma"`)
Wraps `pynq.lib.DMA` to stream captured sample buffers into ARM host memory.
* **`init_dma()`**: Allocates contiguous RAM memory buffers and starts the AXI-Stream receive channel.
* **`transfer_all(data_queue: Queue)`**: Waits for DMA completion, packages the raw binary buffer into a `DMAPayload`, and pushes it to the outgoing queue.

### `SwitchNode` (Type: `"data_switch"`)
Wraps `AXIStreamSwitchDriver` for multiplexing AXI-Stream channels.
* **`set_master_to_input(slave_index: int)`**: Routes a specific slave stream input to the master output connected to the DMA.

### `FIFONode` (Type: `"acquisition_fifo"`)
Wraps `FIFOWrapper` to monitor hardware memory depth.
* **`update_max_hw_shots()`**: Computes how many full hardware repetitions can fit into the physical FIFO RAM before risking an overflow condition.
