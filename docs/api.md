# FIREQ Server API Reference & Software Architecture

The `FIREQ Server` represents the central orchestrator of the FIREQ quantum control platform. Operating on the SoC target, it bridges high-level client experiment definitions with the low-level FPGA hardware blocks. 

This document serves as the complete technical specification of the server's API. It describes:
- The runtime entry point, 
- The object-oriented system tree,
- The parameter binding interfaces
- The dependency resolution,
- The sweep execution engine,
- The binary networking protocol.

## Module Index & Core Components

Below is the complete API reference split into functional modules.

```{toctree}
:maxdepth: 1

api_modules/runtime
api_modules/system_api
api_modules/system_tree
api_modules/hardware_dependency
api_modules/sweep_experiment
api_modules/network_protocol