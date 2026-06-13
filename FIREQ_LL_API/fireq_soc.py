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

logger = logging.getLogger(__name__)

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
        # reset the PL server, clears bugged pynq caches that could lead to issues
        PL.reset()

        # Load overlay
        try:
            logger.debug("Creating overlay from %s", bitfile_name)
            super().__init__(bitfile_name, ignore_version=ignore_version)
        except Exception as e:
            # better to raise an exception: server aware of the problem
            logger.error("Error during overlay creation: %s", e)
            raise RuntimeError(f"FIREQ: error during overlay creation: {e}") from e

        # HWH + parser
        self._fireq_hwh_file = os.path.splitext(self.bitfile_name)[0] + ".hwh"
        self._fireq_parser = FireqParser(self._fireq_hwh_file)

        # RF clocks initialisation
        if init_clocks:
            self._init_rf_clks()

        # init the axi interfaces of FIREQ drivers
        self._init_fireq_ips()

        # Organize ALL IPs in the design
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

        # freeze calibration for all ADCs
        for adc_index in self.active_adcs:
            self.set_adc_autocalibration_status(adc_index, freeze=True)

    @staticmethod
    def _init_rf_clks(lmk_freq: float = 245.76, lmx_freq: float = 491.52) -> None:
        """Initialise the LMK and LMX clocks for the RF-DC hierarchy.

        The radio clocks are required to talk to the RF-DCs and only need to be
        initialised once per session.

        :param lmk_freq: Frequency of the LMK clock in MHz, defaults to 245.76
        :type lmk_freq: float
        :param lmx_freq: Frequency of the LMX clock in MHz, defaults to 491.52
        :type lmx_freq: float
        """
        logger.debug(f"Initialising RF clocks: LMK={lmk_freq} MHz, LMX={lmx_freq} MHz")
        xrfclk.set_ref_clks(lmk_freq=lmk_freq, lmx_freq=lmx_freq)

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------
    def _check_fireq_ips(self) -> None:
        """Check that the design contains the necessary FIREQ IPs to conduct experiments."""
        if len(self.ips) == 0:
            logger.error("No FIREQ IPs found in the design.")
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
        logger.debug("Found %s generators, %s acquisitions and %s trigger generators", gen_count, acq_count, ctrl_count)
        if gen_count == 0 or acq_count == 0 or ctrl_count == 0:
            logger.error("The design does not contain the necessary FIREQ IPs to conduct experiments.")
            raise RuntimeError("The design does not contain the necessary FIREQ IPs to conduct experiments.")

    def _init_fireq_ips(self) -> None:
        """Initialize the axi interfaces for FIREQ IPs.

        Some FIREQ IPs have two axi4 interfaces, one full and one lite.
        PYNQ does not support this, so we need to manually set the axi4 interfaces.
        """
        logger.debug("Initialising FIREQ IPs")
        mmap = self._fireq_parser.get_address_mapping()

        # check that the ps name is the mapping, otherwise raise an error
        if self._fireq_parser.ps_name not in mmap.keys():
            logger.error(f"PS name {self._fireq_parser.ps_name} not found in memory map.")
            raise RuntimeError(f"PS name {self._fireq_parser.ps_name} not found in memory map.")

        for axi_map in mmap[self._fireq_parser.ps_name]:
            if not hasattr(self, axi_map["INSTANCE"]):
                continue

            ip_object = getattr(self, axi_map["INSTANCE"])
            # Only consider FIREQ low-level drivers
            if not isinstance(ip_object, _FIREQDriver):
                continue

            # Init AXI based on mapping
            logger.debug("Initialising AXI interfaces for %s", axi_map["INSTANCE"])
            axi_base = int(axi_map["BASEVALUE"], 16)
            axi_range = int(axi_map["HIGHVALUE"], 16) - axi_base + 1
            if axi_map["SLAVEBUSINTERFACE"] == "s00_axi":
                ip_object.init_axi_full_interface(axi_base, axi_range)
            elif axi_map["SLAVEBUSINTERFACE"] == "s01_axi":
                ip_object.init_axi_lite_interface(axi_base, axi_range)

        # find and initialize the FIFOs
        for node, data in self._fireq_parser.system_graph.nodes(data=True):
            if data["vlnv"] in FIFOWrapper.bindto:
                logger.debug("Initialising FIFO %s", data["instance"])
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
            logger.debug("Found IP %s", instance)
            ip_object = getattr(self, instance)
            # add the ip to the dictionary, by storing the instance name as the key and the type as the value
            self.ips[node] = (instance, ip_object, type(ip_object).__name__)

    def _discover_rfdc(self) -> None:
        """Discover the RF-DC IP and initialize the clocks."""
        # find the rfdc ip in the system graph
        for ip in self.ips.values():
            if ip[2] == "RFdc":
                logger.debug("Found RF-DC IP %s", ip[0])
                self.rfdc = ip[1]
                break

        # if the rfdc is not found, assume debug overlay and set the sample rates to 1 and 2 GSps
        if self.rfdc is None:
            logger.warning("RF-DC IP not found, assuming debug overlay")
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
        logger.debug("Active ADCs: %s", self.active_adcs)
        logger.debug("Active DACs: %s", self.active_dacs)

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
            logger.error("IPs have different fabric frequencies")
            raise NotImplementedError("IPs have different fabric frequencies")
        # check that all the generators have the same sampling frequency
        if len(set(gen_sr)) > 1:
            logger.error("Generators have different sampling frequencies")
            raise RuntimeError("Generators have different sampling frequencies")
        # check that all the acquisitions have the same sampling frequency
        if len(set(acq_sr)) > 1:
            logger.error("Acquisitions have different sampling frequencies")
            raise RuntimeError("Acquisitions have different sampling frequencies")
        # set the sampling frequencies
        self.fabric_frequency = fabric_frequency[0]
        self.dac_samplerate = gen_sr[0]
        self.adc_samplerate = acq_sr[0]

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
        logger.debug("ADC %s calibration frozen: %s", adc_index, freeze)


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
