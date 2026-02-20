# Execution Package

Experiment orchestration and sweep execution logic for FIREQ.

## Architecture

```
execution/
 ├── message_handler.py   – run/sweep orchestrator and binary stream emission
 ├── handlers.py          – focused handlers for status, reset, envelopes, and waves
 └── sweep_planning.py    – sweep planning, variable path discovery, and fast-path flags
```

## Files

| File | Public API | Responsibility |
|---|---|---|
| `message_handler.py` | `MessageHandler` | High-level execution orchestrator for single experiments (`run`) and multi-point sweeps (`run_sweep`), including hardware configuration sequencing, acquisition streaming, and protocol metadata/chunk emission. |
| `handlers.py` | `StatusHandler`, `ResetHandler`, `EnvelopeHandler`, `WaveHandler` | Specialized adapter wrappers that isolate status inspection, reset/recovery, envelope upload validation/application, and wave compilation error handling. |
| `sweep_planning.py` | `SweepPlan`, `plan_sweep`, `apply_sweep_point`, `compute_sweep_flags`, `generate_sweep_points`, `ValueTracker`, ... | Sweep infrastructure for discovering `$variable` placeholders, generating cartesian/zipped points, computing update flags, and applying only changed settings during fast-path sweep execution. |
| `__init__.py` | — | Re-exports the execution package public surface (`MessageHandler`, handlers, and sweep planning types/helpers). |
