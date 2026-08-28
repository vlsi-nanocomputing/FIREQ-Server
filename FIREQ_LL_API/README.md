# FIREQ_LL_API

Low-level PYNQ drivers for the FIREQ FPGA IPs. This package sits directly on
top of PYNQ and the Xilinx RF data converters: it loads the overlay, parses the
hardware description, and exposes register/memory-level control of every FIREQ
IP in the design.

All drivers in this collection work with **low-level units**: clock cycles,
number of samples, frequencies normalized to the sampling rate, and phases
normalized to `2π`. Physical units (MHz, ns, radians) are handled one layer up
in `FIREQ_SYSTEM`.

## Architecture

```text
FIREQ_LL_API/
├── fireq_soc.py                 - FIREQSoC: overlay loading + IP discovery
├── _fireq_parser.py             - FireqParser: .hwh parsing + connectivity graphs
├── generator_driver.py          - GeneratorDriver (axisGeneratorIP)
├── acquisition_driver.py        - AcquisitionDriver (axisAcquisitionIP)
├── trigger_generator_driver.py  - TriggerGeneratorDriver (axisTriggerGeneratorIP)
├── axi_stream_switch_driver.py  - AXIStreamSwitchDriver (axis_switch)
├── fifo_wrapper.py              - FIFOWrapper (axis_data_fifo)
├── _utils.py                    - _FIREQDriver base, MMIO debug wrapper, bit helpers
└── __init__.py                  - public exports
```

## Files

| File | Public API | Responsibility |
|---|---|---|
| `fireq_soc.py` | `FIREQSoC`, `load_fireq` | Loads the `.bit` overlay, initialises the LMK/LMX RF clocks, runs `FireqParser` on the `.hwh`, binds the AXI Full/Lite interfaces of the FIREQ drivers, discovers all IPs (FIREQ, RF-DC, MTS groups, clocks), and validates the design (required IPs present, consistent sample rates). |
| `_fireq_parser.py` | `FireqParser` | Parses the `.hwh` file into `networkx` graphs (`system_graph`, `control_graph` AXI4, `dataflow_graph` AXI-Stream, `clock_graph`), collapses pass-through modules (width/clock converters, register slices), and provides the AXI address mapping, module parameters, and interface-to-bus-id maps. Supports the `rf4x2` and `zcu216` boards. |
| `generator_driver.py` | `GeneratorDriver` | Controls the generator IP: envelope memory (per-channel and common), wave memory (WDWs), memory-mapped FIFO (drive sequence), readout wave, drive/readout modulation frequency and initial phase, trigger channel mask, LFSR seed, manual trigger, and the WDW builder (`build_pulse_wdw`, `build_vz_wdw`, `build_envelope_specific_wdw`). |
| `acquisition_driver.py` | `AcquisitionDriver` | Controls the acquisition IP: acquisition duration, output mode (`raw` / `decimated` / `accumulated`), demodulation frequency and initial phase, trigger channel, time of flight, and payload size/format calculation for a single shot. |
| `trigger_generator_driver.py` | `TriggerGeneratorDriver` | Programs the experiment timeline: experiment duration, number of hardware shots, and the per-channel delay schedule (`insert_delay`); starts the experiment and reports completion (`is_done`). |
| `axi_stream_switch_driver.py` | `AXIStreamSwitchDriver` | Routes the AXI-Stream master to one of the switch inputs (`switch_to_input`). Only one master is supported. |
| `fifo_wrapper.py` | `FIFOWrapper` | Wraps an `axis_data_fifo` and exposes its size in bytes. FIFOs are software-only objects: they are used to track packet sizes, so they are not represented by a PYNQ driver class. |
| `_utils.py` | `_FIREQDriver`, `_DebugMMIO`, `_get_bit(s)`, `_set_bit(s)`, `_compute_pinc_poff` | Shared base driver (AXI interface initialisation, debug level), a transaction-logging MMIO wrapper, register bit helpers, and DDS phase increment/offset computation including Nyquist-zone handling. |

## FIREQSoC

`FIREQSoC` (a PYNQ `Overlay` subclass) is the entry point of the package:

```python
from FIREQ_LL_API import FIREQSoC, load_fireq

soc = load_fireq("overlay.bit")          # loads bitfile, init clocks + IPs
# or, with version check and clock init control:
soc = FIREQSoC("overlay.bit", ignore_version=False, init_clocks=True)
```

On construction it:

1. Resets the PL server (`PL.reset()`), which clears bugged PYNQ caches.
2. Loads the overlay and instantiates `FireqParser` on the matching `.hwh`.
3. Initialises the RF clocks (LMK `245.76` MHz, LMX `491.52` MHz).
4. Binds the AXI interfaces of the FIREQ drivers from the `.hwh` address map —
   the generator IPs have two AXI4 interfaces (one Full, one Lite) and PYNQ only
   binds one, so the mapping is applied manually.
5. Discovers every IP in the design (`soc.ips`) and validates that the design
   contains at least one generator, one acquisition and exactly one trigger
   generator.
6. Discovers the RF-DC (`soc.rfdc`) with its active ADC/DAC blocks, the MTS
   (multi-tile synchronisation) groups and masters, and the fabric/DAC/ADC clock
   frequencies (in MHz). If no RF-DC is present a debug overlay is assumed.
7. Freezes the calibration of all active ADCs.

Useful helpers:

- `soc.reset_all_ip_memory_and_registers()` — zeroes all registers/memories of
  every FIREQ driver in the design.
- `soc.syncronize_mts_groups()` — activates multi-tile synchronisation for the
  DACs.
- `soc.set_nyquist_zone(tile, block_id, zone)` — selects the Nyquist zone of a
  DAC/ADC block.
- `soc.set_adc_autocalibration_status(adc_index, freeze)` — freezes/unfreezes
  ADC calibration.
- `soc.get_ip_frequency(full_ip_name, clock_port)` — fabric frequency of an IP
  from the clock graph.
- `soc.set_logger(logger)` — replaces the logger used by the SoC and, via the
  shared `set_logger` convention, by the drivers and the parser.

## Driver conventions

- Every `_FIREQDriver` exposes `init_axi_lite_interface` and
  `init_axi_full_interface`; the generator also splits its AXI Full segment into
  the envelope memory, the common envelope memory, the wave memory and the
  memory-mapped FIFO.
- Mutating methods return an integer error code (`0` on success, `-3` on
  out-of-range input) rather than raising.
- `reset_memory_and_registers()` zeroes all registers and memories of the IP.
- Frequencies are passed normalized to the sampling rate (fraction of `fs`) and
  phases as fractions of `2π`; use `_compute_pinc_poff` (in `_utils`) to obtain
  the raw phase increment/offset for a physical frequency, including Nyquist
  zone handling.
- Wave definitions (WDWs) are 128-bit words packed by the `build_*` methods of
  `GeneratorDriver` (pulse, VZ gate, envelope flags) and stored either in the
  wave memory or in the readout wave registers.
- `set_debug_level` on `_FIREQDriver` swaps the MMIO objects with `_DebugMMIO`
  wrappers that log every AXI transaction to a file (level `1`), or restores
  the originals (level `0`).

## Usage notes

- The package must run inside a PYNQ 3.x environment on the target board
  (`pynq`, `xrfdc`, `xrfclk`); the RF clock initialisation writes to board
  hardware and requires appropriate permissions.
- `FIREQSoC` raises `RuntimeError` when the overlay cannot be created or the
  design is missing required FIREQ IPs, and `NotImplementedError` when the
  fabric frequencies are not uniform or more than one trigger generator is
  present.

## Related documentation

- [`FIREQ_SYSTEM/README.md`](../FIREQ_SYSTEM/README.md) — high-level nodes that
  wrap these drivers.
- [`FIREQ_SERVER/README.md`](../FIREQ_SERVER/README.md) — TCP server that uses
  the system node on top of this package.
