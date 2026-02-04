"""High-level adapter for FIREQ hardware control.

fireq_utils.server.hardware.ol_adapter
======================================

Purpose
-------
This package implements a *server-facing adapter layer* on top of the low-level
FIREQ hardware drivers exposed by `FIREQ_LL_API.overlay_driver.FIREQ_SoC`.

It applies the Adapter pattern to:
- expose an API for experiment execution,
- expose meaningful error logs,
- keep a coherent High-Level (HL) cache synchronized with Low-Level (LL) driver state.

Architecture
------------
The OverlayAdapter uses composition to integrate four operation classes:
- GeneratorOps: Wave management, envelope upload, FIFO programming
- TriggerOps: Trigger generator configuration and execution
- AcquisitionOps: DMA-based acquisition with chunking and sweep optimization
- ExperimentOps: High-level multi-acquisition orchestration

Key design principles
---------------------
- Fail fast on configuration errors (never let invalid states reach hardware).
- Make HL-LL synchronization explicit and verifiable.
- Expose JSON-serializable outputs suitable for logging and remote control.
"""

from .adapter import OverlayAdapter

__all__ = ["OverlayAdapter"]
