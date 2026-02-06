"""Cache management for the hardware adapter.

This module provides centralized cache containers and utility functions for
managing the High-Level (HL) cache that synchronizes with the Low-Level (LL)
driver state.

Cache containers track:
- Wave definitions and envelopes per generator
- Acquisition and trigger channel assignments
- DMA and sweep state
- Timing statistics
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .overlay_adapter_types import WaveEntry

if TYPE_CHECKING:
    from ..dma_engine import DMAEngine
    from .low_level_access import LowLevelAccess


@dataclass
class CacheContainers:
    """Unified container for all adapter cache state.

    This class holds all mutable state shared across operation classes,
    organized by domain to maintain clear separation of concerns.

    State ownership:
    - Wave domain: wave_store, last_fifo, readout_wave_store
    - Acquisition domain: sweep_prepared, last_hw_shots, last_timing_stats
    - Modulation domain: acq_trigger_channel
    - Trigger domain: trigger_drive_fifo_hwm
    """

    # Wave management state (per-generator caches)
    wave_store: dict[int, dict[str, WaveEntry]] = field(default_factory=dict)
    """Cache of compiled Wave Definition Words (WDW) per generator."""

    last_fifo: dict[int, list[str]] = field(default_factory=dict)
    """Memory of last used FIFO sequence per generator."""

    readout_wave_store: dict[int, WaveEntry] = field(default_factory=dict)
    """Cache for readout waves (one per generator)."""

    # Acquisition state
    sweep_prepared: bool = False
    """Flag indicating whether sweep mode is active."""

    last_hw_shots: int | None = None
    """Memoization of last configured hardware shots to avoid redundant writes."""

    last_timing_stats: dict[str, float] = field(
        default_factory=lambda: {
            "total_ms": 0.0,
            "fpga_wait_ms": 0.0,
            "dma_overhead_ms": 0.0,
            "sw_overhead_ms": 0.0,
        }
    )
    """Timing statistics from last acquisition."""

    # Modulation state
    acq_trigger_channel: dict[int, int] = field(default_factory=dict)
    """Track acquisition trigger channels for diagnostics."""

    # Trigger state
    trigger_drive_fifo_hwm: dict[int, int] = field(default_factory=dict)
    """Track high water mark for trigger generator drive FIFOs (per channel)."""


@dataclass
class AdapterContext:
    """Shared context for all operation classes following composition pattern.

    Centralizes all dependencies and shared state, providing a single
    parameter to each operation class constructor for clean dependency injection.

    Operation object references (trigger, generator, acquisition) are stored
    after initialization to enable cross-dependencies between operation classes.

    Attributes
    ----------
    overlay_driver : object
        The low-level FIREQ SoC overlay driver instance.
    ll : LowLevelAccess
        Low-level access helper for driver calls and error handling.
    cache : CacheContainers
        Shared cache containers for all state.
    logger : logging.Logger
        Logger instance for debug/error reporting.
    dma_engine : DMAEngine
        DMA orchestration engine for multi-ADC acquisition.
    trigger : object | None
        Reference to TriggerGeneratorOps instance (set after initialization for cross-dependency).
    generator : object | None
        Reference to GeneratorOps instance (set after initialization for cross-dependency).
    acquisition : object | None
        Reference to AcquisitionOps instance (set after initialization for cross-dependency).
    """

    overlay_driver: object
    ll: LowLevelAccess  # type: ignore  # noqa: F821
    cache: CacheContainers
    logger: logging.Logger
    dma_engine: DMAEngine  # type: ignore  # noqa: F821
    trigger: object | None = None
    generator: object | None = None
    acquisition: object | None = None


def get_wave_cache(cache: CacheContainers, gen_index: int) -> dict[str, WaveEntry]:
    """Retrieve the HL wave cache for a generator (lazy-initialized).

    :param cache: The cache containers object.
    :param gen_index: Index of the target generator.
    :return: A dictionary mapping wave IDs to their corresponding WaveEntry objects.
    """
    wave_cache = cache.wave_store.get(gen_index)
    if wave_cache is None:
        wave_cache = {}
        cache.wave_store[gen_index] = wave_cache
    return wave_cache


__all__ = [
    "AdapterContext",
    "CacheContainers",
    "get_wave_cache",
]
