# Execution Package

Experiment orchestration and sweep execution logic for FIREQ.

## Architecture

```text
execution/
├── message_handler.py   - thin public facade used by the network layer
├── handlers.py          - focused handlers for status, reset, envelopes, and waves
├── hardware_config.py   - full hardware configuration sequencing
├── streaming.py         - acquisition stream parameters, metadata, chunk emission
├── sweep_planning.py    - sweep planning, variable discovery, point generation, flags
├── sweep_updates.py     - fast-path per-point hardware delta updates
├── sweep_runner.py      - sweep lifecycle orchestration
└── __init__.py          - execution package public re-exports
```

## Design Overview

The package is now split by responsibility instead of concentrating all run and
sweep logic in `message_handler.py`.

- `MessageHandler` is the only entry point used by the network layer.
- `HardwareConfigurator` owns point-zero/full configuration application.
- `AcquisitionStreamer` owns acquisition stream parameter resolution and chunk emission.
- `SweepPlan` and related helpers own sweep point generation and change analysis.
- `SweepUpdateApplier` owns fast-path per-point hardware mutations after point zero.
- `SweepRunner` owns the multi-point sweep lifecycle and final status emission.

This keeps the public API stable while making the internal execution flow easier
to understand and maintain.

## Files

| File | Public API | Responsibility |
|---|---|---|
| `message_handler.py` | `MessageHandler` | Public execution facade for single experiments (`run`) and multi-point sweeps (`run_sweep`). Wires the specialized collaborators together and exposes the API used by `FIREQServer`. |
| `handlers.py` | `StatusHandler`, `ResetHandler`, `EnvelopeHandler`, `WaveHandler` | Specialized adapter wrappers isolating status inspection, reset/recovery, envelope upload validation/application, and wave compilation error handling. |
| `hardware_config.py` | `HardwareConfigurator` | Applies a full experiment configuration to hardware: envelope upload, wave compilation, generator setup, acquisition setup, and trigger programming. |
| `streaming.py` | `AcquisitionStreamer`, `AcquisitionStreamParams` | Resolves acquisition stream parameters, computes chunk counts, builds metadata payloads, and emits acquisition/sweep binary chunks. |
| `sweep_planning.py` | `SweepPlan`, `plan_sweep`, `apply_sweep_point`, `compute_sweep_flags`, `generate_sweep_points`, `find_variable_paths`, `FLAG_RULES`, `SimplePath`, `SweepFlagsDict` | Pure sweep-planning infrastructure for discovering `$variable` placeholders, generating cartesian/zipped points, computing update flags, and applying values into a config. |
| `sweep_updates.py` | `SweepUpdateApplier`, `ValueTracker`, `make_hashable`, `extract_modulation_value`, `apply_generator_signal_updates` | Fast-path sweep-update machinery for applying only the changed generator/acquisition/trigger fields between sweep points. |
| `sweep_runner.py` | `SweepRunner` | Internal multi-point sweep orchestrator coordinating plan creation, point-zero setup, `prepare_sweep()` / `end_sweep()` lifecycle, point streaming, timing accumulation, and final status emission. |
| `__init__.py` | `MessageHandler`, `HardwareConfigurator`, `SweepPlan`, `plan_sweep`, `apply_sweep_point`, `StatusHandler`, `ResetHandler`, `EnvelopeHandler`, `WaveHandler`, ... | Re-exports the public execution surface used elsewhere in the server package. Internal collaborators such as `SweepRunner` and `AcquisitionStreamer` stay module-local. |

## Single-Run Flow

1. `MessageHandler.run()` receives a full or partial experiment config.
2. `HardwareConfigurator` applies envelopes, waves, generators, acquisitions, and trigger settings.
3. `AcquisitionStreamer` resolves stream parameters and emits binary acquisition chunks.
4. `MessageHandler` wraps those results into `StreamHeader`, `BinaryChunk`, and `StreamTiming`.

## Sweep Flow

1. `MessageHandler.run_sweep()` delegates to `SweepRunner`.
2. `SweepRunner` builds a `SweepPlan` from the base config and variable specs.
3. Point zero is applied through `HardwareConfigurator`, then streamed through `AcquisitionStreamer`.
4. `prepare_sweep()` is called when needed.
5. Remaining points are mutated in-place with `apply_sweep_point()` and applied through `SweepUpdateApplier`.
6. Each point is streamed through `AcquisitionStreamer`.
7. `SweepRunner` emits the final `SweepStatus` timing/result item and ensures `end_sweep()` cleanup.
