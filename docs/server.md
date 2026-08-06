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