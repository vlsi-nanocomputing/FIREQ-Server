# Server Deployment & Operational Guide

This page documents the deployment workflow, initial configuration, CLI startup prompts, and execution runtime for the FIREQ server on the PYNQ-based RFSoC board.

---

## Deployment on the Target Board

To deploy the server application, transfer the project folder and the FPGA bitstream files onto the target Linux filesystem:

```bash
scp -r /path/to/FIREQ-Server xilinx@<board-ip>:/home/xilinx/
```

Before launching the service, ensure that the FPGA overlay artifacts (`.bit` and matching `.hwh` files) are located under the target directory `/home/xilinx/`.

## Environment Setup & SSH Access

1. Open a terminal session to connect to the board:

```bash
ssh xilinx@<board-ip>
```
2. Acquire root privileges (required for PYNQ direct memory-mapped access and hardware registers):

```bash
sudo -i
```
3. Activate the system PYNQ virtual environment to load numpy, pynq, and network libraries:

```bash
source /etc/profile.d/pynq_venv.sh
```
4. If dependencies need to be installed or updated, run:

```bash
cd /home/xilinx/FIREQ-Server
pip install -r requirements.txt
```

## Interactive Startup Sequence

Launch the server using the main entry point:

```bash
python API.py
```

Upon launch, `API.py` executes an interactive prompt setup:

- **Logging level**: Type `debug` or press `Enter` for default `info`.
- **Overlay filename**: Enter the bitstream filename relative to `/home/xilinx/` (press `Enter` for default `overlay.bit`). The matching `.hwh` hardware handoff file must reside in the same directory.
- **Server host**: Define the listening interface (press `Enter` for `0.0.0.0` to bind all network interfaces).
- **Server port**: Define the TCP port (press `Enter` for `5000`).
- **Auth token**: Define the secret security token (press `Enter` for default `"fireq"`).

Once inputs are accepted, `FIREQServer` loads the FPGA overlay, initializes memory-mapped registers, binds the socket to the chosen address, and starts worker threads.

To stop the server safely without leaving hardware registers in uninitialized states, press `Ctrl+C` (`KeyboardInterrupt`).

## Network Communication & Client Commands

The server operates a binary TCP socket server managed by `ReceiveWorker` and `SendWorker` threads.

### Protocol Framing

All network frames follow a structured format:

1. **Length Header**: 4-byte Big-Endian Unsigned Integer stating the size ($N$) of the MsgPack header.
2. **MsgPack Payload**: Serialized dictionary specifying command type (`type`), parameter trees, authentication token, and optional binary size (`tsize`).
3. **Binary Data (Optional)**: Trailing byte arrays containing raw DMA acquisition samples or envelope buffers.

### Commands Handled by Server

- **Authentication & Token Check**: Validates client request tokens against the configured server token.
- **`apply_configuration`**: Deserializes system configuration maps, updating node tree parameters (`$duration`, `$dfrequency`, etc.) and recalculating execution DAG dependencies.
- **`config_and_run` / `run_experiment`**: Applies runtime sweeps via `SweepExperiment`, programs hardware registers, triggers pulse generation, and streams `DMAPayload` objects back to the client.
- **`reset_all`**: Clears memory buffers, resets internal hardware states, and resets sub-system wrappers.
- **`logout` / Connection Teardown**: Closes worker threads cleanly and frees memory buffers.

Clients can connect using the official client library available at [FIREQ-Client on GitHub](https://github.com/vlsi-nanocomputing/FIREQ-Client).

## Logging & Traceability

The server employs standard Python `logging` for operational monitoring:

- **`INFO` (Default)**: Tracks lifecycle events including bitstream loading, client connection/disconnection, handshake validation, experiment run execution, and teardown.
- **`DEBUG`**: Detailed logging of binary network frame sizes, raw dictionary payloads, topological order updates within `_DependencyOrchestrator`, and DMA buffer states.

## Error Handling & Resiliency

- **Invalid Overlay Path**: If the requested `.bit` file does not exist, `API.py` logs an error and aborts startup cleanly.
- **Invalid Port / Binding Error**: Errors during socket binding log an exception and exit immediately without leaving dangling background threads.
- **Malformed Client Packets / Unauthorized Token**: The server rejects invalid packets or mismatched tokens, returning an explicit error header to the client while keeping the background network loop active.
- **Hardware & DMA Buffer Safety**: The `FIFONode` and `_DependencyOrchestrator` dynamically calculate physical memory capacity to avoid FPGA buffer overruns during high-shot experiment execution.
