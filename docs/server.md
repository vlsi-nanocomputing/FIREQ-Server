# Server

This page documents the FIREQ server deployment and startup workflow on the PYNQ-based target board.

## Quick reference

A typical server startup workflow is:

1. copy the project to the board,
2. connect over SSH,
3. switch to root with `sudo -i` if needed,
4. activate the environment with `source /etc/profile.d/pynq_venv.sh`,
5. install dependencies with `pip install -r requirements.txt`,
6. start the server with `python API.py`.

## Deployment on the board

To deploy the server to the board, copy the relevant project files to the target system, for example:

```bash
scp -r /path/to/FIREQ-Server xilinx@<board-ip>:/home/xilinx/
```

Make sure the overlay files and any required runtime assets are available on the board before starting the server.

## Overlay path and bitstream files

The server expects the overlay files to be available in the board filesystem. In practice, the overlay path is typically relative to:

```bash
/home/xilinx
```

When configuring the overlay, you will need to provide:

- the `.bit` file name,
- the `.hwh` file name.

These values are used by the server when loading the FPGA overlay.

## SSH connection

Connect to the board over SSH:

```bash
ssh xilinx@<board-ip>
```

## Switching to root

Once logged in, switch to the root user when privileged operations are required:

```bash
sudo -i
```

Use the password for the root account when prompted.

## Python environment

Before running the server, activate the PYNQ virtual environment:

```bash
source /etc/profile.d/pynv_venv.sh
```

Then install the Python dependencies:

```bash
pip install -r requirements.txt
```

## Starting the server

Change to the project directory and launch the server:

```bash
cd /home/xilinx/FIREQ-test
python API.py
```

After the server is running, the FIREQ-Client can be started from a separate computer to connect to the board and submit experiments.
The client repository is available at [FIREQ-Client on GitHub](https://github.com/vlsi-nanocomputing/FIREQ-Client).

## Guided startup prompts

The server startup flow asks for several values interactively:

- `login level`: choose the logging verbosity, for example `debug` or `info`.
- `overlay filename`: provide the overlay name relative to `/home/xilinx`.
- `.bit` and `.hwh`: specify the associated FPGA bitstream and hardware handoff files.

## Exposed endpoints or commands

The server accepts a small set of client commands over the TCP connection. The main ones are:

- `ping`: checks that the server is reachable and responsive.
- `apply_configuration`: applies a hardware configuration received from the client.
- `config_and_run`: applies the configuration and starts an experiment, including sweep execution when variables are provided.
- `reset_all`: resets the hardware state.
- `logout`: closes the client connection.

Unknown or malformed commands are answered with an error message.

## Communication protocol

The server uses a simple framed TCP protocol:

- the client sends a 4-byte big-endian length prefix,
- followed by a serialized message header,
- and optionally additional binary payload data.

The server replies with structured network packets and, for experiment runs, streamed binary data that can include acquisition frames and timing information.

During startup, the server also performs a handshake with the client. The client must provide the expected authentication token, otherwise the connection is rejected.

## Logging

The server uses Python logging with configurable verbosity. The startup script prompts for the logging level and supports:

- `info` for standard operational logs,
- `debug` for more detailed execution traces.

Typical messages include server startup, client connection, handshake status, command processing, experiment start/end, and connection teardown.

## Error handling

The server is designed to fail gracefully when something goes wrong:

- invalid or unsupported commands generate an error packet for the client;
- malformed or invalid configurations are rejected with a warning or error message;
- experiment execution failures are logged and reported back to the client;
- broken connections are handled by closing the client session and clearing the I/O queues.

In practice, errors are logged at warning or exception level and propagated back through the network layer whenever possible.
