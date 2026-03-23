# file: fireq-utils/server/execution/__init__.py
"""Experiment execution components for FIREQ.

Provides:
- MessageHandler: High-level orchestrator for experiment execution
- Sweep functions: plan_sweep, apply_sweep_point for sweep planning and execution
- Specialized handlers: StatusHandler, ResetHandler, EnvelopeHandler, WaveHandler
"""

from .handlers import EnvelopeHandler, ResetHandler, StatusHandler, WaveHandler
from .hardware_config import HardwareConfigurator
from .message_handler import MessageHandler
from .sweep_planning import (
    FLAG_RULES,
    SimplePath,
    SweepFlagsDict,
    SweepPlan,
    apply_sweep_point,
    compute_sweep_flags,
    find_variable_paths,
    generate_sweep_points,
    plan_sweep,
)

__all__ = [
    # Main orchestrator
    "MessageHandler",
    "HardwareConfigurator",
    # Sweep infrastructure
    "SweepPlan",
    "plan_sweep",
    "apply_sweep_point",
    "SimplePath",
    "SweepFlagsDict",
    "FLAG_RULES",
    "compute_sweep_flags",
    "find_variable_paths",
    "generate_sweep_points",
    # Handlers
    "StatusHandler",
    "ResetHandler",
    "EnvelopeHandler",
    "WaveHandler",
]
