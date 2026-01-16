from pynq import Overlay, PL
import xrfdc
import xrfclk
import os
from typing import Dict, Tuple
import re
from ._fireq_parser import FIREQ_parser
from ._utils import _FIREQDriver
from .acquisition_driver import AcquisitionDriver
from .generator_driver import GeneratorDriver
from .trigger_generator_driver import TriggerGeneratorDriver

__all__ = ["FIREQ_SoC"]


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
            self._init_rf_clks()

        # 4) FIREQ IPs lists
        self._generation_ips = []  # type: list[GeneratorDriver]
        self._acquistion_ips = []  # type: list[AcquisitionDriver]
        self._trigger_ip = []  # type: list[TriggerGeneratorDriver]
        self._GEN_RF_MAP: Dict[int, Dict[str, Tuple[int, int]]] = {}
        self._ACQ_RF_MAP: Dict[int, Tuple[int, int]] = {}
        self._cached_nyquist_zone: Dict[tuple, int] = {}

        # 5) Low-level discovery (bind AXI + classify IPs)
        self._rf = getattr(self, "usp_rf_data_converter_0", None)
        self._axis_switch = getattr(self, "axis_switch_0", None)
        self._dma = getattr(self, "axi_dma_0", None)

        self._discover_fireq_ips()

        if self._rf:
            self._map_rf_topology()
        # Basic resource sanity check
        if not self._generation_ips:
            raise RuntimeError("FIREQ_SoC: no Generator IPs found in overlay.")
        if not self._acquistion_ips:
            raise RuntimeError("FIREQ_SoC: no Acquisition IPs found in overlay.")
        if not self._trigger_ip:
            raise RuntimeError("FIREQ_SoC: no TriggerGenerator IP found in overlay.")

        # 6) Hardware specs (clock validation, sample rates etc.)
        self.num_generators = len(self._generation_ips)
        self.num_acquisitions = len(self._acquistion_ips)
        self.num_triggers = len(self._trigger_ip)

        self.hw_specs = self._build_hw_specs()

        # 7) Health flag
        self._healthy = True  # if _build_hw_specs did not raise exceptions
        # 8) recomended PL reset
        PL.reset()

    @staticmethod
    def _init_rf_clks(lmk_freq: float = 245.76, lmx_freq: float = 491.52) -> None:
        """
        Initialise the LMK and LMX clocks for the RF-DC hierarchy.

        The radio clocks are required to talk to the RF-DCs and only need
        to be initialised once per session.
        """
        xrfclk.set_ref_clks(lmk_freq=lmk_freq, lmx_freq=lmx_freq)

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------
    def _discover_fireq_ips(self) -> None:
        """
        Use FIREQ_parser.GetAddressMapping() to:
        - Bind AXI interfaces on FIREQ IPs.
        - Populate IP lists: _generation_ips, _readout_ips, _trigger_ip.
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
            elif isinstance(ip_object, AcquisitionDriver):
                self._acquistion_ips.append(ip_object)
            elif isinstance(ip_object, TriggerGeneratorDriver):
                self._trigger_ip.append(ip_object)

    def _map_rf_topology(self) -> None:
        """
        Derive the physical RF connections (Tile/Block) for Generators and Acquisitions
        by traversing the AXI-Stream connectivity graph.
        """
        all_modules = list(self._FIREQ_parser._Modules)
        rfdc_name = "usp_rf_data_converter_0"

        # ---------------------------------------------------------------------
        # 1. Map Generators (DAC Path)
        # ---------------------------------------------------------------------
        for idx, gen in enumerate(self._generation_ips):
            gen_fullpath = getattr(gen, "_fullpath", "")
            if not gen_fullpath:
                continue

            gen_instance_name = gen_fullpath.split("/")[0]
            gen_xml = self._FIREQ_parser._GetModule(gen_instance_name)
            if not gen_xml:
                continue

            conn = self._FIREQ_parser.GetConnectivity(gen_xml, all_modules)

            def find_rfdc_sink(node_dict):
                if node_dict["NODE"] == rfdc_name:
                    return node_dict["BUS_M/S"][1], node_dict["BUS_M/S"][0]
                for child in node_dict.get("CHILDREN", []):
                    res = find_rfdc_sink(child)
                    if res:
                        return res
                return None

            res = find_rfdc_sink(conn)

            if res:
                rfdc_port, gen_port_alias = res
                # DAC uses sXY_axis (slave ports)
                m = re.match(r"s(\d)(\d)_axis", rfdc_port)
                if m:
                    dac_global_index = int(m.group(1)) * 10 + int(m.group(2))
                    # Dual DAC tile: 2 blocks per tile
                    tile = dac_global_index // 2
                    block = dac_global_index % 2

                    label = "drive" if "m0" in gen_port_alias or "drive" in gen_port_alias else "readout"

                    if idx not in self._GEN_RF_MAP:
                        self._GEN_RF_MAP[idx] = {}
                    self._GEN_RF_MAP[idx][label] = (tile, block)

        # ---------------------------------------------------------------------
        # 2. Map Acquisitions (ADC Path)
        # ---------------------------------------------------------------------
        rfdc_xml = self._FIREQ_parser._GetModule(rfdc_name)
        if rfdc_xml:
            conn_rfdc = self._FIREQ_parser.GetConnectivity(rfdc_xml, all_modules)

            def trace_rfdc_source(node_dict, source_port_name, at_root=True):
                if at_root and node_dict["NODE"] == rfdc_name:
                    for child in node_dict["CHILDREN"]:
                        if child["BUS_M/S"][0] == source_port_name:
                            return trace_rfdc_source(child, source_port_name, at_root=False)
                    return None

                curr_name = node_dict["NODE"]
                for idx, acq in enumerate(self._acquistion_ips):
                    acq_fullpath = getattr(acq, "_fullpath", "")
                    if acq_fullpath.split("/")[0] == curr_name:
                        return idx

                for child in node_dict.get("CHILDREN", []):
                    res = trace_rfdc_source(child, source_port_name, at_root=False)
                    if res is not None:
                        return res
                return None

            rfdc_interfaces = self._FIREQ_parser._GetBusInterfaces(rfdc_xml)

            for bus_name, attribs in rfdc_interfaces.items():
                if attribs["TYPE"] in ["MASTER", "INITIATOR"]:
                    port_name = attribs["NAME"]
                    # ADC uses mXY_axis (master ports)
                    m = re.match(r"m(\d)(\d)_axis", port_name)
                    if m:
                        # Dual ADC tile: 2 blocks per tile
                        tile = int(m.group(1))
                        port_indicator = int(m.group(2))
                        block = port_indicator // 2

                        acq_idx = trace_rfdc_source(conn_rfdc, port_name)

                        if acq_idx is not None:
                            self._ACQ_RF_MAP[acq_idx] = (tile, block)

    def _get_fifo_depth(self, *, acq_inst: str, mode: str) -> int:
        """
        FIFO depth extraction based on  HWH structure:

        axisAcquistionIP_X (m00_axis or m01_axis)
            -> axis_register_slice (S_AXIS shares BUSNAME)
            -> axis_data_fifo (S_AXIS shares BUSNAME of regslice M_AXIS)
            -> read FIFO_DEPTH (or C_FIFO_DEPTH)

        mode: "raw" or "decimated"
        """
        # 1) Map mode -> which Acq output port we follow
        #    (Swap these two if your design uses the opposite mapping)
        mode_to_port = {
            "decimated": "m01_axis",
            "raw": "m00_axis",
        }
        if mode not in mode_to_port:
            raise ValueError(f"Unknown mode={mode}. Expected one of {list(mode_to_port)}")

        port_name = mode_to_port[mode]

        # 2) Get Acq module XML and its BUSNAME for that port
        acq_mod = self._FIREQ_parser._GetModule(acq_inst)
        if acq_mod is None:
            return -1

        acq_busifs = self._FIREQ_parser._GetBusInterfaces(acq_mod)  # dict: busname -> attribs
        start_busname = None
        for busname, a in acq_busifs.items():
            if a.get("NAME") == port_name:
                start_busname = busname
                break
        if not start_busname:
            return -1

        # 3) Find the register slice whose S_AXIS shares that BUSNAME
        regslice_inst = None
        regslice_mod = None
        for m in self._FIREQ_parser._Modules:
            inst = m.attrib.get("INSTANCE", "")
            vlnv = (m.attrib.get("VLNV", "") or "").lower()
            if "axis_register_slice" not in vlnv:
                continue

            busifs = self._FIREQ_parser._GetBusInterfaces(m)
            # we want the S_AXIS endpoint bound to the same BUSNAME
            for busname, a in busifs.items():
                if busname == start_busname and a.get("NAME") == "S_AXIS":
                    regslice_inst = inst
                    regslice_mod = m
                    break
            if regslice_inst:
                break

        if not regslice_inst:
            return -1

        # 4) From that register slice, take its M_AXIS BUSNAME
        regslice_busifs = self._FIREQ_parser._GetBusInterfaces(regslice_mod)
        next_busname = None
        for busname, a in regslice_busifs.items():
            if a.get("NAME") == "M_AXIS":
                next_busname = busname
                break
        if not next_busname:
            return -1

        # 5) Find the axis_data_fifo whose S_AXIS shares next_busname
        fifo_inst = None
        for m in self._FIREQ_parser._Modules:
            inst = m.attrib.get("INSTANCE", "")
            vlnv = (m.attrib.get("VLNV", "") or "").lower()
            if "axis_data_fifo" not in vlnv:
                continue

            busifs = self._FIREQ_parser._GetBusInterfaces(m)
            for busname, a in busifs.items():
                if busname == next_busname and a.get("NAME") == "S_AXIS":
                    fifo_inst = inst
                    break
            if fifo_inst:
                break

        if not fifo_inst:
            return -1

        # 6) Read FIFO depth parameter (Vivado sometimes uses FIFO_DEPTH or C_FIFO_DEPTH)
        depth = self._FIREQ_parser._GetParameter(fifo_inst, "FIFO_DEPTH") or self._FIREQ_parser._GetParameter(
            fifo_inst, "C_FIFO_DEPTH"
        )
        try:
            return int(depth)
        except (TypeError, ValueError):
            return -1

    # ------------------------------------------------------------------
    # Hardware specs builder
    # ------------------------------------------------------------------
    def _build_hw_specs(self) -> dict:
        """
        Build a validated dictionary of hardware specifications: scalability aware.

        Return structure (high level):

            {
              "rf": { ... },              # DAC/ADC tiles, sample rate, nyquist, ecc.
              "acquisitions": [ {...} ],  # one per AcquisitionDriver
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
            raise RuntimeError("FIREQ_SoC: no RF Data Converter hierarchy found (usp_rf_data_converter_0 is missing).")

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
                    raise RuntimeError(f"FIREQ_SoC: DAC Clock mismatch! Tile {i} has {sr} Hz vs {found_dac_sr} Hz.")

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
                    raise RuntimeError(f"FIREQ_SoC: ADC Clock mismatch! Tile {i} has {sr} Hz vs {found_adc_sr} Hz.")

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
        # AcquisitionDrivers: una entry per ciascun IP di readout
        # ------------------------------------------------------------------
        acquisitions_specs = []
        for idx, acq in enumerate(self._acquistion_ips):
            desc = getattr(acq, "description", {}) or {}
            acq_inst = f"axisAcquistionIP_{idx}"

            d_dec = self._get_fifo_depth(acq_inst=acq_inst, mode="decimated")
            d_raw = self._get_fifo_depth(acq_inst=acq_inst, mode="raw")

            acq_specs = {
                "index": idx,
                "name": desc.get("name"),
                "_fullpath": desc.get("_fullpath"),
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
                "raw_fifo_depth_words": d_raw,
                "decimated_fifo_depth_words": d_dec,
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
                "_fullpath": desc.get("_fullpath"),
                "sample_bits": getattr(gen, "SampleSize", None),
                "parallelism": getattr(gen, "NumberOfChannels", None),
                "phase_bits": getattr(gen, "PhaseDepth", None),
                "trigger_word_width": getattr(gen, "TriggerChannels", None),
                "duration_bits": getattr(gen, "DurationWidth", None),
                "max_duration_cycles": getattr(gen, "MaximumDuration", None),
                "sample_mem_addr_bits": getattr(gen, "SampleMemoryAddressWidth", None),
                "sample_mem_depth_words_per_channel": getattr(gen, "ChannelSampleMemoryDepth", None),
                "fractional_precision_bits": getattr(gen, "FractionalPrecision", None),
                "wave_memory_depth": getattr(gen, "WaveMemorySegmentDepth", None),
                "mm_fifo_depth": getattr(gen, "MemoryMappedFifoSegmentDepth", None),
                "axi_full_depth_bytes": getattr(gen, "AxiFullInterfaceDepth", None),
                "total_sample_mem_segment_depth": getattr(gen, "TotalSampleMemorySegmentDepth", None),
                "lfsr_seed_bits": getattr(gen, "SeedLfsrWidth", None),
            }
            generators_specs.append(gen_specs)

        # ------------------------------------------------------------------
        # TriggerGeneratorDrivers: una entry per ciascun IP di trigger
        # ------------------------------------------------------------------
        triggers_specs = []
        for idx, trig in enumerate(self._trigger_ip):
            desc = getattr(trig, "description", {}) or {}
            trig_specs = {
                "index": idx,
                "name": desc.get("name"),
                "_fullpath": desc.get("_fullpath"),
                "trigger_channels": getattr(trig, "TriggerChannels", None),
                "fifo_interface_mem_depth_bytes": getattr(trig, "FifoInterfaceMemoryDepth", None),
                "channel_fifo_depth_words": getattr(trig, "ChannelFifoDepth", None),
                "fifo_output_width_bits": getattr(trig, "FifoOutputWidth", None),
                "drive_delay_max": getattr(trig, "DriveDelayMax", None),
                "experiment_timer_max": getattr(trig, "ExperimentTimerMax", None),
                "max_hw_repetitions": getattr(trig, "MaxHWRepetitions", None),
            }
            triggers_specs.append(trig_specs)

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------

        adc_parallelism_set = sorted({a["parallelism"] for a in acquisitions_specs if a["parallelism"] is not None})
        dac_parallelism_set = sorted({g["parallelism"] for g in generators_specs if g["parallelism"] is not None})

        trigger_channels_set = sorted(
            {t["trigger_channels"] for t in triggers_specs if t["trigger_channels"] is not None}
        )
        max_hw_reps_list = [t["max_hw_repetitions"] for t in triggers_specs if t["max_hw_repetitions"] is not None]
        exp_timer_max_list = [
            t["experiment_timer_max"] for t in triggers_specs if t["experiment_timer_max"] is not None
        ]

        summary = {
            "dac_sr_hz": rf_specs["dac_sr_hz"],
            "adc_sr_hz": rf_specs["adc_sr_hz"],
            "dac_nyquist_hz": rf_specs["dac_nyquist_hz"],
            "adc_nyquist_hz": rf_specs["adc_nyquist_hz"],
            "adc_parallelism_set": adc_parallelism_set,
            "dac_parallelism_set": dac_parallelism_set,
            # the following are set only if uniform across all IPs
            "adc_parallelism": adc_parallelism_set[0] if len(adc_parallelism_set) == 1 else None,
            "dac_parallelism": dac_parallelism_set[0] if len(dac_parallelism_set) == 1 else None,
            # Trigger summary
            # if more than one trigger generator IP, the list is aware and maximum timing is not forced to be uniform
            "trigger_channels_set": trigger_channels_set,
            "trigger_channels": trigger_channels_set[0] if len(trigger_channels_set) == 1 else None,
            "max_hw_repetitions_min": min(max_hw_reps_list) if max_hw_reps_list else None,
            "experiment_timer_max_min": min(exp_timer_max_list) if exp_timer_max_list else None,
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
    def configure_dac_mix_mode(self, gen_index: int, label: str, freq_mhz: float) -> dict:
        """
        Configure RF-DC NyquistZone for a generator label based on frequency.

        Automatically determines if mixing mode is needed (even Nyquist zones)
        and configures the appropriate DAC tile/block.

        :param gen_index: Generator index
        :param label: 'drive' or 'readout'
        :param freq_mhz: Target frequency in MHz
        :return: Dict with zone info
        :raises ValueError: If gen_index or label not in mapping
        """
        if self._rf is None:
            return {"status": "skipped", "reason": "no_rf_dc"}

        # Lookup tile/block
        gen_map = self._GEN_RF_MAP.get(gen_index)
        if gen_map is None:
            raise ValueError(f"No RF mapping for gen_index={gen_index}")

        tile_block = gen_map.get(label)
        if tile_block is None:
            raise ValueError(f"No RF mapping for label='{label}' on gen={gen_index}")

        tile, block = tile_block

        # Compute Nyquist zone
        dac_nyquist_hz = self.hw_specs["summary"]["dac_nyquist_hz"]
        freq_hz = freq_mhz * 1e6
        nyquist_zone = max(1, int(freq_hz / dac_nyquist_hz) + 1)

        # AMD convention: Odd zones → 1, Even zones → 2
        amd_zone = 1 if nyquist_zone % 2 == 1 else 2

        cache_key = (tile, block)
        changed = False

        if self._cached_nyquist_zone.get(cache_key) != amd_zone:
            self._rf.dac_tiles[tile].blocks[block].NyquistZone = amd_zone
            self._cached_nyquist_zone[cache_key] = amd_zone
            changed = True

        return {
            "gen_index": gen_index,
            "label": label,
            "freq_mhz": freq_mhz,
            "nyquist_zone": nyquist_zone,
            "amd_zone": amd_zone,
            "tile": tile,
            "block": block,
            "changed": changed,
        }

    # FIREQ IPs

    def configure_adc_mix_mode(self, acq_index: int, freq_mhz: float) -> dict:
        """
        Configure RF-DC NyquistZone for an ADC channel based on frequency.

        Automatically determines if mixing mode is needed (even Nyquist zones)
        and configures the appropriate ADC tile/block.

        :param acq_index: Acquisition IP index
        :param freq_mhz: Demodulation frequency in MHz
        :return: Dict with zone info
        :raises ValueError: If acq_index not in mapping
        """
        if self._rf is None:
            return {"status": "skipped", "reason": "no_rf_dc"}

        # Lookup tile/block
        tile_block = self._ACQ_RF_MAP.get(acq_index)
        if tile_block is None:
            raise ValueError(f"No RF mapping for acq_index={acq_index}")

        tile, block = tile_block

        # Compute Nyquist zone
        adc_nyquist_hz = self.hw_specs["summary"]["adc_nyquist_hz"]
        freq_hz = freq_mhz * 1e6
        nyquist_zone = max(1, int(freq_hz / adc_nyquist_hz) + 1)

        # AMD convention: Odd zones → 1, Even zones → 2
        amd_zone = 1 if nyquist_zone % 2 == 1 else 2

        cache_key = ("adc", tile, block)  # prefix to avoid collision with DAC cache
        changed = False

        if self._cached_nyquist_zone.get(cache_key) != amd_zone:
            self._rf.adc_tiles[tile].blocks[block].NyquistZone = amd_zone
            self._cached_nyquist_zone[cache_key] = amd_zone
            changed = True

        return {
            "acq_index": acq_index,
            "freq_mhz": freq_mhz,
            "nyquist_zone": nyquist_zone,
            "amd_zone": amd_zone,
            "tile": tile,
            "block": block,
            "changed": changed,
        }

    @property
    def generators(self):
        """List of GeneratorDriver instances."""
        return list(self._generation_ips)

    @property
    def acquisitions(self):
        """List of AcquisitionDriver instances."""
        return list(self._acquistion_ips)

    @property
    def trigger(self):
        """Convenience shortcut: TriggerGeneratorDriver, if any."""
        return self._trigger_ip[0]

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
