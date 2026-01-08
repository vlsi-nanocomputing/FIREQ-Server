import xml.etree.ElementTree as ET

__all__ = ['FIREQ_parser']

# stings found in .hwh file that define a module as a master for a certain axi connection
MASTER_TYPE_LIST = ['MASTER', 'INITIATOR']
# names of ips that are considered to be transparent to a master slave connection for our purposes
PASS_THROUGH_MODULES = ['xilinx.com:ip:axis_dwidth_converter:', 'xilinx.com:ip:axis_data_fifo:', 'xilinx.com:ip:axis_register_slice:', 'xilinx.com:ip:axis_switch:']

class FIREQ_parser:
    """
    FIREQ parser class, used to parse the .hwh file to retrive module connectivity and memory mappings.
    """

    def __init__ (self, HWHFile : str):
        # set .hwh file path
        self._FilePath = HWHFile
        # generate a tree from file (xml)
        self._Tree = ET.parse(HWHFile)
        # get the tree root
        self._Root = self._Tree.getroot()
        # find all modules (IPs) in the design
        self._Modules = self._Root.find('MODULES')

    def _GetModule(self, module_name : str):
        """
        Get the xml description of the IP named by module_name.
        
        :param module_name: IP instance name given in vivado block diagram
        :type module_name: str
        :return: xml element defining the IP or None if no ips were found
        :rtype: xml.etree.ElementTree.Element | None
        """

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
        :return: Dictionary with bus name as key and dictionary description of bus as item
        :rtype: dict[str, dict[str, str]]
        """
        return_dict = {}
        # get the axi bus interfaces for this module
        module_bus_interfaces = module.find('BUSINTERFACES')
        
        # parse the bus interfaces
        for businterface in module_bus_interfaces.iter('BUSINTERFACE'):
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
        """
        Get the connectivity path of the master interfaces of master module.\n
        The return is a dictionary that encodes the connections in a graph like manner,
        where each node is a module and the connections are defined by the axi bus
        names.
         
        :param master_module: Module to check connectivity from
        :type master_module: xml.etree.ElementTree.Element
        :param module_list: list of xml.etree.ElementTree.Element modules
        :type module_list: list
        :return: Connectivity graph
        :rtype: dict[str, Any]
        """
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
        """
        Gets the AXI Memory mapping of the PS/PL master interface
        
        :return: Dict describing the mapping for ips
        :rtype: dict | None
        """
        retdict = {}
        # find the zynq
        for module in self._Root.findall("./MODULES//MEMORYMAP/.."):
            if module.attrib['VLNV'].startswith('xilinx.com:ip:zynq_ultra_ps_e:'):
                # get all memrange children
                for mapping in module.findall("./MEMORYMAP/MEMRANGE"):
                    # append to return dict the memory mapping
                    try:
                        retdict[mapping.attrib['INSTANCE']].append(mapping.attrib.copy())
                    except:
                        retdict[mapping.attrib['INSTANCE']] = [mapping.attrib.copy()]
                    # remove instance name from memory mapping since we are using it for the dict keys
                    del retdict[mapping.attrib['INSTANCE']][-1]['INSTANCE']
                # return the dictionary with the memroy mapping 
                return retdict
    
    def _GetParameter(self, module_name: str, param_name: str):
        """
        Return the VALUE of a PARAMETER with NAME=param_name for module INSTANCE=module_name.
        Returns None if not found.
        """
        mod = self._GetModule(module_name)
        if mod is None:
            return None

        params = mod.find("PARAMETERS")
        if params is None:
            return None

        for p in params.findall("PARAMETER"):
            if p.attrib.get("NAME") == param_name:
                return p.attrib.get("VALUE")

        return None
