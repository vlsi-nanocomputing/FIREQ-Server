"""Trigger generator operations for OverlayAdapter.

This module provides the TriggerGeneratorOps class that handles:
- Trigger generator shots configuration
- Experiment duration settings
- Drive and readout delay programming
- Experiment triggering
"""

from __future__ import annotations

import logging

from ...models.exceptions import ConfigurationError
from ._errors import check_driver_result


class TriggerGeneratorOps:
    """Trigger generator control: shots, timing delays, and experiment execution.

    This class owns its own state for drive FIFO high-water-mark tracking.

    :param fireq_soc: The FIREQ_SoC hardware driver instance.
    :type fireq_soc: FIREQ_SoC-compatible
    :param logger: Logger instance for debug/error reporting.
    :type logger: logging.Logger
    """

    _DRIVER_NAME = "TriggerGeneratorDriver"

    def __init__(self, fireq_soc: object, logger: logging.Logger) -> None:
        """Initialize the TriggerGeneratorOps class.

        :param fireq_soc: The FIREQ_SoC hardware driver instance.
        :type fireq_soc: FIREQ_SoC-compatible
        :param logger: Logger instance for debug/error reporting.
        :type logger: logging.Logger
        """
        self._fireq_soc = fireq_soc
        self._logger = logger

        self._drive_fifo_hwm: dict[int, int] = {}

    # ========================================================================
    # PRIVATE HELPERS
    # ========================================================================

    def _get_trig(self) -> object:
        """Retrieve the low-level Trigger Generator driver.

        :return: The low-level trigger driver instance.
        :rtype: object
        :raises ConfigurationError: If no trigger generator is available.
        """
        if self._fireq_soc.trigger is None:
            raise ConfigurationError("No trigger generator available in overlay")
        return self._fireq_soc.trigger

    def _check(self, result: object, *, operation: str, hint: str | None = None) -> object:
        """Check a driver return code and raise on error.

        :param result: Raw return value from the driver method.
        :type result: object
        :param operation: Name of the driver operation.
        :type operation: str
        :param hint: Explicit diagnostic hint.
        :type hint: str | None
        :return: The original result on success.
        :rtype: object
        :raises ConfigurationError: If the result is a negative integer.
        """
        return check_driver_result(
            result,
            operation=operation,
            driver_name=self._DRIVER_NAME,
            logger=self._logger,
            hint=hint,
        )

    # ========================================================================
    # PUBLIC PROPERTIES
    # ========================================================================

    @property
    def max_hw_shots(self) -> int:
        """Maximum number of shots the trigger generator can execute in one run.

        :return: Hardware repetition limit (10-bit register).
        :rtype: int
        """
        return int(self._get_trig().max_hw_repetitions)

    # ========================================================================
    # PUBLIC METHODS
    # ========================================================================

    def set_shots(self, shots: int) -> dict:
        """Set the number of hardware repetitions (shots) for the trigger generator.

        :param shots: Number of repetitions (must be within hardware limits).
        :type shots: int
        :return: Dictionary containing the set number of shots.
        :rtype: dict
        """
        trigger_device = self._get_trig()
        shots = int(shots)

        if shots < 1 or shots > int(trigger_device.max_hw_repetitions):
            raise ConfigurationError(f"shots={shots} out of range [1..{int(trigger_device.max_hw_repetitions)}]")

        self._check(
            trigger_device.set_number_of_shots(shots),
            operation="set_number_of_shots",
        )
        return {"shots": shots}

    def set_duration(self, duration_cycles: int) -> dict:
        """Set the experiment duration (repetition period) in clock cycles.

        :param duration_cycles: The duration of the experiment in FPGA clock cycles.
        :type duration_cycles: int
        :return: A dictionary containing the configured experiment duration.
        :rtype: dict
        :raises ConfigurationError: If duration_cycles is less than 1.
        """
        self._logger.debug("Setting experiment duration. Clock Cycles : %d", duration_cycles)
        trigger_device = self._get_trig()
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
        :type drive: dict | None
        :param readout: Dictionary mapping channel indices to readout delay
            specifications.
        :type readout: dict | None
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
        trigger_device = self._get_trig()
        drive = drive or {}
        readout = readout or {}

        start_idx = int(drive_start_index)
        if start_idx < 1 or start_idx > int(trigger_device.channel_fifo_depth):
            raise ConfigurationError(f"drive_start_index={start_idx} out of range")

        readout_programmed = self._program_readout_delays(trigger_device, readout)
        drive_report = self._program_drive_delays(trigger_device, drive, start_idx)

        self._logger.debug(
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
        trigger = self._get_trig()
        trigger.start_experiment()

    def reset_drive_tracking(self) -> None:
        """Reset the high-water-mark tracking for drive FIFOs.

        Forces a full FIFO clear on the next ``program_delays`` call.
        """
        self._drive_fifo_hwm.clear()
        self._logger.debug("reset_drive_tracking: cleared HWM state")

    # ========================================================================
    # INTERNAL HELPERS
    # ========================================================================

    def _program_readout_delays(self, trigger_device: object, readout: dict) -> list[int]:
        """Program readout delay for each channel.

        :param trigger_device: The low-level trigger generator driver.
        :param readout: Dictionary mapping channel indices to delay specs.
        :return: List of programmed readout channel indices.
        """
        programmed = []
        for channel_key, spec in readout.items():
            channel = int(channel_key)
            if not (isinstance(spec, dict) and "delay" in spec):
                raise ConfigurationError(f"readout[{channel}] must be dict with key 'delay'")
            readout_delay = int(spec["delay"])
            self._logger.debug(
                "program_delays: readout ch=%d delay=%d",
                channel,
                readout_delay,
            )

            self._check(
                trigger_device.set_readout_delay(readout_delay, channel),
                operation="set_readout_delay",
            )
            programmed.append(channel)
        return programmed

    def _program_drive_delays(self, trigger_device: object, drive: dict, start_idx: int) -> dict:
        """Program drive FIFO entries for each channel with lazy cleanup.

        :param trigger_device: The low-level trigger generator driver.
        :param drive: Dictionary mapping channel indices to delay specs.
        :param start_idx: FIFO index to start writing (1-based).
        :return: Report dictionary mapping channels to programming summaries.
        """
        report = {}
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
            self._logger.debug(
                "program_delays: drive ch=%d entries_list=%s",
                channel,
                entries_list,
            )
            for entry_idx, pair in enumerate(entries_list):
                if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                    raise ConfigurationError(
                        f"drive[{channel}] entry #{entry_idx} must be (delay, gen_bit), got: {pair}"
                    )

                delay, generator_bit = pair
                delay = int(delay)
                generator_bit = 1 if int(generator_bit) else 0

                fifo_index = start_idx + entry_idx
                self._logger.debug(
                    "program_delays: drive ch=%d FIFO[%d] delay=%d generator_bit=%d",
                    channel,
                    fifo_index,
                    delay,
                    generator_bit,
                )
                self._check(
                    trigger_device.insert_drive_delay(channel, fifo_index, delay, generator_bit),
                    operation="insert_drive_delay",
                )

            # Only clear slots that previously contained data (avoids unnecessary AXI transactions).
            new_high_water_mark = start_idx + len(entries_list) - 1  # last written index (1-based)
            previous_high_water_mark = self._drive_fifo_hwm.get(channel, 0)

            # Clear only if the new sequence is shorter than the previous one
            if previous_high_water_mark > new_high_water_mark:
                for fifo_index in range(new_high_water_mark + 1, previous_high_water_mark + 1):
                    self._check(
                        trigger_device.insert_drive_delay(channel, fifo_index, int(trigger_device.drive_delay_max), 0),
                        operation="insert_drive_delay",
                    )
                cleared_count = previous_high_water_mark - new_high_water_mark
            else:
                cleared_count = 0

            # Update the high water mark for this channel
            self._drive_fifo_hwm[channel] = new_high_water_mark

            report[channel] = {
                "start_index": start_idx,
                "n_entries": len(entries_list),
                "padded": cleared_count,
            }

        return report


__all__ = ["TriggerGeneratorOps"]
