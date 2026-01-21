import xml.etree.ElementTree as ET

__all__ = ['FIREQParser']

# stings found in .hwh file that define a module as a master for a certain axi connection
MASTER_TYPE_LIST = ['MASTER', 'INITIATOR']
# names of ips that are considered to be transparent to a master slave connection for our purposes
PASS_THROUGH_MODULES = ['xilinx.com:ip:axis_dwidth_converter:', 'xilinx.com:ip:axis_data_fifo:', 'xilinx.com:ip:axis_register_slice:', 'xilinx.com:ip:axis_switch:']

class FIREQParser:
    """
    FIREQ parser class, used to parse the .hwh file to retrive module connectivity and memory mappings.
    """

    def __init__ (self, hwh_file : str):
        """
        Initialize the FIREQ parser with a hardware description file.

        :param hwh_file: Path to the .hwh (Xilinx Hardware Handoff) file
        :type hwh_file: str
        """
        # set .hwh file path
        self._hwh_file_path = hwh_file
        # generate a tree from file (xml)
        self._xml_tree = ET.parse(hwh_file)
        # get the tree root
        self._xml_root = self._xml_tree.getroot()
        # find all modules (IPs) in the design
        self._Modules = self._xml_root.find('MODULES')

    def get_module(self, module_name : str):
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
    
    def get_bus_interfaces(self, module: ET.Element):
        """
        Get a dictionary of the bus interfaces connected to the module.\n
        Returns a dictionary where each key is the bus name and the entry is a dictionary
        with the rest of the xml elements.

        :param module: Child object of xml file with tag MODULE
        :type module: xml.etree.ElementTree.Element
        :return: Dictionary with bus name as key and dictionary description of bus as item
        :rtype: dict[str, dict[str, str]]
        """
        bus_interface_dict = {}
        # get the axi bus interfaces for this module
        module_bus_interfaces = module.find('BUSINTERFACES')

        # parse the bus interfaces
        for bus_interface in module_bus_interfaces.iter('BUSINTERFACE'):
            bus_name = bus_interface.attrib['BUSNAME']
            bus_interface_dict[bus_name] = bus_interface.attrib.copy()
            del bus_interface_dict[bus_name]['BUSNAME']

        # return the parsed bus interfaces
        # the BUSNAME, e.g. the keys of this dict is the name of the bus net
        # each entry is a dict and the keys can be:
        #    NAME: this is the name of the bus related to the module
        #    TYPE: {MASTER,INITIATOR} OR {SLAVE,TARGET}
        #    VLNV: xilinx.com:interface:aximm:1.0 for AXI4 and xilinx.com:interface:axis:1.0 for AXI STREAM
        return bus_interface_dict
    
    def get_connectivity(self, master_module : ET.Element, module_list : list):
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
        connectivity_graph = {'NODE' : master_module_name, 'BUS_M/S' : (None, None), 'CHILDREN' : []}
        # get the bus interfaces for this module
        master_bus_interfaces = self.get_bus_interfaces(master_module)
        # iterate over the bus interfaces
        for bus_name in master_bus_interfaces:
            # check if the bus type is a master
            if master_bus_interfaces[bus_name]['TYPE'] not in MASTER_TYPE_LIST:
                continue
            # iterate over the modules to find the slave module connected to bus
            for slave_module in module_list:
                slave_bus_interfaces = self.get_bus_interfaces(slave_module)
                if bus_name not in slave_bus_interfaces or slave_module == master_module:
                    continue
                else:
                    # if it's present in this dictionary it must be a slave module
                    if any(slave_module.attrib['VLNV'].startswith(item) for item in PASS_THROUGH_MODULES):
                        child_node = self.get_connectivity(slave_module, module_list)
                        child_node['BUS_M/S'] = (master_bus_interfaces[bus_name]['NAME'], slave_bus_interfaces[bus_name]['NAME'])
                        connectivity_graph['CHILDREN'].append(child_node)
                        break
                    else:
                        connectivity_graph['CHILDREN'].append({'NODE' : slave_module.attrib['INSTANCE'], 'BUS_M/S': (master_bus_interfaces[bus_name]['NAME'], slave_bus_interfaces[bus_name]['NAME'])})
                        break

        return connectivity_graph
    
    def get_address_mapping(self):
        """
        Gets the AXI Memory mapping of the PS/PL master interface

        :return: Dict describing the mapping for ips
        :rtype: dict | None
        """
        address_mapping_dict = {}
        # find the zynq
        for module in self._xml_root.findall("./MODULES//MEMORYMAP/.."):
            if module.attrib['VLNV'].startswith('xilinx.com:ip:zynq_ultra_ps_e:'):
                # get all memrange children
                for memory_range in module.findall("./MEMORYMAP/MEMRANGE"):
                    # append to return dict the memory mapping
                    try:
                        address_mapping_dict[memory_range.attrib['INSTANCE']].append(memory_range.attrib.copy())
                    except KeyError:
                        address_mapping_dict[memory_range.attrib['INSTANCE']] = [memory_range.attrib.copy()]
                    # remove instance name from memory mapping since we are using it for the dict keys
                    del address_mapping_dict[memory_range.attrib['INSTANCE']][-1]['INSTANCE']
                # return the dictionary with the memory mapping
                return address_mapping_dict
    
    def get_parameter(self, module_name: str, param_name: str):
        """
        Return the VALUE of a PARAMETER with NAME=param_name for module INSTANCE=module_name.

        :param module_name: IP instance name given in vivado block diagram
        :type module_name: str
        :param param_name: Name of the parameter to retrieve
        :type param_name: str
        :return: Value of the parameter or None if not found
        :rtype: str | None
        """
        module = self.get_module(module_name)
        if module is None:
            return None

        parameters = module.find("PARAMETERS")
        if parameters is None:
            return None

        for parameter in parameters.findall("PARAMETER"):
            if parameter.attrib.get("NAME") == param_name:
                return parameter.attrib.get("VALUE")

        return None
