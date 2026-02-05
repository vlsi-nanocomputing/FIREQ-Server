"""Trigger generator operations for OverlayAdapter.

This module provides the TriggerOps class that handles:
- Trigger generator shots configuration
- Experiment duration settings
- Drive and readout delay programming
- Experiment triggering
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...models.exceptions import ConfigurationError

if TYPE_CHECKING:
    from .cache import AdapterContext


class TriggerOps:
    """Operation class for trigger generator control.

    This class handles all trigger-related hardware operations, including
    shot configuration, timing delays, and experiment execution.

    Attributes:
    -----------
    _ctx : AdapterContext
        Shared context containing ll, cache, logger, and other dependencies.
    """

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize the TriggerOps class.

        :param ctx: Shared adapter context with all dependencies.
        :type ctx: AdapterContext
        """
        self._ctx = ctx

    def set_shots(self, shots: int) -> dict:
        """Set the number of hardware repetitions (shots) for the trigger generator.

        :param shots: Number of repetitions (must be within hardware limits).
        :type shots: int
        :return: Dictionary containing the set number of shots.
        :rtype: dict
        """
        trigger_device = self._ctx.ll.get_trig()
        shots = int(shots)

        if shots < 1 or shots > int(trigger_device.max_hw_repetitions):
            raise ConfigurationError(f"shots={shots} out of range [1..{int(trigger_device.max_hw_repetitions)}]")

        trigger_device.set_number_of_shots(shots)
        return {"shots": shots}

    def set_duration(self, duration_cycles: int) -> dict:
        """Set the total duration of the experiment in clock cycles.

        This parameter defines the repetition period of the global trigger sequence.

        :param duration_cycles: The duration of the experiment in FPGA clock cycles.
        :type duration_cycles: int
        :return: A dictionary containing the configured experiment duration.
        :rtype: dict
        :raises ConfigurationError: If duration_cycles is less than 1.
        """
        self._ctx.logger.debug("Setting experiment duration. Clock Cycles : %d", duration_cycles)
        trigger_device = self._ctx.ll.get_trig()
        duration_cycles = int(duration_cycles)
        if duration_cycles < 1:
            raise ConfigurationError(f"duration={duration_cycles} is not valid. Must be positive.")

        trigger_device.set_experiment_duration(duration_cycles)
        return {"experiment_duration": duration_cycles}

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
        self._ctx.logger.debug("Setting experiment delays in the Trigger Generator")
        self._ctx.logger.debug(
            "---Experiment delay details--- \n1. drive_start_index = %d \n2.drive_delays = %s \n3.readout_delays= %s",
            drive_start_index,
            drive,
            readout,
        )
        trigger_device = self._ctx.ll.get_trig()
        drive = drive or {}
        readout = readout or {}

        start_idx = int(drive_start_index)
        if start_idx < 1 or start_idx > int(trigger_device.channel_fifo_depth):
            raise ConfigurationError(f"drive_start_index={start_idx} out of range")

        # --- readout delays (1 scalar per channel)
        readout_programmed = []
        for channel_key, spec in readout.items():
            channel = int(channel_key)
            if not (isinstance(spec, dict) and "delay" in spec):
                raise ConfigurationError(f"readout[{channel}] must be dict with key 'delay'")
            readout_delay = int(spec["delay"])
            self._ctx.logger.debug(
                "program_delays: readout ch=%d delay=%d",
                channel,
                readout_delay,
            )

            self._ctx.ll.call(
                trigger_device.set_readout_delay(readout_delay, channel),
                operation="set_readout_delay",
                driver_name="TriggerGeneratorDriver",
                config_error=True,
            )
            readout_programmed.append(channel)

        # --- drive FIFO entries
        drive_report = {}
        for channel_key, spec in drive.items():
            channel = int(channel_key)
            if not (isinstance(spec, dict) and "delay" in spec):
                raise ConfigurationError(f"drive[{channel}] must be dict with key 'delay'")

            entries_list = list(spec["delay"])  # list of pairs

            # check capacity relative to start index
            max_writable = int(trigger_device.channel_fifo_depth) - (start_idx - 1)
            if len(entries_list) > max_writable:
                raise ConfigurationError(
                    f"drive[{channel}] too long for start_index={start_idx}: " f"{len(entries_list)} > {max_writable}"
                )

            # program the requested block (patching supported via start_idx)
            self._ctx.logger.debug(
                "program_delays: drive ch=%d entries_list=%s",
                channel,
                entries_list,
            )
            for k, pair in enumerate(entries_list):
                if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                    raise ConfigurationError(f"drive[{channel}] entry #{k} must be (delay, gen_bit), got: {pair}")

                delay, generator_bit = pair
                delay = int(delay)
                generator_bit = 1 if int(generator_bit) else 0

                fifo_index = start_idx + k  # LL index is 1-based
                self._ctx.logger.debug(
                    "program_delays: drive ch=%d FIFO[%d] delay=%d generator_bit=%d",
                    channel,
                    fifo_index,
                    delay,
                    generator_bit,
                )
                self._ctx.ll.call(
                    trigger_device.insert_drive_delay(channel, fifo_index, delay, generator_bit),
                    operation="insert_drive_delay",
                    driver_name="TriggerGeneratorDriver",
                    config_error=True,
                )

            # Lazy FIFO cleanup: only clear slots that previously contained data.
            # This optimization avoids thousands of unnecessary AXI transactions during sweeps.
            new_high_water_mark = start_idx + len(entries_list) - 1  # last written index (1-based)
            previous_high_water_mark = self._ctx.cache.trigger_drive_fifo_hwm.get(channel, 0)

            # Clear only if the new sequence is shorter than the previous one
            if previous_high_water_mark > new_high_water_mark:
                for fifo_index in range(new_high_water_mark + 1, previous_high_water_mark + 1):
                    self._ctx.ll.call(
                        trigger_device.insert_drive_delay(channel, fifo_index, int(trigger_device.drive_delay_max), 0),
                        operation="insert_drive_delay",
                        driver_name="TriggerGeneratorDriver",
                        config_error=True,
                    )
                cleared_count = previous_high_water_mark - new_high_water_mark
            else:
                cleared_count = 0

            # Update the high water mark for this channel
            self._ctx.cache.trigger_drive_fifo_hwm[channel] = new_high_water_mark

            drive_report[channel] = {
                "start_index": start_idx,
                "n_entries": len(entries_list),
                "padded": cleared_count,
            }

        self._ctx.logger.debug(
            "program_delays: DONE readout_channels=%s drive_report=%s",
            sorted(readout_programmed),
            drive_report,
        )
        return {
            "readout_channels_programmed": sorted(readout_programmed),
            "drive_programmed": drive_report,
        }

    def trigger_experiment(self) -> None:
        """Trigger the experiment."""
        trigger = self._ctx.ll.get_trig()
        trigger.start_experiment()

    def reset_drive_tracking(self) -> None:
        """Reset the high water mark tracking for trigger generator drive FIFOs.

        Call this after a hardware reset or when you want to force a full FIFO clear
        on the next program_delays call. This is useful when the FIFO state is
        unknown or when switching between different experiment configurations.
        """
        self._ctx.cache.trigger_drive_fifo_hwm.clear()
        self._ctx.logger.debug("reset_drive_tracking: cleared HWM state")


__all__ = ["TriggerOps"]
