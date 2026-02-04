"""FIFO sequence programming and drive source control.

This module handles:
- Drive sequence FIFO programming
- Drive source selection (FIFO vs LFSR)
- FIFO cache management
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from ....models.exceptions import ConfigurationError
from . import utils as gu

if TYPE_CHECKING:
    from ..cache import AdapterContext


class FIFOOps:
    """FIFO sequence and drive source operations."""

    def __init__(self, ctx: AdapterContext) -> None:  # type: ignore  # noqa: F821
        """Initialize FIFOOps.

        :param ctx: Shared adapter context with all dependencies.
        """
        self._ctx = ctx

    def _update_fifo_cache(
        self,
        gen_index: int,
        wave_id_list: list[str],
        start_index: int,
    ) -> list[str]:
        """Update FIFO cache with new sequence and return complete FIFO.

        :param gen_index: Generator index.
        :param wave_id_list: Wave IDs being programmed.
        :param start_index: FIFO write start index.
        :return: Updated complete FIFO sequence.
        :raises ConfigurationError: If patching from non-1 start_index with insufficient cache.
        """
        end_index = start_index + len(wave_id_list) - 1
        prev = self._ctx.cache.last_fifo.get(int(gen_index), [])

        if start_index == 1:
            new_fifo = list(wave_id_list)
        else:
            if len(prev) < (start_index - 1):
                raise ConfigurationError(
                    f"program_drive_sequence: cannot patch from start_index={start_index} "
                    f"because last_fifo has only {len(prev)} entries. "
                    f"Program from 1 first, then patch."
                )
            suffix = prev[end_index:] if len(prev) >= end_index else []
            new_fifo = prev[: start_index - 1] + list(wave_id_list) + suffix

        self._ctx.cache.last_fifo[int(gen_index)] = new_fifo
        return new_fifo

    def program_drive_sequence(
        self,
        *,
        gen_index: int,
        wave_id_list: list[str],
        start_index: int = 1,
    ) -> dict:
        """Program the generator FIFO with a sequence of wave_ids.

        :param gen_index: Index of the target generator.
        :param wave_id_list: Ordered list of wave IDs to execute.
        :param start_index: FIFO index to start writing at (default 1).
        :return: A dictionary containing the updated FIFO sequence.
        """
        # Import here to avoid circular dependency
        from .waves import WaveOps  # noqa: PLC0415

        self._ctx.logger.debug("program_drive_sequence: gen=%d n=%d", gen_index, len(wave_id_list))

        gen = self._ctx.ll.get_gen(gen_index)
        wave_ops = WaveOps(self._ctx)
        cache = wave_ops.get_wave_cache(gen_index)
        start_index = int(start_index)

        if start_index < 1:
            raise ConfigurationError(f"program_drive_sequence: start_index must be >= 1, got {start_index}")

        gu.validate_fifo_capacity(int(gen.memory_mapped_fifo_segment_depth), start_index, len(wave_id_list))
        gu.validate_wave_ids_in_cache(cache, wave_id_list, gen.wave_memory_dict)

        self.set_drive_source(gen_index=gen_index, source="fifo")
        self._ctx.logger.debug("program_drive_sequence: set_drive_source(gen=%d, source='fifo')", gen_index)

        for i, wave_id in enumerate(wave_id_list, start=start_index):
            wave_addr = gen.wave_memory_dict.get(wave_id, "UNKNOWN")
            self._ctx.logger.debug(
                "program_drive_sequence: FIFO[%d] = wave_id='%s' addr=%s",
                i,
                wave_id,
                wave_addr,
            )
            self._ctx.ll.call(
                gen.add_wave_to_drive_wave_sequence(i, wave_id),
                operation="add_wave_to_drive_wave_sequence",
                driver_name="GeneratorDriver",
                config_error=True,
            )

        new_fifo = self._update_fifo_cache(gen_index, wave_id_list, start_index)

        self._ctx.logger.debug(
            "program_drive_sequence: done gen=%d fifo_len=%d",
            gen_index,
            len(wave_id_list),
        )
        return {"gen_index": int(gen_index), "fifo": new_fifo}

    def set_drive_source(
        self,
        *,
        gen_index: int,
        source: Literal["fifo", "lfsr"],
        seed: int | None = None,
    ) -> dict:
        """Select the source for the drive wave sequence.

        If source="lfsr" and seed is provided, the LFSR seed is programmed before
        enabling LFSR. If source="fifo", the seed parameter is ignored.

        :param gen_index: Index of the generator.
        :param source: Selection between "fifo" (programmed sequence) or "lfsr".
        :param seed: Optional LFSR seed value. Used only when source="lfsr".
        :return: Selected source status (and seed, if applied).
        """
        self._ctx.logger.debug("set_drive_source: gen=%d source=%s seed=%s", gen_index, source, seed)

        gen = self._ctx.ll.get_gen(gen_index)

        source_lower = str(source).lower()
        if source_lower == "fifo":
            source_val = 0

        elif source_lower == "lfsr":
            source_val = 1

            if seed is not None:
                self._ctx.ll.call(
                    gen.set_lfsr_seed(int(seed)),
                    operation="set_lfsr_seed",
                    driver_name="GeneratorDriver",
                    config_error=True,
                )
        else:
            raise ConfigurationError(f"set_drive_source: invalid source='{source}'. Use 'fifo' or 'lfsr'.")

        self._ctx.ll.call(
            gen.set_drive_order_source(source_val),
            operation="set_drive_order_source",
            driver_name="GeneratorDriver",
            config_error=True,
        )

        self._ctx.logger.debug("set_drive_source: done gen=%d source=%s", gen_index, source_lower)

        out = {"gen_index": int(gen_index), "source": source_lower}
        if source_lower == "lfsr" and seed is not None:
            out["seed"] = int(seed)
        return out


__all__ = ["FIFOOps"]
