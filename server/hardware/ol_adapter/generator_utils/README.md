# Generator Utilities

Pure utility functions for wave and envelope processing, used by `GeneratorOps` (defined in the parent package's `_gen_ops.py`).

## Files

| File | Role | Responsibility |
|---|---|---|
| `_wave_utils.py` | Pure functions | Wave entry construction, replacement policy checks, envelope validation/processing, FIFO capacity validation, and readout wave cache checks. |
| `_iq_conversion.py` | Pure function | Converts float I/Q sample arrays to hardware-native `cint16` format with scaling and clipping. Acts as a safety guardrail for out-of-range envelope values. |
| `__init__.py` | — | Package docstring only (no public exports; functions are imported directly by `_gen_ops.py`). |
