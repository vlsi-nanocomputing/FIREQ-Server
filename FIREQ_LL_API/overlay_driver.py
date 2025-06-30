from pynq import Overlay
import xrfdc
import xrfclk
import os
from ._Parser import *
from ._Utils import *
from .acquistion_driver import *
from .generator_driver import *
from .trigger_generator_driver import *

__all__ = ['FIREQ_SoC']

def _init_rf_clks(lmk_freq=245.76, lmx_freq=491.52):
    """Initialise the LMK and LMX clocks for the radio hierarchy.

    The radio clocks are required to talk to the RF-DCs and only need
    to be initialised once per session.

    """        
    xrfclk.set_ref_clks(lmk_freq=lmk_freq, lmx_freq=lmx_freq)

class FIREQ_SoC(Overlay):

    def __init__(self, bitfile_name, ignore_version=False):

        try:
            super().__init__(bitfile_name, ignore_version= ignore_version)
        except:
            print("FIREQ: error during overlay creation, exiting FIREQ custom init routine")
            return

        # get the hwh file
        self._FIREQ_hwh_file = os.path.splitext(self.bitfile_name)[0] + ".hwh"

        # configure parser
        self._FIREQ_parser = FIREQ_parser(self._FIREQ_hwh_file)

        # configure the clocks for the rf
        _init_rf_clks()

        # parse hwh, get address mapping
        mmap = self._FIREQ_parser.GetAddressMapping()
        self._readout_ips = []
        self._generation_ips = []
        self._trigger_ips = []
        for ip in mmap:
            # check that we have the ip as attribute
            if hasattr(self, ip):
                ip_object = getattr(self, ip)
            else:
                continue
            # then check the type of the ip driver, call fireq specific functions if it's a fireq ip
            if isinstance(ip_object, _FIREQDriver):
                for map in mmap[ip]:
                    axi_base = map['BASEVALUE']
                    axi_range = map['HIGHVALUE'] - axi_base + 1
                    if map['SLAVEBUSINTERFACE'] == 's01_axi':
                        ip_object.InitAxiFullInterface(axi_base, axi_range)
                    elif map['SLAVEBUSINTERFACE'] == 's00_axi':
                        ip_object.InitAxiLiteInterface(axi_base, axi_range)
                # since we are iterating over the ips, create the list of readout and generation ips
                if isinstance(ip_object, GeneratorDriver):
                    self._generation_ips.append(ip_object)
                elif isinstance(ip_object, AcquistionDriver):
                    self._readout_ips.append(ip_object)
                elif isinstance(ip_object, TriggerGeneratorDriver):
                    self._trigger_ips.append(ip_object)

