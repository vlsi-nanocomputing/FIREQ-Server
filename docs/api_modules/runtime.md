# Runtime Management & Entry Point

The server is launched via an interactive CLI entry point module. Upon execution, it establishes standard system logging, prompts for environment settings, instantiates the root system interface with the chosen bitstream, and binds the TCP network server to a specified network interface.

## Application Lifecycle Management

* **Logging Configuration (`setup_logging(level)`)**: 
  Configures logging outputs with timestamped formats (`%(asctime)s - %(name)s - %(levelname)s - %(message)s`). The logging level can be dynamically set to `logging.DEBUG` or `logging.INFO` during startup.
  
* **Startup Sequence (`main()`)**: 
  1. Requests the `.bit` overlay file path relative to the base directory `/home/xilinx/` (defaulting to `overlay.bit`).
  2. Prompts for host binding address (defaulting to `0.0.0.0`) and TCP port (defaulting to `5000`).
  3. Accepts an authentication token (defaulting to `"fireq"`).
  4. Instantiates `FIREQServer(ol_filepath, host, port)` and calls `server.start()`.
  5. Intercepts `KeyboardInterrupt` signals to execute a clean teardown (`server.stop()`) without corrupting FPGA registers.
