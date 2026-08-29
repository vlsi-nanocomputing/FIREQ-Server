# Server Deployment & Operational Guide

This page documents the deployment workflow, initial configuration, CLI startup prompts, and execution runtime for the FIREQ server on the PYNQ-based RFSoC board.

---

The server runs on the Linux system of the RFSoC board. From your local
workstation, clone the server repository and copy it to the board:

```bash
git clone https://github.com/vlsi-nanocomputing/FIREQ-Server.git
scp -r FIREQ-Server xilinx@<board-ip>:/home/xilinx/
```

As an alternative, when the board has network access to GitHub, connect to the
board and clone the repository directly there:

```bash
ssh xilinx@<board-ip>
cd /home/xilinx
git clone https://github.com/vlsi-nanocomputing/FIREQ-Server.git
exit
```

The prepackaged overlays are located in:

* `FIREQ-Server/overlays/zcu216` for the ZCU216
* `FIREQ-Server/rfsoc4x2_overlay/rfsoc4x2` for the RFSoC4x2

Each directory contains the matching `.bit` and `.hwh` files. 

### Board-side startup

Connect to the board and activate the PYNQ environment:

```bash
ssh xilinx@<board-ip>
sudo -i
source /etc/profile.d/pynq_venv.sh
cd /home/xilinx/FIREQ-Server
```

Install or update server dependencies when necessary:

```bash
pip install -r requirements.txt
```

Start the interactive server entry point:

```bash
python start_server.py
```

Upon launch, `start_server.py` executes an interactive prompt setup:

- **Logging level**: Type `debug` or press `Enter` for default `info`.
- **Overlay filename**: Enter the bitstream filename relative to `/home/xilinx/` (press `Enter` for default `overlay.bit`). The matching `.hwh` hardware handoff file must reside in the same directory.
- **Server host**: Define the listening interface (press `Enter` for `0.0.0.0` to bind all network interfaces).
- **Server port**: Define the TCP port (press `Enter` for `5000`).
- **Auth token**: Define the secret security token (press `Enter` for default `"fireq"`).

Once inputs are accepted, `FIREQServer` loads the FPGA overlay, initializes memory-mapped registers, binds the socket to the chosen address, and starts worker threads.

To stop the server safely without leaving hardware registers in uninitialized states, press `Ctrl+C` (`KeyboardInterrupt`).