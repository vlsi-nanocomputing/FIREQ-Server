from pynq import Overlay, PL
import xrfdc
import xrfclk
import os

from ._Parser import FIREQ_parser
from ._Utils import _FIREQDriver
from .acquistion_driver import AcquistionDriver
from .generator_driver import GeneratorDriver
from .trigger_generator_driver import TriggerGeneratorDriver

__all__ = ["FIREQ_SoC"]


def _init_rf_clks(lmk_freq: float = 245.76, lmx_freq: float = 491.52) -> None:
    """
    Initialise the LMK and LMX clocks for the RF-DC hierarchy.

    The radio clocks are required to talk to the RF-DCs and only need
    to be initialised once per session.
    """
    xrfclk.set_ref_clks(lmk_freq=lmk_freq, lmx_freq=lmx_freq)


class FIREQ_SoC(Overlay):
    """
    Low-level representation of the FIREQ SoC.

    Responsibilities:
    - Load the bitfile.
    - Initialise RF clocks (optional).
    - Use FIREQ_parser on the HWH to:
        * Bind AXI full/lite interfaces for FIREQ IPs.
        * Build lists of Generator/Acquisition/Trigger IPs.
    - Discover infrastructure IPs (RF-DC, AXI switch, DMA).
    - Build validated hardware specs (sample rates, Nyquist, etc.).
    """

    def __init__(
        self,
        bitfile_name: str,
        ignore_version: bool = False,
        init_clocks: bool = True,
    ):
        # 1) Load overlay
        try:
            super().__init__(bitfile_name, ignore_version=ignore_version)
        except Exception as e:
            # better to raise an exception: server aware of the problem
            raise RuntimeError(f"FIREQ: error during overlay creation: {e}") from e
        
        # 2) HWH + parser
        self._FIREQ_hwh_file = os.path.splitext(self.bitfile_name)[0] + ".hwh"
        self._FIREQ_parser = FIREQ_parser(self._FIREQ_hwh_file)

        # 3) RF clocks initialisation
        if init_clocks:
            _init_rf_clks()

        # 4) FIREQ IPs lists
        self._generation_ips = []  # type: list[GeneratorDriver]
        self._readout_ips = []     # type: list[AcquistionDriver]
        self._trigger_ips = []     # type: list[TriggerGeneratorDriver]

        # 5) Low-level discovery (bind AXI + classify IPs)
        self._rf = getattr(self, "usp_rf_data_converter_0", None)
        self._axis_switch = getattr(self, "axis_switch_0", None)
        self._dma = getattr(self, "axi_dma_0", None)
        
        self._discover_fireq_ips()

        # Basic resource sanity check
        if not self._generation_ips:
            raise RuntimeError("FIREQ_SoC: no Generator IPs found in overlay.")
        if not self._readout_ips:
            raise RuntimeError("FIREQ_SoC: no Acquisition IPs found in overlay.")
        if not self._trigger_ips:
            raise RuntimeError("FIREQ_SoC: no TriggerGenerator IP found in overlay.")      

        # 6) Hardware specs (clock validation, sample rates, etc.)
        self.hw_specs = self._build_hw_specs()
        # 7) Health flag
        self._healthy = True  # if _build_hw_specs did not raise exceptions
        # 8) recomended PL reset
        PL.reset()
        

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------
    def _discover_fireq_ips(self) -> None:
        """
        Use FIREQ_parser.GetAddressMapping() to:
        - Bind AXI interfaces on FIREQ IPs.
        - Populate IP lists: _generation_ips, _readout_ips, _trigger_ips.
        """
        mmap = self._FIREQ_parser.GetAddressMapping()

        for ip_name, maps in mmap.items():
            if not hasattr(self, ip_name):
                continue

            ip_object = getattr(self, ip_name)

            # Only consider FIREQ low-level drivers
            if not isinstance(ip_object, _FIREQDriver):
                continue

            # Init AXI based on mapping
            for m in maps:
                axi_base = int(m["BASEVALUE"], 16)
                axi_range = int(m["HIGHVALUE"], 16) - axi_base + 1

                if m["SLAVEBUSINTERFACE"] == "s00_axi":
                    ip_object.init_axi_full_interface(axi_base, axi_range)
                elif m["SLAVEBUSINTERFACE"] == "s01_axi":
                    ip_object.init_axi_lite_interface(axi_base, axi_range)

            # Classification by driver type
            if isinstance(ip_object, GeneratorDriver):
                self._generation_ips.append(ip_object)
            elif isinstance(ip_object, AcquistionDriver):
                self._readout_ips.append(ip_object)
            elif isinstance(ip_object, TriggerGeneratorDriver):
                self._trigger_ips.append(ip_object)

    def _build_hw_specs(self) -> dict:
        """
        Build a validated dictionary of hardware specifications: scalability aware.

        Return structure (high level):

            {
              "rf": { ... },              # DAC/ADC tiles, sample rate, nyquist, ecc.
              "acquisitions": [ {...} ],  # one per AcquistionDriver
              "generators":   [ {...} ],  # one per GeneratorDriver
              "triggers":     [ {...} ],  # one per TriggerGeneratorDriver
              "summary": { ... }          # convenient global aggregates
            }

        Strong constraint: DAC/ADC clocks (RF-DC tiles) must be synchronous
        among themselves (multi-tile sync). If not, an exception is raised.
        For the rest (parallelism, memories, etc.) differences are accepted
        among the IPs, which are reflected in the acquisitions/generators lists.
        """
        rf = self._rf
        if rf is None:
            raise RuntimeError(
                "FIREQ_SoC: no RF Data Converter hierarchy found "
                "(usp_rf_data_converter_0 is missing)."
            )

         # --- DAC Validation ---
        found_dac_sr = None
        dac_tile_specs = []

        for i, tile in enumerate(rf.dac_tiles):
            try:
                lock_stat = getattr(tile, "PLLLockStatus", "Unknown")
                sr_ghz = tile.PLLConfig["SampleRate"]  # tipicamente in GHz
                sr = float(sr_ghz) * 1e9

                if found_dac_sr is None:
                    found_dac_sr = sr
                elif abs(sr - found_dac_sr) > 1e3:  # tolleranza 1 kHz
                    raise RuntimeError(
                        f"FIREQ_SoC: DAC Clock mismatch! "
                        f"Tile {i} has {sr} Hz vs {found_dac_sr} Hz."
                    )

                dac_tile_specs.append(
                    {
                        "tile": i,
                        "sample_rate_hz": sr,
                        "pll_lock": lock_stat,
                    }
                )
            except Exception as e:
                # se una tile è spenta / non valida, la skippiamo
                # (comportamento analogo a HardwareInventory)
                continue

        if found_dac_sr is None:
            raise RuntimeError("FIREQ_SoC: no active DAC tiles found in the RF-DC.")

        dac_sr = found_dac_sr

        # --- ADC Validation ---
        found_adc_sr = None
        adc_tile_specs = []

        for i, tile in enumerate(rf.adc_tiles):
            try:
                lock_stat = getattr(tile, "PLLLockStatus", "Unknown")
                sr_ghz = tile.PLLConfig["SampleRate"]
                sr = float(sr_ghz) * 1e9

                if found_adc_sr is None:
                    found_adc_sr = sr
                elif abs(sr - found_adc_sr) > 1e3:
                    raise RuntimeError(
                        f"FIREQ_SoC: ADC Clock mismatch! "
                        f"Tile {i} has {sr} Hz vs {found_adc_sr} Hz."
                    )

                adc_tile_specs.append(
                    {
                        "tile": i,
                        "sample_rate_hz": sr,
                        "pll_lock": lock_stat,
                    }
                )
            except Exception:
                continue

        if found_adc_sr is None:
            raise RuntimeError("FIREQ_SoC: no active ADC tiles found in the RF-DC.")

        adc_sr = found_adc_sr

        rf_specs = {
            "dac_sr_hz": dac_sr,
            "adc_sr_hz": adc_sr,
            "dac_nyquist_hz": dac_sr / 2.0,
            "adc_nyquist_hz": adc_sr / 2.0,
            "dac_tiles": dac_tile_specs,
            "adc_tiles": adc_tile_specs,
        }

        # ------------------------------------------------------------------
        # AcquistionDrivers: una entry per ciascun IP di readout
        # ------------------------------------------------------------------
        acquisitions_specs = []
        for idx, acq in enumerate(self._readout_ips):
            desc = getattr(acq, "description", {}) or {}
            acq_specs = {
                "index": idx,
                "name": desc.get("name"),
                "fullpath": desc.get("fullpath"),

                "sample_bits": getattr(acq, "SampleSize", None),
                "parallelism": getattr(acq, "NumberOfChannels", None),
                "phase_bits": getattr(acq, "PhaseDepth", None),
                "trigger_word_width": getattr(acq, "TriggerChannels", None),

                "duration_bits": getattr(acq, "DurationWidth", None),
                "max_duration_cycles": getattr(acq, "MaximumDuration", None),

                "time_of_flight_bits": getattr(acq, "TimeOfFlightWidth", None),
                "time_of_flight_max": getattr(acq, "TimeOfFlightMax", None),

                "raw_output_width_bits": getattr(acq, "NDCMT_OutputWidth", None),
                "dec_output_width_bits": getattr(acq, "DCMT_OutputWidth", None),
            }
            acquisitions_specs.append(acq_specs)

        # ------------------------------------------------------------------
        # GeneratorDrivers: una entry per ciascun IP di generazione
        # ------------------------------------------------------------------
        generators_specs = []
        for idx, gen in enumerate(self._generation_ips):
            desc = getattr(gen, "description", {}) or {}
            gen_specs = {
                "index": idx,
                "name": desc.get("name"),
                "fullpath": desc.get("fullpath"),

                "sample_bits": getattr(gen, "SampleSize", None),
                "parallelism": getattr(gen, "NumberOfChannels", None),
                "phase_bits": getattr(gen, "PhaseDepth", None),
                "trigger_word_width": getattr(gen, "TriggerChannels", None),

                "duration_bits": getattr(gen, "DurationWidth", None),
                "max_duration_cycles": getattr(gen, "MaximumDuration", None),

                "sample_mem_addr_bits": getattr(gen, "SampleMemoryAddressWidth", None),
                "sample_mem_depth_words_per_channel": getattr(
                    gen, "ChannelSampleMemoryDepth", None
                ),
                "fractional_precision_bits": getattr(
                    gen, "FractionalPrecision", None
                ),

                "wave_memory_depth": getattr(gen, "WaveMemorySegmentDepth", None),
                "mm_fifo_depth": getattr(
                    gen, "MemoryMappedFifoSegmentDepth", None
                ),
                "axi_full_depth_bytes": getattr(
                    gen, "AxiFullInterfaceDepth", None
                ),
                "total_sample_mem_segment_depth": getattr(
                    gen, "TotalSampleMemorySegmentDepth", None
                ),

                "lfsr_seed_bits": getattr(gen, "SeedLfsrWidth", None),
            }
            generators_specs.append(gen_specs)

        # ------------------------------------------------------------------
        # TriggerGeneratorDrivers: una entry per ciascun IP di trigger
        # ------------------------------------------------------------------
        triggers_specs = []
        for idx, trig in enumerate(self._trigger_ips):
            desc = getattr(trig, "description", {}) or {}
            trig_specs = {
                "index": idx,
                "name": desc.get("name"),
                "fullpath": desc.get("fullpath"),

                "trigger_channels": getattr(trig, "TriggerChannels", None),

                "fifo_interface_mem_depth_bytes": getattr(
                    trig, "FifoInterfaceMemoryDepth", None
                ),
                "channel_fifo_depth_words": getattr(
                    trig, "ChannelFifoDepth", None
                ),
                "fifo_output_width_bits": getattr(
                    trig, "FifoOutputWidth", None
                ),

                "drive_delay_max": getattr(trig, "DriveDelayMax", None),
                "experiment_timer_max": getattr(trig, "ExperimentTimerMax", None),
                "max_hw_repetitions": getattr(trig, "MaxHWRepetitions", None),
            }
            triggers_specs.append(trig_specs)

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        
        adc_parallelism_set = sorted(
            {a["parallelism"] for a in acquisitions_specs if a["parallelism"] is not None}
        )
        dac_parallelism_set = sorted(
            {g["parallelism"] for g in generators_specs if g["parallelism"] is not None}
        )

        trigger_channels_set = sorted(
            {t["trigger_channels"] for t in triggers_specs if t["trigger_channels"] is not None}
        )
        max_hw_reps_list = [
            t["max_hw_repetitions"] for t in triggers_specs
            if t["max_hw_repetitions"] is not None
        ]
        exp_timer_max_list = [
            t["experiment_timer_max"] for t in triggers_specs
            if t["experiment_timer_max"] is not None
        ]

        summary = {
            "dac_sr_hz": rf_specs["dac_sr_hz"],
            "adc_sr_hz": rf_specs["adc_sr_hz"],
            "dac_nyquist_hz": rf_specs["dac_nyquist_hz"],
            "adc_nyquist_hz": rf_specs["adc_nyquist_hz"],
            "adc_parallelism_set": adc_parallelism_set,
            "dac_parallelism_set": dac_parallelism_set,
            # the following are set only if uniform across all IPs
            "adc_parallelism": adc_parallelism_set[0]
            if len(adc_parallelism_set) == 1
            else None,
            "dac_parallelism": dac_parallelism_set[0]
            if len(dac_parallelism_set) == 1
            else None,

            # Trigger summary 
            # if more than one trigger generator IP, the list is aware and maximum timing is not forced to be uniform
            "trigger_channels_set": trigger_channels_set,
            "trigger_channels": trigger_channels_set[0]
                if len(trigger_channels_set) == 1 else None,
            "max_hw_repetitions_min": min(max_hw_reps_list)
                if max_hw_reps_list else None,
            "experiment_timer_max_min": min(exp_timer_max_list)
                if exp_timer_max_list else None,
            
        }

        specs = {
            "rf": rf_specs,
            "acquisitions": acquisitions_specs,
            "generators": generators_specs,
            "triggers": triggers_specs,
            "summary": summary,
        }

        return specs

    # ------------------------------------------------------------------
    # GeneratorIP low-level helpers 
    # ------------------------------------------------------------------

    _WDW_BYTES = 16  # 128-bit per WDW

    def _get_gen(self, gen_index: int) -> GeneratorDriver:
        if gen_index < 0 or gen_index >= len(self._generation_ips):
            raise IndexError(f"gen_index out of range: {gen_index}")
        return self._generation_ips[gen_index]

    def envelope_cache(self, gen_index: int = 0) -> dict:
        """Snapshot of GeneratorDriver envelope cache (name -> meta)."""
        gen = self._get_gen(gen_index)
        return dict(gen.EnvelopeMemoryDict)

    def wave_cache(self, gen_index: int = 0) -> dict:
        """Snapshot of GeneratorDriver wave cache (name/slot -> byte address)."""
        gen = self._get_gen(gen_index)
        return dict(gen.WaveMemoryDict)

    def wave_mem_stats(self, gen_index: int = 0) -> dict:
        """Wave memory usage in bytes/slots, derived from driver counters."""
        gen = self._get_gen(gen_index)
        used_bytes = int(gen.WaveMemoryDict.get("_NEXT", 0))
        total_bytes = int(getattr(gen, "WaveMemorySegmentDepth", 0))
        total_slots = (total_bytes // self._WDW_BYTES) if total_bytes else 0
        used_slots = used_bytes // self._WDW_BYTES
        return {
            "used_bytes": used_bytes,
            "total_bytes": total_bytes,
            "used_slots": used_slots,
            "total_slots": total_slots,
            "free_slots": max(0, total_slots - used_slots),
        }
    

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    # FIREQ IPs

    @property
    def generators(self):
        """List of GeneratorDriver instances."""
        return list(self._generation_ips)

    @property
    def acquisitions(self):
        """List of AcquistionDriver instances."""
        return list(self._readout_ips)

    @property
    def triggers(self):
        """List of TriggerGeneratorDriver instances."""
        return list(self._trigger_ips)

    @property
    def trigger(self):
        """Convenience shortcut: first TriggerGeneratorDriver, if any."""
        return self._trigger_ips[0] if self._trigger_ips else None

    # Infra IPs

    @property
    def rf(self):
        """RF-DC hierarchy (usp_rf_data_converter_0)."""
        return self._rf

    @property
    def axis_switch(self):
        """AXI-Stream switch (axis_switch_0), if present."""
        return self._axis_switch

    @property
    def dma(self):
        """AXI DMA (axi_dma_0), if present."""
        return self._dma

    @property
    def is_healthy(self) -> bool:
        """True if hardware specs were built without errors."""
        return bool(self._healthy)

    def summary(self) -> dict:
        """Return a compact dictionary describing the SoC."""
        return {
            "bitfile": self.bitfile_name,
            "num_generators": self.num_generators,
            "num_acquisitions": self.num_acquisitions,
            "num_triggers": self.num_triggers,
            "has_dma": self.dma is not None,
            "has_axis_switch": self.axis_switch is not None,
            "has_rf": self.rf is not None,
            "hw_specs": self.hw_specs,
        }

def load_fireq(bitfile_name: str, init_clocks: bool = True) -> FIREQ_SoC:
    """Helper per creare e inizializzare un FIREQ_SoC."""
    return FIREQ_SoC(bitfile_name, ignore_version=False, init_clocks=init_clocks)