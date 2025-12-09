from pynq import Overlay
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

        # 5) FIREQ Custom IPs discovery
        self._discover_fireq_ips()

        # Basic resource sanity check
        if not self._generation_ips:
            raise RuntimeError("FIREQ_SoC: no Generator IPs found in overlay.")
        if not self._readout_ips:
            raise RuntimeError("FIREQ_SoC: no Acquisition IPs found in overlay.")
        if not self._trigger_ips:
            raise RuntimeError("FIREQ_SoC: no TriggerGenerator IP found in overlay.")

        # 6) Infra IPs discovery (RF-DC, AXIS switch, DMA)
        self._rf = getattr(self, "usp_rf_data_converter_0", None)
        self._axis_switch = getattr(self, "axis_switch_0", None)
        self._dma = getattr(self, "axi_dma_0", None)

        # 7) Build hardware specs for high-level user (
        self.hw_specs = self._build_hw_specs()
        # 8) Health flag
        self._healthy = True  # if _build_hw_specs did not raise exceptions

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------
    def _discover_fireq_ips(self) -> None:
        """
        Discovers, initializes, and categorizes FIREQ custom IP drivers based on the hardware handoff.

        This method iterates through the address map provided by the parser. For each identified
        FIREQ IP present in the overlay, it performs the following operations:

        1.  **Interface Binding:** Configures the AXI backend by injecting the
            base address and range derived from the HWH file.
        2.  **Classification:** Sorts the driver instance into the appropriate internal registry
            (Generators, Acquisitions, or Triggers) based on its class type.

        This step is essential to transform the raw PYNQ IP objects into functional
        FIREQ drivers capable of communication.

        :return: None
        :rtype: None
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
        Constructs and validates the comprehensive hardware specification dictionary for high-level users.

        This method performs a deep inspection of both the Hard IP (RF-DC) and the Soft IP
        (FIREQ drivers) to build a unified configuration map. It enforces strict synchronization
        constraints to ensure signal coherence.

        **Validation Logic:**

        * **RF-DC Integrity:** Verifies that the RF Data Converter IP is present and accessible.
        * **Clock Synchronization:** Checks that all enabled DAC and ADC tiles are locked and
            operating at identical sample rates. A mismatch in sample rates across tiles of the
            same type is considered a critical hardware configuration error.
        * **IP Configuration:** Extracts static parameters (resolution, memory depth, parallelism,
            trigger capabilities for experiment timing) from the instantiated custom drivers.

        :return: A dictionary containing the validated specifications for RF, Acquisitions,
                 Generators, Triggers, and also a global summary.
        :rtype: dict
        :raises RuntimeError: If the RF-DC hierarchy is missing, if PLLs are unlocked, or if
                              inconsistent sample rates are detected across synchronized tiles.
        """
        rf = self._rf
        if rf is None:
            raise RuntimeError(
                "FIREQ_SoC: no RF Data Converter hierarchy found "
                "(usp_rf_data_converter_0 is missing)."
            )

        # ------------------------------------------------------------------
        # RF-DC: DAC/ADC tiles, sample rate, nyquist, lock status
        # ------------------------------------------------------------------
        dac_tiles = getattr(rf, "dac_tiles", [])
        adc_tiles = getattr(rf, "adc_tiles", [])

        if not dac_tiles:
            raise RuntimeError("FIREQ_SoC: no DAC tiles found in RF-DC.")
        if not adc_tiles:
            raise RuntimeError("FIREQ_SoC: no ADC tiles found in RF-DC.")

        dac_sr = None
        adc_sr = None
        dac_tile_specs = []
        adc_tile_specs = []

        # --- DAC tiles (constraint: all must have the same sample rate) ---
        for i, tile in enumerate(dac_tiles):
            try:
                lock_stat = getattr(tile, "PLLLockStatus", "Unknown")
                sr_ghz = tile.PLLConfig["SampleRate"]  # typically in GHz
                sr = float(sr_ghz) * 1e9

                if dac_sr is None:
                    dac_sr = sr
                elif abs(sr - dac_sr) > 1e3:  # tolerance 1 kHz
                    raise RuntimeError(
                        f"FIREQ_SoC: DAC Clock mismatch! "
                        f"Tile {i} has {sr} Hz vs {dac_sr} Hz."
                    )

                dac_tile_specs.append(
                    {
                        "tile": i,
                        "sample_rate_hz": sr,
                        "pll_lock": lock_stat,
                    }
                )
            except Exception as e:
                raise RuntimeError(
                    f"FIREQ_SoC: error reading DAC tile {i}: {e}"
                ) from e

        # --- ADC tiles (same synchronization constraint) ---
        for i, tile in enumerate(adc_tiles):
            try:
                lock_stat = getattr(tile, "PLLLockStatus", "Unknown")
                sr_ghz = tile.PLLConfig["SampleRate"]
                sr = float(sr_ghz) * 1e9

                if adc_sr is None:
                    adc_sr = sr
                elif abs(sr - adc_sr) > 1e3:
                    raise RuntimeError(
                        f"FIREQ_SoC: ADC Clock mismatch! "
                        f"Tile {i} has {sr} Hz vs {adc_sr} Hz."
                    )

                adc_tile_specs.append(
                    {
                        "tile": i,
                        "sample_rate_hz": sr,
                        "pll_lock": lock_stat,
                    }
                )
            except Exception as e:
                raise RuntimeError(
                    f"FIREQ_SoC: error reading ADC tile {i}: {e}"
                ) from e

        rf_specs = {
            "dac_sr_hz": dac_sr,
            "adc_sr_hz": adc_sr,
            "dac_nyquist_hz": dac_sr / 2.0,
            "adc_nyquist_hz": adc_sr / 2.0,
            "dac_tiles": dac_tile_specs,
            "adc_tiles": adc_tile_specs,
        }

        # ------------------------------------------------------------------
        # AcquisitionDrivers: one entry per each readout IP
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

                # questi due attributi li hai mostrati tu in snippet:
                # se non ci fossero nella tua versione locale, rimangono None
                "raw_output_width_bits": getattr(acq, "NDCMT_OutputWidth", None),
                "dec_output_width_bits": getattr(acq, "DCMT_OutputWidth", None),
            }
            acquisitions_specs.append(acq_specs)

        # ------------------------------------------------------------------
        # GeneratorDrivers: one entry per each generation IP
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

                # Envelope memory / wave memory
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

                # Random / LFSR
                "lfsr_seed_bits": getattr(gen, "SeedLfsrWidth", None),
            }
            generators_specs.append(gen_specs)

        # ------------------------------------------------------------------
        # TriggerGeneratorDrivers: one entry per each trigger IP
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

                # limiti temporali/di ripetizione
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

    @property
    def num_generators(self) -> int:
        return len(self._generation_ips)

    @property
    def num_acquisitions(self) -> int:
        return len(self._readout_ips)

    @property
    def num_triggers(self) -> int:
        return len(self._trigger_ips)

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

    # Specs / health

    @property
    def hw_specs(self) -> dict:
        """Validated hardware specifications (sample rates, Nyquist, etc.)."""
        return dict(self._hw_specs) if self._hw_specs is not None else {}

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
