"""Entry point for the FIREQ Server.

This module starts the FIREQServer with a loaded FIREQ SoC overlay.
"""

import logging
import os
import sys

from FIREQ_LL_API import FIREQSoC
from server import FIREQServer, MessageHandler, OverlayAdapter

# Path to the base directory where the overlay files are stored
BASE_PATH = "/home/xilinx/jupyter_notebooks/"


def setup_logging() -> logging.Logger:
    """Configure logging for the server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return logging.getLogger(__name__)


def main() -> None:
    """Prompt for overlay, load it, and start the FIREQ server."""
    logger = setup_logging()
    logger.info("### FIREQ Server startup ###\n")

    # Get overlay path
    ol_folder = input("\n# Insert Overlay folder\n")
    ol_filename = input('# Insert Overlay filename (press Enter for "overlay.bit")\n')
    if not ol_filename:
        ol_filename = "overlay.bit"

    ol_filepath = BASE_PATH + ol_folder + "/" + ol_filename

    # Check existence of overlay
    if not os.path.exists(ol_filepath):
        logger.error(f"Overlay invalid filepath '{ol_filepath}'")
        sys.exit(-1)

    # Load the overlay
    logger.info(f"Loading overlay from {ol_filepath}")
    try:
        overlay_driver = FIREQSoC(ol_filepath)
    except Exception as e:
        logger.error(f"Failed to load overlay: {e}")
        sys.exit(-1)

    # Create adapter and message handler
    logger.info("Initializing hardware adapter and message handler...")
    try:
        adapter = OverlayAdapter(overlay_driver, logger=logger)
        handler = MessageHandler(adapter, logger=logger)
    except Exception as e:
        logger.error(f"Failed to initialize adapter/handler: {e}")
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

    # Create and start server
    logger.info(f"Starting FIREQ Server on {host}:{port}")
    try:
        server = FIREQServer(handler, host=host, port=port, auth_token=auth_token, logger=logger)
        server.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, stopping server...")
        server.stop()
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(-1)


if __name__ == "__main__":
    main()
