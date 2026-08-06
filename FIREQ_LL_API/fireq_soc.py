"""Low-level FIREQ SoC overlay support and discovery helpers."""

import logging
import os
import re
import time

import xrfclk
import xrfdc  # noqa: F401
from pynq import PL, Overlay

from ._fireq_parser import FireqParser
from ._utils import _FIREQDriver
from .acquisition_driver import AcquisitionDriver
from .fifo_wrapper import FIFOWrapper
from .generator_driver import GeneratorDriver
from .trigger_generator_driver import TriggerGeneratorDriver

__all__ = ["FIREQSoC", "load_fireq"]


class FIREQSoC(Overlay):
    """Low-level representation of the FIREQ SoC.

    Responsibilities:
    - Load the bitfile.
    - Initialise RF clocks (optional).
    - Use FireqParser on the HWH to:

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
    ) -> None:
        """Initialize the FIREQ SoC overlay.

        :param bitfile_name: Path to the .bit file
        :type bitfile_name: str
        :param ignore_version: Whether to ignore the bitfile version check, defaults to
            False
        :type ignore_version: bool
        :param init_clocks: Whether to initialize RF clocks on startup, defaults to True
        :type init_clocks: bool
        :raises RuntimeError: If overlay creation fails
        """
        # set up logger
        self.log = logging.getLogger(__name__)

        # reset the PL server, clears bugged pynq caches that could lead to issues
        PL.reset()

        # Load overlay
        try:
            self.log.debug("Creating overlay from %s", bitfile_name)
            super().__init__(bitfile_name, ignore_version=ignore_version)
        except Exception as e:
            # better to raise an exception: server aware of the problem
            self.log.error("Error during overlay creation: %s", e)
            raise RuntimeError(f"FIREQ: error during overlay creation: {e}") from e

        # HWH + parser
        self._fireq_hwh_file = os.path.splitext(self.bitfile_name)[0] + ".hwh"
        self._fireq_parser = FireqParser(self._fireq_hwh_file)

        # RF clocks initialisation
        if init_clocks:
            self._init_rf_clks()

        # init the axi interfaces of FIREQ drivers
        self._init_fireq_ips()

        # Organize ALL IPs in the design as (instance (str), ip_object, type(ip_object).__name__)
        self.ips = {}
        self._discover_ips()

        # Check the existance of FIREQ ips in the design
        self._check_fireq_ips()

        # Find the rfdc IP
        self.rfdc = None
        self.active_adcs = None
        self.active_dacs = None
        self._discover_rfdc()

        # Find the clocks
        self.dac_samplerate = None
        self.adc_samplerate = None
        self.fabric_frequency = None
        self._discover_clocks()
        self.log.debug(f"DAC sampling rate is: {self.dac_samplerate} MHz")
        self.log.debug(f"ADC sampling rate is: {self.adc_samplerate} MHz")
        self.log.debug(f"Fabric clock is: {self.fabric_frequency} MHz")

        # freeze calibration for all ADCs
        for adc_index in self.active_adcs:
            self.set_adc_autocalibration_status(adc_index, freeze=True)

    def set_logger(self, new_logger: logging.Logger) -> None:
        """Set the logger for this object.

        :param new_logger: Logger object to use
        :type new_logger: logging.Logger
        """
        self.log = new_logger

    def _init_rf_clks(self, lmk_freq: float = 245.76, lmx_freq: float = 491.52) -> None:
        """Initialise the LMK and LMX clocks for the RF-DC hierarchy.

        The radio clocks are required to talk to the RF-DCs and only need to be
        initialised once per session.

        :param lmk_freq: Frequency of the LMK clock in MHz, defaults to 245.76
        :type lmk_freq: float
        :param lmx_freq: Frequency of the LMX clock in MHz, defaults to 491.52
        :type lmx_freq: float
        """
        self.log.debug("Initialising RF clocks: LMK=%s MHz, LMX=%s MHz", lmk_freq, lmx_freq)
        xrfclk.set_ref_clks(lmk_freq=lmk_freq, lmx_freq=lmx_freq)

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------
    def _check_fireq_ips(self) -> None:
        """Check that the design contains the necessary FIREQ IPs to conduct experiments."""
        if len(self.ips) == 0:
            self.log.error("No FIREQ IPs found in the design.")
            raise RuntimeError("No FIREQ IPs found in the design.")
        gen_count = 0
        acq_count = 0
        ctrl_count = 0
        for ip in self.ips.values():
            if ip[2] == GeneratorDriver.__name__:
                gen_count += 1
            elif ip[2] == AcquisitionDriver.__name__:
                acq_count += 1
            elif ip[2] == TriggerGeneratorDriver.__name__:
                ctrl_count += 1
        self.log.debug(
            "Found %s generators, %s acquisitions and %s trigger generators", gen_count, acq_count, ctrl_count
        )
        if gen_count == 0 or acq_count == 0 or ctrl_count == 0:
            self.log.error("The design does not contain the necessary FIREQ IPs to conduct experiments.")
            raise RuntimeError("The design does not contain the necessary FIREQ IPs to conduct experiments.")
        if ctrl_count > 1:
            self.log.error("Only one control IP (TriggerGenerator) is allowed in the design at this time")
            raise NotImplementedError("Only one control IP (TriggerGenerator) is allowed in the design at this time")

    def _init_fireq_ips(self) -> None:
        """Initialize the axi interfaces for FIREQ IPs.

        Some FIREQ IPs have two axi4 interfaces, one full and one lite.
        PYNQ does not support this, so we need to manually set the axi4 interfaces.
        """
        self.log.debug("Initialising FIREQ IPs")
        mmap = self._fireq_parser.get_address_mapping()

        # check that the ps name is the mapping, otherwise raise an error
        if self._fireq_parser.ps_name not in mmap.keys():
            self.log.error("PS name %s not found in memory map.", self._fireq_parser.ps_name)
            raise RuntimeError(f"PS name {self._fireq_parser.ps_name} not found in memory map.")

        for axi_map in mmap[self._fireq_parser.ps_name]:
            if not hasattr(self, axi_map["INSTANCE"]):
                continue

            ip_object = getattr(self, axi_map["INSTANCE"])
            # Only consider FIREQ low-level drivers
            if not isinstance(ip_object, _FIREQDriver):
                continue

            # Init AXI based on mapping
            self.log.debug("Initialising AXI interfaces for %s", axi_map["INSTANCE"])
            axi_base = int(axi_map["BASEVALUE"], 16)
            axi_range = int(axi_map["HIGHVALUE"], 16) - axi_base + 1
            if axi_map["SLAVEBUSINTERFACE"] == "s00_axi":
                ip_object.init_axi_full_interface(axi_base, axi_range)
            elif axi_map["SLAVEBUSINTERFACE"] == "s01_axi":
                ip_object.init_axi_lite_interface(axi_base, axi_range)

        # find and initialize the FIFOs
        for node, data in self._fireq_parser.system_graph.nodes(data=True):
            if data["vlnv"] in FIFOWrapper.bindto:
                self.log.debug("Initialising FIFO %s", data["instance"])
                fifo = FIFOWrapper(self._fireq_parser.get_module_parameters(node))
                setattr(self, data["instance"], fifo)

    def _discover_ips(self) -> None:
        """Build the IP lists."""
        # go through the system graph and find all addressable ips
        for node in self._fireq_parser.system_graph:
            instance = self._fireq_parser.system_graph.nodes[node]["instance"]
            # continue if not an attribute or if the name is the ps name, because pynq crashes
            if instance is self._fireq_parser.ps_name or not hasattr(self, instance):
                continue
            self.log.debug("Found IP %s", instance)
            ip_object = getattr(self, instance)
            # add the ip to the dictionary, by storing the instance name as the key and the type as the value
            self.ips[node] = (instance, ip_object, type(ip_object).__name__)

    def _discover_rfdc(self) -> None:
        """Discover the RF-DC IP and initialize the clocks."""
        # find the rfdc ip in the system graph
        for ip in self.ips.values():
            if ip[2] == "RFdc":
                self.log.debug("Found RF-DC IP %s", ip[0])
                self.rfdc = ip[1]
                break

        # if the rfdc is not found, assume debug overlay and set the sample rates to 1 and 2 GSps
        if self.rfdc is None:
            self.log.warning("RF-DC IP not found, assuming debug overlay")
            return

        # if the rfdc is found, extract the active ADCs and DACs from the rfdc object
        active_adcs = []
        active_dacs = []
        for tile_id, tile in enumerate(self.rfdc.adc_tiles):
            for block_id, block in enumerate(tile.blocks):
                try:
                    _ = block.BlockStatus
                    active_adcs.append((tile_id, block_id))
                except Exception:
                    continue
        for tile_id, tile in enumerate(self.rfdc.dac_tiles):
            for block_id, block in enumerate(tile.blocks):
                try:
                    _ = block.BlockStatus
                    active_dacs.append((tile_id, block_id))
                except Exception:
                    continue
        self.active_adcs = active_adcs
        self.active_dacs = active_dacs
        self.log.debug("Active ADCs: %s", self.active_adcs)
        self.log.debug("Active DACs: %s", self.active_dacs)

    def _discover_clocks(self) -> None:
        """Discover the clocks and set the sample rates.

        The FIREQ system is defined by 3 main clocks:
        - the fabric clock, which is the clock used by IPs
        - the DAC sampling rate
        - the ADC sampling rate
        """
        # go through the ips, find generators and acquisition and calculate the sampling frequency
        # sampling frequency is the fabric frequency times the number of channels
        gen_sr = []
        acq_sr = []
        fabric_frequency = []
        for full_name, ip in self.ips.items():
            # add the fabric frequency:
            if ip[2] in [GeneratorDriver.__name__, AcquisitionDriver.__name__, TriggerGeneratorDriver.__name__]:
                fabric_frequency.append(self.get_ip_frequency(full_name, ip[1].fabric_clock_port))
            # add the sr
            if ip[2] == GeneratorDriver.__name__:
                gen_sr.append(fabric_frequency[-1] * ip[1].number_of_channels)
            elif ip[2] == AcquisitionDriver.__name__:
                acq_sr.append(fabric_frequency[-1] * ip[1].number_of_channels)
        # check that all fabric frequencies are the same
        if len(set(fabric_frequency)) > 1:
            self.log.error("IPs have different fabric frequencies")
            raise NotImplementedError("IPs have different fabric frequencies")
        # check that all the generators have the same sampling frequency
        if len(set(gen_sr)) > 1:
            self.log.error("Generators have different sampling frequencies")
            raise RuntimeError("Generators have different sampling frequencies")
        # check that all the acquisitions have the same sampling frequency
        if len(set(acq_sr)) > 1:
            self.log.error("Acquisitions have different sampling frequencies")
            raise RuntimeError("Acquisitions have different sampling frequencies")
        # set the fabric and sampling frequencies in MHz
        self.fabric_frequency = fabric_frequency[0] / 1e6
        self.dac_samplerate = gen_sr[0] / 1e6
        self.adc_samplerate = acq_sr[0] / 1e6

        # set the DAC to ADC ratio for the acquisition drivers
        for full_name, ip in self.ips.items():
            if ip[2] == AcquisitionDriver.__name__:
                ip[1].set_dac_to_adc_ratio(int(self.dac_samplerate // self.adc_samplerate))

    # ------------------------------------------------------------------
    # IP helpers
    # ------------------------------------------------------------------

    def get_ip_frequency(self, full_ip_name: str, clock_port: str) -> float:
        """Get the fabric frequency of an IP.

        :param full_ip_name: Full name of the IP.
        :type full_ip_name: str
        :param clock_port: Name of the clock port of the IP.
        :type clock_port: str
        :return: Fabric frequency in MHz.
        :rtype: float
        """
        # use the clock graph on the parser to extract the clock frequency
        clock_graph = self._fireq_parser.clock_graph
        # get the clock frequency of the ip
        for _, v, data in clock_graph.edges(data=True):
            if v == full_ip_name and data["slave_port"] == clock_port:
                return float(data["frequency"])
        # if not found, raise an error
        raise RuntimeError(f"Clock not found for IP {full_ip_name} on port {clock_port}")

    def reset_all_ip_memory_and_registers(self) -> None:
        """Reset the registers and memory for all FIREQ IPs in the design."""
        for _, (_, ip, _) in self.ips.items():
            if isinstance(ip, _FIREQDriver):
                ip.reset_memory_and_registers()

    # ------------------------------------------------------------------
    # RF helpers
    # ------------------------------------------------------------------

    def set_adc_autocalibration_status(self, adc_index: tuple[int, int], freeze: bool = True) -> None:
        """Freeze or unfreeze the calibration of all ADCs.

        :param adc_index: Index of the ADC to set the calibration status for as (tile, block).
        :type adc_index: tuple[int, int]
        :param freeze: Whether to freeze the calibration, defaults to True
        :type freeze: bool
        """
        tile, block = adc_index

        # Freeze calibration for the ADC block if freeze, otherwise unfreeze
        if freeze:
            self.rfdc.adc_tiles[tile].blocks[block].CalFreeze["FreezeCalibration"] = 1
        else:
            self.rfdc.adc_tiles[tile].blocks[block].CalFreeze["FreezeCalibration"] = 0
        self.log.debug("ADC %s calibration frozen: %s", adc_index, freeze)


def load_fireq(bitfile_name: str, init_clocks: bool = True) -> FIREQSoC:
    """Create and initialize a FIREQSoC instance.

    :param bitfile_name: Path to the bitfile to load
    :type bitfile_name: str
    :param init_clocks: Whether to initialize RF clocks
    :type init_clocks: bool
    :return: Initialized FIREQSoC instance
    :rtype: FIREQSoC
    """
    return FIREQSoC(bitfile_name, ignore_version=False, init_clocks=init_clocks)
