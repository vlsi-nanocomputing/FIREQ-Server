"""Trigger generator operations for OverlayAdapter.

This module provides the TriggerOps class that handles:
- Trigger generator shots configuration
- Experiment duration settings
- Drive and readout delay programming
- Experiment triggering
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...models.exceptions import ConfigurationError
from .ll_access import LowLevelAccess

if TYPE_CHECKING:
    from .cache import CacheContainers


class TriggerOps:
    """Operation class for trigger generator control.

    This class handles all trigger-related hardware operations, including
    shot configuration, timing delays, and experiment execution.

    Attributes:
    -----------
    _ll : LowLevelAccess
        Unified interface for low-level driver access and error handling.
    _logger : logging.Logger
        Logger instance for debug/error reporting.
    _cache : CacheContainers
        Shared cache with trigger state (tg_drive_hwm).
    """

    def __init__(
        self,
        ll: LowLevelAccess,
        cache: CacheContainers,
        logger: logging.Logger,
    ) -> None:
        """Initialize the TriggerOps class.

        :param ll: Low-level driver access helper.
        :type ll: LowLevelAccess
        :param cache: Shared cache containers.
        :type cache: CacheContainers
        :param logger: Logger instance.
        :type logger: logging.Logger
        """
        self._ll = ll
        self._cache = cache
        self._logger = logger

    def set_shots(self, shots: int) -> dict:
        """Set the number of hardware repetitions (shots) for the trigger generator.

        :param shots: Number of repetitions (must be within hardware limits).
        :type shots: int
        :return: Dictionary containing the set number of shots.
        :rtype: dict
        """
        t = self._ll.get_trig()
        shots_i = int(shots)

        if shots_i < 1 or shots_i > int(t.max_hw_repetitions):
            raise ConfigurationError(f"shots={shots_i} out of range [1..{int(t.max_hw_repetitions)}]")

        t.set_number_of_shots(shots_i)
        return {"shots": shots_i}

    def set_duration(self, duration_cycles: int) -> dict:
        """Set the total duration of the experiment in clock cycles.

        This parameter defines the repetition period of the global trigger sequence.

        :param duration_cycles: The duration of the experiment in FPGA clock cycles.
        :type duration_cycles: int
        :return: A dictionary containing the configured experiment duration.
        :rtype: dict
        :raises ConfigurationError: If duration_cycles is less than 1.
        """
        self._logger.debug("Setting experiment duration. Clock Cycles : %d", duration_cycles)
        t = self._ll.get_trig()
        dur_i = int(duration_cycles)
        if dur_i < 1:
            raise ConfigurationError(f"duration={dur_i} is not valid. Must be positive.")

        t.set_experiment_duration(dur_i)
        return {"experiment_duration": dur_i}

    def program_delays(
        self,
        *,
        drive: dict | None = None,
        readout: dict | None = None,
        drive_start_index: int = 1,
    ) -> dict:
        """Program the timing delays for drive and readout triggers.

        For each programmed drive channel, entries from
        ``drive_start_index + len(entries)`` to the FIFO end are cleared. This means
        partial patching does not preserve any existing tail.

        :param drive: Dictionary mapping channel indices to lists of (delay, value)
            pairs.
        :type drive: Optional[dict]
        :param readout: Dictionary mapping channel indices to readout delay
            specifications.
        :type readout: Optional[dict]
        :param drive_start_index: FIFO index to start writing drive delays (default 1).
            Higher indices imply patching.
        :type drive_start_index: int
        :return: Report of programmed readout channels and drive sequences.
        :rtype: dict
        """
        self._logger.debug("Setting experiment delays in the Trigger Generator")
        self._logger.debug(
            "---Experiment delay details--- \n1. drive_start_index = %d \n2.drive_delays = %s \n3.readout_delays= %s",
            drive_start_index,
            drive,
            readout,
        )
        t = self._ll.get_trig()
        drive = drive or {}
        readout = readout or {}

        start_idx = int(drive_start_index)
        if start_idx < 1 or start_idx > int(t.channel_fifo_depth):
            raise ConfigurationError(f"drive_start_index={start_idx} out of range")

        # --- readout delays (1 scalar per channel)
        ro_programmed = []
        for ch_key, spec in readout.items():
            ch = int(ch_key)
            if not (isinstance(spec, dict) and "delay" in spec):
                raise ConfigurationError(f"readout[{ch}] must be dict with key 'delay'")
            ro_delay = int(spec["delay"])
            self._logger.debug(
                "program_delays: readout ch=%d delay=%d",
                ch,
                ro_delay,
            )

            self._ll.call(
                t.set_readout_delay(ro_delay, ch),
                operation="set_readout_delay",
                driver_name="TriggerGeneratorDriver",
                config_error=True,
            )
            ro_programmed.append(ch)

        # --- drive FIFO entries
        drive_report = {}
        for ch_key, spec in drive.items():
            ch = int(ch_key)
            if not (isinstance(spec, dict) and "delay" in spec):
                raise ConfigurationError(f"drive[{ch}] must be dict with key 'delay'")

            entries_list = list(spec["delay"])  # list of pairs

            # check capacity relative to start index
            max_writable = int(t.channel_fifo_depth) - (start_idx - 1)
            if len(entries_list) > max_writable:
                raise ConfigurationError(
                    f"drive[{ch}] too long for start_index={start_idx}: " f"{len(entries_list)} > {max_writable}"
                )

            # program the requested block (patching supported via start_idx)
            self._logger.debug(
                "program_delays: drive ch=%d entries_list=%s",
                ch,
                entries_list,
            )
            for k, pair in enumerate(entries_list):
                if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                    raise ConfigurationError(f"drive[{ch}] entry #{k} must be (delay, gen_bit), got: {pair}")

                delay, gen = pair
                delay_i = int(delay)
                gen_i = 1 if int(gen) else 0

                fifo_index = start_idx + k  # LL index is 1-based
                self._logger.debug(
                    "program_delays: drive ch=%d FIFO[%d] delay=%d gen_bit=%d",
                    ch,
                    fifo_index,
                    delay_i,
                    gen_i,
                )
                self._ll.call(
                    t.insert_drive_delay(ch, fifo_index, delay_i, gen_i),
                    operation="insert_drive_delay",
                    driver_name="TriggerGeneratorDriver",
                    config_error=True,
                )

            # Lazy FIFO cleanup: only clear slots that previously contained data.
            # This optimization avoids thousands of unnecessary AXI transactions during sweeps.
            new_hwm = start_idx + len(entries_list) - 1  # last written index (1-based)
            prev_hwm = self._cache.tg_drive_hwm.get(ch, 0)

            # Clear only if the new sequence is shorter than the previous one
            if prev_hwm > new_hwm:
                for fifo_index in range(new_hwm + 1, prev_hwm + 1):
                    self._ll.call(
                        t.insert_drive_delay(ch, fifo_index, int(t.drive_delay_max), 0),
                        operation="insert_drive_delay",
                        driver_name="TriggerGeneratorDriver",
                        config_error=True,
                    )
                cleared_count = prev_hwm - new_hwm
            else:
                cleared_count = 0

            # Update the high water mark for this channel
            self._cache.tg_drive_hwm[ch] = new_hwm

            drive_report[ch] = {
                "start_index": start_idx,
                "n_entries": len(entries_list),
                "padded": cleared_count,
            }

        self._logger.debug(
            "program_delays: DONE readout_channels=%s drive_report=%s",
            sorted(ro_programmed),
            drive_report,
        )
        return {
            "readout_channels_programmed": sorted(ro_programmed),
            "drive_programmed": drive_report,
        }

    def trigger_experiment(self) -> None:
        """Trigger the experiment."""
        trigger = self._ll.get_trig()
        trigger.start_experiment()

    def reset_drive_tracking(self) -> None:
        """Reset the high water mark tracking for trigger generator drive FIFOs.

        Call this after a hardware reset or when you want to force a full FIFO clear
        on the next program_delays call. This is useful when the FIFO state is
        unknown or when switching between different experiment configurations.
        """
        self._cache.tg_drive_hwm.clear()
        self._logger.debug("reset_drive_tracking: cleared HWM state")


__all__ = ["TriggerOps"]
