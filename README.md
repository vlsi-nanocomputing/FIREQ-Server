# FIREQ Server

A simple API for loading FPGA overlays into the FIREQ SoC system.

## Requirements

- Python 3.10+
- Dependencies listed in `pyproject.toml`:
  - numpy
  - pynq >= 3.0.0
  - pytest

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Or, if using pyproject.toml
pip install -e .
```

## Usage

### Start the server

```bash
python3 API.py
```

### Startup Procedure

The server will prompt for the following information:

1. **Overlay folder path**
   - Where your overlay files are located relative to `/home/xilinx/jupyter_notebooks/`
   - Example: `my_overlays`

2. **Overlay filename** (optional)
   - Default: `overlay.bit`
   - Example: `custom_overlay.bit`

3. **Server host** (optional)
   - Where the server binds
   - Default: `0.0.0.0` (all interfaces)
   - Example: `localhost`

4. **Server port** (optional)
   - Default: `5000`
   - Example: `8080`

5. **Auth token** (optional)
   - Token for client authentication
   - Default: `fireq`

### Example Startup Session

```
### FIREQ Server startup ###

# Insert Overlay folder
my_overlays

# Insert Overlay filename (press Enter for "overlay.bit")
my_custom.bit

# Insert server host (press Enter for "0.0.0.0")
0.0.0.0

# Insert server port (press Enter for "5000")
5000

# Insert auth token (press Enter for "fireq")
fireq

Starting FIREQ Server on 0.0.0.0:5000
```

### Stopping the Server

Press `Ctrl+C` to gracefully stop the server. The server will clean up hardware resources before exiting.

## Project Structure

```
fireq-utils/
├── API.py                    # Server entry point
├── FIREQ_LL_API/            # Main package
│   └── OverlayDriver.py     # Overlay and SoC management
├── pyproject.toml           # Project configuration
└── README.md                # This file
```

## Client Communication

Once the server is running, clients can connect via TCP on the specified host and port. The server expects:

1. **Handshake**: Client must authenticate with the correct auth token
2. **Commands**: JSON-formatted commands using the length-prefixed protocol
3. **Responses**: JSON responses and optional binary data streams

See the protocol documentation for detailed message formats.

## Troubleshooting

**Error: "Overlay invalid filepath"**
- Check that the folder path and filename are correct
- Make sure the files exist in `/home/xilinx/jupyter_notebooks/`

**Error: "Module not found" or "Failed to initialize adapter"**
- Install all dependencies: `pip install -e .`
- Verify you have Python 3.10 installed: `python --version`
- Ensure the PYNQ environment is properly configured

**Port already in use**
- Change the port number when prompted (pick a different unused port)
- Or stop any other FIREQ servers running on that port

**Connection refused**
- Make sure the server is running
- Verify the correct host and port
- Check firewall settings if connecting remotely

**Authentication failed**
- Verify the auth token matches on both server and client
- Default token is `fireq`
