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

from dataclasses import dataclass, field

from .types import WaveEntry


@dataclass
class CacheContainers:
    """Unified container for all adapter cache state.

    This class holds all mutable state shared across operation classes,
    organized by domain to maintain clear separation of concerns.

    State ownership:
    - Wave domain: wave_store, last_fifo, readout_wave_store
    - Acquisition domain: sweep_prepared, last_hw_shots, last_timing_stats
    - Modulation domain: acq_trigger_channel
    - Trigger domain: tg_drive_hwm
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
    tg_drive_hwm: dict[int, int] = field(default_factory=dict)
    """Track high water mark for trigger generator drive FIFOs (per channel)."""


def get_wave_cache(cache: CacheContainers, gen_index: int) -> dict[str, WaveEntry]:
    """Retrieve the High-Level wave cache for a specific generator.

    This utility employs lazy initialization: if the cache for the requested
    generator does not exist, an empty dictionary is created, stored, and returned.

    :param cache: The cache containers object.
    :param gen_index: Index of the target generator.
    :return: A dictionary mapping wave IDs to their corresponding WaveEntry objects.
    """
    wave_cache = cache.wave_store.get(gen_index)
    if wave_cache is None:
        wave_cache = {}
        cache.wave_store[gen_index] = wave_cache
    return wave_cache


def reset_cache_for_generator(cache: CacheContainers, gen_index: int) -> None:
    """Reset all cache entries for a specific generator.

    Clears wave memory, envelope cache, FIFO tracking, and readout configuration.

    :param cache: The cache containers object.
    :param gen_index: Index of the target generator.
    """
    cache.wave_store.pop(gen_index, None)
    cache.last_fifo.pop(gen_index, None)
    cache.readout_wave_store.pop(gen_index, None)


def reset_sweep_state(cache: CacheContainers) -> None:
    """Reset sweep mode state and associated memoization.

    Called when transitioning out of sweep mode.

    :param cache: The cache containers object.
    """
    cache.sweep_prepared = False
    cache.last_hw_shots = None


__all__ = [
    "CacheContainers",
    "get_wave_cache",
    "reset_cache_for_generator",
    "reset_sweep_state",
]
