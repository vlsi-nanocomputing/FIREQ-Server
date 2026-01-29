# file: fireq-utils/server/results.py
"""Result containers for FIREQ operations.

This module provides dataclass containers for standardized operation results.
Each result type includes a ``to_dict()`` method for JSON serialization.
"""

from dataclasses import dataclass


@dataclass
class HardwareStatusResult:
    """Structured status snapshot for a single generator.

    This is an object meant to return user-friendly status queries.

    Invariants
    ----------
    - When ``ok`` is True, the fields ``envelopes`` and ``waves_count`` reflect the current
      generator caches, and ``hw_summary`` is included for context/debugging.
    - When ``ok`` is False, ``error`` contains a human-readable failure reason and other
      fields may be partial defaults.

    Notes
    -----
    The payload is intentionally JSON-friendly: it is designed to be sent over a network
    OR logged without carrying heavy binary buffers.
    """

    ok: bool
    gen_index: int
    envelopes: list[str]
    waves_count: int
    readout_wave: dict | None = None
    hw_summary: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        :return: Dict representation of the status.
        :rtype: dict
        """
        return {
            "ok": self.ok,
            "gen_index": self.gen_index,
            "envelopes": self.envelopes,
            "waves_count": self.waves_count,
            "readout_wave": self.readout_wave,
            "hw_summary": self.hw_summary,
            "error": self.error,
        }


@dataclass
class ResetResult:
    """Outcome of a reset operation on a generator-owned memory region.

    Reset operations are used to recover from stale state (e.g., compiled waves referring
    to removed envelopes) or to enforce a clean execution environment for a new session.

    Fields
    ------
    - ``action`` identifies the reset type (e.g., wave_reset, envelope_reset).
    - ``details`` contains adapter-specific metadata for debugging (kept optional).
    """

    ok: bool
    gen_index: int
    action: str
    details: dict
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        :return: Dict representation of the reset outcome.
        :rtype: dict
        """
        return {
            "ok": self.ok,
            "gen_index": self.gen_index,
            "action": self.action,
            "details": self.details,
            "error": self.error,
        }


@dataclass
class SweepTimingStats:
    """Detailed timing breakdown for sweep execution.

    This structure accumulates timing measurements throughout a sweep
    to provide comprehensive performance instrumentation.

    Fields are organized into:
    - Fixed overhead (O(1)): executed once per sweep
    - Per-point accumulated (O(n)): scales with number of sweep points
    """

    # --- Fixed overhead (O(1) - once per sweep) ---
    plan_ms: float = 0.0  # Time to create sweep plan
    setup_ms: float = 0.0  # Point 0 configuration (envelopes, waves, generators, etc.)
    prepare_sweep_ms: float = 0.0  # prepare_sweep() + build_fast_tasks()
    finalize_ms: float = 0.0  # Time after last point, before response sent

    # --- Per-point accumulated (O(n) - scales with n_points) ---
    total_hardware_ms: float = 0.0  # Sum of all fpga_wait_ms (DMA wait)
    total_dma_overhead_ms: float = 0.0  # Sum of all buffer invalidate
    total_sw_overhead_ms: float = 0.0  # Sum of run_acquisition + loop overhead
    inter_point_overhead_ms: float = 0.0  # Residual time between points

    # --- Metadata ---
    n_points_timed: int = 0  # Validation counter

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        :return: Dict representation of the timing stats.
        :rtype: dict
        """
        measured_total = (
            self.plan_ms
            + self.setup_ms
            + self.prepare_sweep_ms
            + self.total_hardware_ms
            + self.total_dma_overhead_ms
            + self.total_sw_overhead_ms
            + self.inter_point_overhead_ms
            + self.finalize_ms
        )
        return {
            # Fixed overhead
            "plan_ms": self.plan_ms,
            "setup_ms": self.setup_ms,
            "prepare_sweep_ms": self.prepare_sweep_ms,
            "finalize_ms": self.finalize_ms,
            # Per-point accumulated
            "total_hardware_ms": self.total_hardware_ms,
            "total_dma_overhead_ms": self.total_dma_overhead_ms,
            "total_sw_overhead_ms": self.total_sw_overhead_ms,
            "inter_point_overhead_ms": self.inter_point_overhead_ms,
            # Metadata
            "n_points_timed": self.n_points_timed,
            "measured_total_ms": measured_total,
        }


@dataclass
class SweepStatus:
    """Final sweep summary with optional detailed timing breakdown.

    This is the "end-of-run" status of ``MessageHandler.run_sweep()`` and is meant to be
    small and robust: it reports whether the sweep completed successfully, how many points
    were requested vs completed, and the first blocking error if any.
    """

    ok: bool
    sweep_id: str
    n_points: int
    n_completed: int
    error: str | None = None
    timing_stats: SweepTimingStats | None = None

    def to_dict(self) -> dict:
        """Convert sweep status to a dictionary.

        :return: Dict representation of sweep status.
        :rtype: dict
        """
        result = {
            "ok": self.ok,
            "sweep_id": self.sweep_id,
            "n_points": self.n_points,
            "n_completed": self.n_completed,
            "error": self.error,
        }
        if self.timing_stats is not None:
            result["debug_timing"] = self.timing_stats.to_dict()
        return result


__all__ = [
    "HardwareStatusResult",
    "ResetResult",
    "SweepStatus",
    "SweepTimingStats",
]
