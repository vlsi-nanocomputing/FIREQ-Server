# FIREQ Utils

FIREQ control stack for FPGA experiments: low-level PYNQ drivers (`FIREQ_LL_API`), server/runtime orchestration (`server`), and protocol specifications (`specifics`).

## Architecture

```
fireq-utils/
 ├── API.py              – interactive server entry point
 ├── server/             – TCP protocol server + execution orchestration
 ├── FIREQ_LL_API/       – low-level FIREQ SoC/IP drivers
 ├── specifics/          – plain-text protocol specifications (.txt)
 ├── test/               – tests
 ├── pyproject.toml      – project metadata, dependencies, lint/tool config
 └── requirements.txt    – pinned install requirements
```

## Components

| Folder / File | Public API | Responsibility |
|---|---|---|
| [`API.py`](API.py) | `main()` | Prompts for overlay/server parameters, loads `FIREQSoC`, instantiates `OverlayAdapter` + `MessageHandler`, then starts `FIREQServer`. |
| [`server/`](server/README.md) | `FIREQServer`, `MessageHandler`, `OverlayAdapter`, `DMAEngine`, shared models/exceptions | High-level server package: client handshake/auth, command routing, sweep execution, binary streaming protocol, and hardware abstraction wiring. |
| [`FIREQ_LL_API/`](FIREQ_LL_API/__init__.py) | `FIREQSoC`, `load_fireq`, `GeneratorDriver`, `AcquisitionDriver`, `TriggerGeneratorDriver` | Low-level drivers that bind to overlay IPs, expose AXI register/memory control, and build hardware capability metadata from the `.hwh` design. |
| [`specifics/`](specifics/) | `.txt` protocol docs | External protocol documentation in plain text. These files describe request/response payloads and streaming formats used by the server. |

## Server Package

The `server` package contains the runtime service and protocol implementation.

- Network layer (`server/network`): length-prefixed TCP framing, session/auth handling, command dispatch, sender/receiver threading.
- Execution layer (`server/execution`): experiment and sweep orchestration, fast-path sweep updates, stream metadata/chunk emission.
- Hardware layer (`server/hardware`): server-facing `OverlayAdapter` plus DMA orchestration over low-level drivers.
- Models (`server/models`): shared config types, results dataclasses, queue payload classes, exception hierarchy.

See [`server/README.md`](server/README.md) for the package-level breakdown.

## FIREQ_LL_API Package

`FIREQ_LL_API` is the low-level hardware control package on top of PYNQ.

### Architecture

```
FIREQ_LL_API/
 ├── fireq_soc.py                 – overlay loading, IP discovery, hw_specs build
 ├── generator_driver.py          – generator IP control (envelopes, waves, modulation, trigger channel)
 ├── acquisition_driver.py        – acquisition IP control (timing, demodulation, output mode)
 ├── trigger_generator_driver.py  – trigger/timing FIFO programming and shot control
 ├── _fireq_parser.py             – .hwh parser for connectivity + AXI mapping
 ├── _utils.py                    – shared driver/MMIO helpers and bit utilities
 └── __init__.py                  – public low-level exports
```

### Files

| File | Public API | Responsibility |
|---|---|---|
| `fireq_soc.py` | `FIREQSoC`, `load_fireq` | Loads bitstream, initializes clocks, discovers FIREQ IPs, maps RF topology, and computes structured `hw_specs` metadata. |
| `generator_driver.py` | `GeneratorDriver` | Direct generator IP control: envelope memory handling, wave memory/sequencing, modulation and trigger listener configuration. |
| `acquisition_driver.py` | `AcquisitionDriver` | Direct acquisition IP control: demodulation DDS parameters, acquisition timing, trigger channel, and decimated/accumulated output mode. |
| `trigger_generator_driver.py` | `TriggerGeneratorDriver` | Direct trigger generator control: shot count, experiment duration, drive delay FIFO programming, and readout delays. |
| `_fireq_parser.py` | `FireqParser` | Parses `.hwh` XML for module connectivity and PS/PL AXI memory mappings used during driver binding/discovery. |
| `_utils.py` | `_FIREQDriver`, `_DebugMMIO`, helper bit functions | Common base driver and utility primitives used by all low-level IP drivers. |
| `__init__.py` | — | Re-exports the public low-level APIs. |

## Protocol Specs (`specifics/`)

The `specifics` folder is intentionally plain text (`.txt`) and serves as external protocol documentation for clients.

| File | Scope |
|---|---|
| [`specifics/handshake.txt`](specifics/handshake.txt) | Handshake/authentication flow and hardware summary payload (`hw_summary` / `hw_specs`). |
| [`specifics/upload_envelopes.txt`](specifics/upload_envelopes.txt) | Two-step envelope upload protocol (JSON metadata + binary I/Q frames). |
| [`specifics/compilation.txt`](specifics/compilation.txt) | Wave compilation request/response format and wave type semantics (`env`, `vz`). |
| [`specifics/run_experiment_regular.txt`](specifics/run_experiment_regular.txt) | Single experiment execution protocol with `experiment_header`, binary chunks, and timing trailer/message. |
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
source /etc/profile.d/pynq_venv.py
python API.py
```

Startup prompts ask for:

1. Overlay folder path under `/home/xilinx/jupyter_notebooks/`
2. Overlay bitfile name (default: `overlay.bit`)
3. Bind host (default: `0.0.0.0`)
4. Port (default: `5000`)
5. Auth token (default: `fireq`)

Stop with `Ctrl+C` for graceful cleanup.
