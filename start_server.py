"""Entry point for the FIREQ Server.

This module starts the FIREQServer with a loaded FIREQ SoC overlay.
"""

import logging
import os
import sys

from FIREQ_SERVER import FIREQServer

# Path to the base directory where the overlay files are stored
HOME_PATH = "/home/xilinx/"


def setup_logging(level: int) -> logging.Logger:
    """Configure logging for the server."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return logging.getLogger(__name__)


def main() -> None:
    """Prompt for overlay, load it, and start the FIREQ server."""
    logger = setup_logging(level=logging.INFO)
    logger.info("### FIREQ Server startup ###\n")

    # set logging level
    log_level = input("Input logging level: 'debug', 'info' (press enter for 'info')\n")
    if log_level == "debug":
        logger.setLevel(logging.DEBUG)
    else:
        pass

    # Get overlay path
    ol_filename = input(f"# Insert Overlay filename relative to '{HOME_PATH}' (press Enter for 'overlay.bit')\n")
    if not ol_filename:
        ol_filename = "overlay.bit"

    ol_filepath = HOME_PATH + ol_filename

    # Check existence of overlay
    if not os.path.exists(ol_filepath):
        logger.error(f"Overlay invalid filepath '{ol_filepath}'")
        sys.exit(-1)

    # Get server configuration
    host = input('# Insert server host (press Enter for "0.0.0.0")\n').strip()
    if not host:
        host = "0.0.0.0"

    port_str = input('# Insert server port (press Enter for "5000")\n').strip()
    if not port_str:
        port = 5000
    else:
        try:
            port = int(port_str)
        except ValueError:
            logger.error(f"Invalid port number: {port_str}")
            sys.exit(-1)

    auth_token = input('# Insert auth token (press Enter for "fireq")\n').strip()
    if not auth_token:
        auth_token = "fireq"

    # run the server
    logger.info(f"Starting server using overlay '{ol_filepath}, on address '{host}:{port}', with token {auth_token}")
    try:
        server = FIREQServer(ol_filepath, host, port)
    except Exception as e:
        logger.error(f"Failed to initialize server: {e}")
        sys.exit(-1)

    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, stopping server...")
        server.stop()


if __name__ == "__main__":
    main()
