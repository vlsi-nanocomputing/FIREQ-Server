# FIREQ Server
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22150509.svg)](https://doi.org/10.5281/zenodo.22150509)
[![GitHub Release](https://img.shields.io/github/v/release/vlsi-nanocomputing/FIREQ-Server)](https://github.com/vlsi-nanocomputing/FIREQ-Server/releases/latest)

Control stack for FIREQ FPGA experiments. This repository contains the
low-level PYNQ drivers (`FIREQ_LL_API`), a high-level hardware system model
(`FIREQ_SYSTEM`), and a single-client TCP server (`FIREQ_SERVER`) that exposes
experiment configuration and execution over the network.

Every Python package has its own README with the details; this file is the
entry point.

## Repository layout

```text
.
├── API.py                - interactive server entry point
├── FIREQ_LL_API/         - low-level PYNQ drivers for FIREQ IPs
├── FIREQ_SYSTEM/         - tree-structured hardware model + dependency DAG
├── FIREQ_SERVER/         - TCP server, network protocol, sweep execution
├── test/                 - legacy pytest suite (kept for reference)
├── overlays/             - board overlays (e.g. ZCU216)
├── deprecated/           - retired code from earlier revisions (not a package)
├── pyproject.toml        - project metadata and tool configuration
├── requirements.txt      - full board/PYNQ environment snapshot
├── sync_board.sh         - rsync deployment helper
├── CONTRIBUTING.md       - contribution and style guidelines
├── CODEOWNERS            - code ownership for the CI
├── .gitlab-ci.yml        - CI pipeline
└── .pre-commit-config.yaml - pre-commit hooks
```

## Components

| Path | Public API | Responsibility |
|---|---|---|
| [`FIREQ_LL_API/`](FIREQ_LL_API/README.md) | `FIREQSoC`, `load_fireq`, `GeneratorDriver`, `AcquisitionDriver`, `TriggerGeneratorDriver`, `AXIStreamSwitchDriver`, `FIFOWrapper` | Loads the overlay, parses the `.hwh` connectivity, and provides register/memory-level control of FIREQ IPs in low-level units (clock cycles, samples, normalized frequencies/phases). |
| [`FIREQ_SYSTEM/`](FIREQ_SYSTEM/README.md) | `FIREQSystemNode`, `AcquisitionNode`, `SignalGeneratorNode`, `TriggerGeneratorNode`, `SwitchNode`, `DMANode`, `FIFONode` | Builds a tree of hardware nodes from the discovered IPs, applies nested configuration dictionaries, resolves inter-node dependencies with a DAG, and streams acquisition data out of the DMAs. |
| [`FIREQ_SERVER/`](FIREQ_SERVER/README.md) | `FIREQServer`, `ReceiveWorker`, `SendWorker`, `FIREQNetworkPacket`, `SweepExperiment` | Single-client TCP server, framed msgpack protocol, handshake/auth, command dispatch, and sweep execution. |
| [`API.py`](API.py) | `main()` | Interactive startup that prompts for the overlay path and network parameters, then starts `FIREQServer`. |
| [`test/`](test/) | `pytest` | Legacy tests from an earlier API revision. They are kept in the tree but are not aligned with the current package structure. |

## Runtime flow

```text
API.py
  └── FIREQServer
        ├── FIREQSystemNode
        │     └── FIREQSoC (FIREQ_LL_API + PYNQ overlay)
        ├── ReceiveWorker ── queue_in ──► main thread
        └── SendWorker    ◄─ queue_out ── main thread
```

1. `FIREQServer` loads the FIREQ overlay through `FIREQSystemNode`/`FIREQSoC`.
2. The server accepts one TCP client and performs a token handshake.
3. The receiver thread parses framed messages into the input queue.
4. The main thread executes commands, optionally running experiments or sweeps.
5. The sender thread serializes responses and streamed DMA payloads to the client.

## Network protocol (summary)

- Each message starts with a 4-byte big-endian header length.
- The header is msgpack-encoded; trailing binary data is optional and its size is
  carried in the header (`tsize` for DMA payloads).
- Commands are delivered as header dictionaries containing `cmd`.
- Responses are header dictionaries with a `type` such as `status`, `warning`,
  `error`, `dma_package`, or `sweep_experiment_header`.

| Command | Message fields | Behaviour |
|---|---|---|
| `ping` | `cmd` | Replies with `{"resp": "pong"}`. |
| `apply_configuration` | `system` | Applies a nested system configuration. Warns if it contains sweepable parameters. |
| `config_and_run` | `system`, optional `variables` | Applies the configuration and runs a single experiment, or a sweep when sweepable callbacks and `variables` are present. |
| `reset_all` | — | Resets IP memory/registers and system node state. |
| `logout` | — | Closes the current client connection. |

See [`FIREQ_SERVER/network/README.md`](FIREQ_SERVER/network/README.md) for the
threading and framing details, and
[`FIREQ_SERVER/execution/README.md`](FIREQ_SERVER/execution/README.md) for
sweeps.

## Configuration dictionaries

`FIREQ_SYSTEM` accepts nested dictionaries where `$`-prefixed keys are
parameters and other keys refer to child nodes. List values create child objects
and must contain `_name`.

```python
config = {
    "system": {
        "$shots": 100,
        "my_generator": {
            "$dfrequency": 200.0,
            "$rchannel": 1,
            "envelope": [
                {"_name": "gauss", "$samples": np.array([0.0 + 0.0j, 0.5 + 0.5j])},
            ],
            "pulse": [
                {"_name": "x90", "_envelope": "gauss", "$duration": 20.0, "$gain": 0.9},
                {"_name": "readout", "_envelope": "gauss", "_readout": True,
                 "$duration": 100.0, "$gain": 0.5},
            ],
            "$drive_order": ["x90"],
        },
        "my_acquisition": {
            "$duration": 500.0,
            "$output_type": "raw",
            "$rfrequency": 200.0,
            "$rphase": 0.0,
            "$rchannel": 1,
            "$tof": 0.0,
        },
        "trigger": {
            "$experiment_duration": 2000.0,
            "delay": [
                {"_name": "readout_delay", "_ttype": "readout",
                 "_channel_mask": 1, "_index": 1, "$delay": 50.0},
            ],
        },
    }
}
```

Node names (`my_generator`, `my_acquisition`, `trigger`) are placeholders; real
names are the IP instance names discovered from the loaded `.hwh` design.
Envelope samples are passed as complex-valued NumPy arrays.

Sweepable parameters are expressed with a string starting with `#`, for example
`"$gain": "#gain"`. `config_and_run` then expects a `variables` object
describing how each sweep variable is generated (`lin`, `const`, or `list`
mode). See [`FIREQ_SYSTEM/README.md`](FIREQ_SYSTEM/README.md) and
[`FIREQ_SERVER/execution/README.md`](FIREQ_SERVER/execution/README.md) for
details.

## Requirements

- Python `3.10`
- PYNQ 3.x environment on the target board (`pynq`, `xrfdc`, `xrfclk`)
- Runtime packages include `numpy`, `msgpack`, `networkx`, `anytree`
- Development tools: `black`, `ruff`, `pre-commit`, `pytest`

See [`pyproject.toml`](pyproject.toml) for tool configuration and
[`requirements.txt`](requirements.txt) for the full board environment snapshot.

## Installation

On the target PYNQ board:

```bash
pip install -r requirements.txt
```

or, for an editable project install:

```bash
pip install -e .
```

## Running the server

```bash
sudo -i
source /etc/profile.d/pynq_venv.sh
python API.py
```

Startup prompts ask for:

1. Logging level (`debug` or `info`, default `info`)
2. Overlay filename relative to `/home/xilinx/` (default `overlay.bit`)
3. Bind host (default `0.0.0.0`)
4. Port (default `5000`)
5. Auth token (default `fireq`)

> **Note:** the current `API.py` reads the token prompt but does not forward it
> to `FIREQServer`, so the server uses its default token (`fireq`).

Stop with `Ctrl+C` for graceful cleanup.

## Development

```bash
pre-commit install
black --check .
ruff check .
pytest            # test/ is currently legacy; see below
```

`sync_board.sh [remote_host]` rsyncs the repository to a PYNQ board (default
host `vlsi-rf4x2.polito.it`).

## Notes on tests

The files in [`test/`](test/) target an earlier API surface (for example,
`MessageHandler`, `OverlayAdapter`, and `SweepStatus`). They are intentionally
left in place for now and are expected to be updated to the current
`FIREQ_SYSTEM`/`FIREQ_SERVER` structure in a follow-up.
