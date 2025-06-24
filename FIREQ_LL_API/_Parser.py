import xml.etree.ElementTree as ET

__all__ = ['FIREQ_parser']

MASTER_TYPE_LIST = ['MASTER', 'INITIATOR']
PASS_THROUGH_MODULES = ['xilinx.com:ip:axis_dwidth_converter:', 'xilinx.com:ip:axis_data_fifo:', 'xilinx.com:ip:axis_register_slice:', 'xilinx.com:ip:axis_switch:']

class FIREQ_parser:

    _FilePath = ""

    def __init__ (self, HWHFile : str):

        # set filepath
        self._FilePath = HWHFile
        # set tree
        self._Tree = ET.parse(HWHFile)
        # set root
        self._Root = self._Tree.getroot()
        # find the modules 
        self._Modules = self._Root.find('MODULES')

    def _GetModule(self, module_name : str):

        # find the module
        for module in self._Modules:
            if module.tag == 'MODULE' and module.attrib['INSTANCE'] == module_name:
                return module
            
        # return none if module not found
        return None
    
    def _GetBusInterfaces(self, module: ET.Element):
        """
        Get a dictionary of the bus interfaces connected to the module.\n
        Returns a dictionary where each key is the bus name and the entry is a dictionary
        with the rest of the xml elements.
        
        :param module: Child object of xml file with tag MODULE
        :type module: xml.etree.ElementTree.Element
        """
        return_dict = {}
        # get the axi bus interfaces for this module
        thisModuleBusInterfaces = module.find('BUSINTERFACES')
        
        # parse the bus interfaces
        for businterface in thisModuleBusInterfaces.iter('BUSINTERFACE'):
            busname = businterface.attrib['BUSNAME']
            return_dict[busname] = businterface.attrib.copy()
            del return_dict[busname]['BUSNAME']

        # return the parsed bus interfaces
        # the BUSNAME, e.g. the keys of this dict is the name of the bus net
        # each entry is a dict and the keys can be:
        #    NAME: this is the name of the bus related to the module
        #    TYPE: {MASTER,INITIATOR} OR {SLAVE,TARGET} 
        #    VLNV: xilinx.com:interface:aximm:1.0 for AXI4 and xilinx.com:interface:axis:1.0 for AXI STREAM
        return return_dict
    
    def GetConnectivity(self, master_module : ET.Element, module_list : list):
        
        # get the name for the master module
        master_module_name = master_module.attrib['INSTANCE']
        # create the return dictionary
        return_dict = {'NODE' : master_module_name, 'BUS_M/S' : (None, None), 'CHILDREN' : []}
        # get the bus interfaces for this module
        master_bus_interfaces = self._GetBusInterfaces(master_module)
        # iterate over the bus interfaces
        for bus in master_bus_interfaces:
            # check if the bus type is a master
            if master_bus_interfaces[bus]['TYPE'] not in MASTER_TYPE_LIST:
                continue
            # iterate over the modules to find the slave module connected to bus
            for mod in module_list:
                slavebusif = self._GetBusInterfaces(mod)
                if bus not in slavebusif or mod == master_module:
                    continue
                else:
                    # if it's present in this dictionary it must be a slave module
                    if any(mod.attrib['VLNV'].startswith(item) for item in PASS_THROUGH_MODULES):
                        child_dict = self.GetConnectivity(mod, module_list)
                        child_dict['BUS_M/S'] = (master_bus_interfaces[bus]['NAME'], slavebusif[bus]['NAME'])
                        return_dict['CHILDREN'].append(child_dict)
                        break
                    else:
                        return_dict['CHILDREN'].append({'NODE' : mod.attrib['INSTANCE'], 'BUS_M/S': (master_bus_interfaces[bus]['NAME'], slavebusif[bus]['NAME'])})
                        break

        return return_dict
    
    def GetAddressMapping(self):

        retdict = {}
        for module in self._Modules.iter('MODULE'):
            if module.attrib['VLNV'].startswith('xilinx.com:ip:zynq_ultra_ps_e:'):
                mmap =  module.find('MEMORYMAP')
                for item in mmap:
                    retdict[item.attrib['INSTANCE']] = item.attrib.copy()
                    del retdict[item.attrib['INSTANCE']]['INSTANCE']
                return retdict


