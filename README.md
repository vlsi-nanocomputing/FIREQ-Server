# FIREQ Utils

FIREQ control stack for FPGA experiments: low-level PYNQ drivers
(`FIREQ_LL_API`), the TCP server/runtime package (`server`), protocol
specifications (`specifics`), and tests.

## Architecture

```text
fireq-utils/
├── API.py              - interactive server entry point
├── server/             - TCP server, execution orchestration, hardware adapter layer
├── FIREQ_LL_API/       - low-level FIREQ SoC/IP drivers
├── specifics/          - plain-text protocol specifications (.txt)
├── test/               - characterization and integration tests
├── pyproject.toml      - project metadata, dependencies, lint/tool config
└── requirements.txt    - pinned install requirements
```

## Main Components

| Folder / File | Public API | Responsibility |
|---|---|---|
| [`API.py`](API.py) | `main()` | Prompts for overlay/server parameters, loads `FIREQSoC`, instantiates `OverlayAdapter` and `MessageHandler`, then starts `FIREQServer`. |
| [`server/`](server/README.md) | `FIREQServer`, `MessageHandler`, `OverlayAdapter`, `DMAEngine`, shared models/exceptions | Runtime package implementing the network protocol, command execution pipeline, sweep streaming, and server-facing hardware abstraction. |
| [`FIREQ_LL_API/`](FIREQ_LL_API/__init__.py) | `FIREQSoC`, `load_fireq`, `GeneratorDriver`, `AcquisitionDriver`, `TriggerGeneratorDriver` | Low-level drivers that bind to overlay IPs, expose AXI register/memory control, and build hardware capability metadata from the `.hwh` design. |
| [`specifics/`](specifics/) | `.txt` protocol docs | External protocol documentation describing request/response payloads and streaming formats used by the server. |
| [`test/`](test/) | `pytest` suites | Characterization and integration tests covering execution flows, network behavior, and overlay-adapter behavior. |

## Server Package

The `server` package is organized by responsibility:

- `server/network`: framed TCP transport, session/auth handling, command dispatch, sender/receiver threads.
- `server/execution`: high-level command orchestration for single runs and sweeps.
- `server/hardware`: server-facing `OverlayAdapter` and DMA engine abstractions over the low-level drivers.
- `server/models`: shared TypedDicts, queue payloads, result dataclasses, and exception hierarchy.

Within `server/execution`, the post-refactor structure is intentionally split:

- `message_handler.py`: thin public facade used by the network layer.
- `hardware_config.py`: full hardware configuration sequencing.
- `streaming.py`: acquisition stream parameter resolution and chunk emission.
- `sweep_planning.py`: sweep point generation and change-flag computation.
- `sweep_updates.py`: fast-path per-point delta updates.
- `sweep_runner.py`: sweep lifecycle orchestration.

See [`server/README.md`](server/README.md) for the package-level breakdown.

## FIREQ_LL_API Package

`FIREQ_LL_API` is the low-level hardware-control package on top of PYNQ.

### Architecture

```text
FIREQ_LL_API/
├── fireq_soc.py                 - overlay loading, IP discovery, hw_specs build
├── generator_driver.py          - generator IP control (envelopes, waves, modulation, trigger channel)
├── acquisition_driver.py        - acquisition IP control (timing, demodulation, output mode)
├── trigger_generator_driver.py  - trigger/timing FIFO programming and shot control
├── _fireq_parser.py             - .hwh parser for connectivity and AXI mapping
├── _utils.py                    - shared driver/MMIO helpers and bit utilities
└── __init__.py                  - public low-level exports
```

### Files

| File | Public API | Responsibility |
|---|---|---|
| `fireq_soc.py` | `FIREQSoC`, `load_fireq` | Loads the bitstream, initializes clocks, discovers FIREQ IPs, maps RF topology, and computes structured `hw_specs` metadata. |
| `generator_driver.py` | `GeneratorDriver` | Direct generator IP control: envelope memory handling, wave memory/sequencing, modulation, and trigger listener configuration. |
| `acquisition_driver.py` | `AcquisitionDriver` | Direct acquisition IP control: demodulation DDS parameters, acquisition timing, trigger channel, and output mode. |
| `trigger_generator_driver.py` | `TriggerGeneratorDriver` | Direct trigger generator control: shot count, experiment duration, drive-delay FIFO programming, and readout delays. |
| `_fireq_parser.py` | `FireqParser` | Parses `.hwh` XML for module connectivity and PS/PL AXI memory mappings used during driver binding and discovery. |
| `_utils.py` | `_FIREQDriver`, `_DebugMMIO`, helper bit functions | Common base driver and utility primitives used by all low-level IP drivers. |
| `__init__.py` | — | Re-exports the public low-level APIs. |

## Protocol Specs (`specifics/`)

The `specifics` folder is intentionally plain text (`.txt`) and serves as
external protocol documentation for clients.

| File | Scope |
|---|---|
| [`specifics/handshake.txt`](specifics/handshake.txt) | Handshake/authentication flow and hardware summary payload (`hw_summary` / `hw_specs`). |
| [`specifics/upload_envelopes.txt`](specifics/upload_envelopes.txt) | Two-step envelope upload protocol (JSON metadata + binary I/Q frames). |
| [`specifics/compilation.txt`](specifics/compilation.txt) | Wave compilation request/response format and wave type semantics (`env`, `vz`). |
| [`specifics/run_experiment_regular.txt`](specifics/run_experiment_regular.txt) | Single-experiment execution protocol with `experiment_header`, binary chunks, and timing trailer/message. |
| [`specifics/run_experiment_sweep.txt`](specifics/run_experiment_sweep.txt) | Sweep execution protocol with variable placeholders, streamed point data, final sweep status, and abort semantics. |

## Requirements

- Python `3.10.x`
- `numpy`, `pynq`, `pytest` (see `pyproject.toml` / `requirements.txt`)

## Installation

```bash
pip install -r requirements.txt
```

or

```bash
pip install -e .
```

## Running the Server

```bash
sudo -i
source /etc/profile.d/pynq_venv.sh
python API.py
```

Startup prompts ask for:

1. Overlay folder path under `/home/xilinx/jupyter_notebooks/`
2. Overlay bitfile name (default: `overlay.bit`)
3. Bind host (default: `0.0.0.0`)
4. Port (default: `5000`)
5. Auth token (default: `fireq`)

Stop with `Ctrl+C` for graceful cleanup.
