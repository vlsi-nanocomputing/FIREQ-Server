"""Parse FIREQ .hwh files for connectivity and memory mappings."""

import logging
import xml.etree.ElementTree as ET
from collections.abc import Generator

import networkx as nx

logger = logging.getLogger(__name__)

# strings found in .hwh file that define a module as a master for a certain axi connection
MASTER_TYPE_LIST = ["MASTER", "INITIATOR"]
SLAVE_TYPE_LIST = ["SLAVE", "TARGET"]

# Names of ips that are considered to be transparent to a master slave connection
# They must have only one master and slave interface to be considered passthrough
# FIFOs are technically pass-through but they are needed in the software to track packet size
AXIS_PASS_THROUGH_MODULES = [
    "xilinx.com:ip:axis_dwidth_converter:",
    "xilinx.com:ip:axis_clock_converter:",
    "xilinx.com:ip:axis_register_slice:",
]
AXI4_PASS_THROUGH_MODULES = [
    "xilinx.com:ip:axi_clock_converter:",
    "xilinx.com:ip:axi_register_slice:",
]

# vlnv strings for axi4 and axi stream interfaces
STREAM_AXI_VLNV = "xilinx.com:interface:axis:1.0"
AXI4_VLNV = "xilinx.com:interface:aximm:1.0"

# VLNV strings for the ps system depending on the board
BOARD_META = {
    "rf4x2": {
        "device": "xczu48dr",
        "board_vlnv": "realdigital.org:rfsoc4x2:part0:",
        "ps_vlnv": "xilinx.com:ip:zynq_ultra_ps_e:",
    },
    "zcu216": {
        "device": "xczu49dr",
        "board_vlnv": "xilinx.com:zcu216:part0:",
        "ps_vlnv": "xilinx.com:ip:zynq_ultra_ps_e:",
    },
}


class FireqParser:
    """Parse .hwh files to retrieve module connectivity and memory mappings.

    This parser also contains three graphs:
    - system_graph: a graph of all the modules in the design with their connections
    - control_graph: a graph of all the axi4 connections in the design
    - dataflow_graph: a graph of all the axi stream connections in the design

    The system graph is built by parsing the .hwh file and extracting the modules and their connections.
    The control and dataflow graphs are built by filtering the system graph for the respective bus types.

    Each node in the graph contains the following attributes:
    - instance: the name of the instance (unique within the scope)
    - type: the type of the module (axi_dma, axisAcquisitionIP, etc.)
    - vlnv: the complete version of the module
    - bus_connections: a dictionary containing the master and slave connections of the module:
        - masters: a list of dictionaries containing the bus_id, bus_type, bus_name and bus_vlnv of
          the master connections
        - slaves: a list of dictionaries containing the bus_id, bus_type, bus_name and bus_vlnv of the slave connections
    """

    def __init__(self, hwh_file: str) -> None:
        """Initialize the FIREQ parser with a hardware description file.

        :param hwh_file: Path to the .hwh (Xilinx Hardware Handoff) file
        :type hwh_file: str
        """
        # set .hwh file path and logger
        self.log = logging.getLogger(__name__)
        self._file_path = hwh_file
        # generate a tree from file (xml)
        self._tree = ET.parse(hwh_file)
        # get the tree root
        self._root = self._tree.getroot()
        # find the board and the associated ps name
        self.board = self._find_board()
        self.ps_name = self._find_ps_name()
        # build the system graph with axi connections
        self.system_graph = None
        self._build_system_graph()
        # create axi4 and axi stream graph
        self.control_graph = self._create_bus_connectivity_graph(AXI4_VLNV)
        self.dataflow_graph = self._create_bus_connectivity_graph(STREAM_AXI_VLNV)
        self.clock_graph = self._create_bus_connectivity_graph("CLK")

    def set_logger(self, new_logger: logging.Logger) -> None:
        """Set the logger for this object.

        :param new_logger: Logger object to use
        :type new_logger: logging.Logger
        """
        self.log = new_logger

    def _find_board(self) -> str:
        """Find the board name from the .hwh file."""
        # the board vlnv is found in the xml child named SYSTEMINFO, as attribute BOARD
        system_info = self._root.find("SYSTEMINFO")
        if system_info is None:
            raise ValueError("HWH not valid: Tag <SYSTEMINFO> not found.")
        board_vlnv = system_info.get("BOARD")
        if board_vlnv is None:
            raise ValueError("HWH not valid: Attribute BOARD not found in <SYSTEMINFO>.")
        for board_name, meta in BOARD_META.items():
            if board_vlnv.startswith(meta["board_vlnv"]):
                return board_name
        raise ValueError(f"Unknown board VLNV: {board_vlnv}. Supported boards: {list(BOARD_META.keys())}.")

    def _find_ps_name(self) -> str:
        """Find the ps name from the .hwh file."""
        # the ps name is found in the xml child named MODULES, as attribute INSTANCE
        # for the module with VLNV starting with the ps_vlnv
        modules = self._root.find("MODULES")
        if modules is None:
            raise ValueError("HWH not valid: Tag <MODULES> not found.")
        for module in modules.findall("MODULE"):
            vlnv = module.get("VLNV")
            if vlnv and vlnv.startswith(BOARD_META[self.board]["ps_vlnv"]):
                return module.get("INSTANCE")
        raise ValueError(f"PS module not found for board {self.board}.")

    def modules(self) -> Generator[ET.Element]:
        """Iterate over xml modules found in the hwh file.

        :yield: xml module
        :rtype: ET.Element
        """
        modules = self._root.find("MODULES")
        if modules is None:
            raise ValueError("HWH not valid: Tag <MODULES> not found.")
        yield from modules.findall("MODULE")

    def _build_system_graph(self) -> None:
        """Build the graph representing the connections between modules.

        The graph is built using the information in the .hwh file.
        The nodes are the modules and the edges are the connections between them.
        Only AXI-Stream and AXI4 connections are considered.
        """
        self.system_graph = nx.MultiDiGraph()

        # extract modules from the hwh and add them in the graph
        # VLNV="xilinx.com:ip:axi_dma:7.1"
        # dictionary of edges, where the key is the businterface
        # the value is another dict of master, slave, master_if, slave_if, bus_vlnv
        axi_out_edges = {}
        axi_in_edges = {}
        clock_out_edges = {}
        clock_in_edges = {}
        for module in self.modules():
            mod_fullname = module.get("FULLNAME")  # name path starting from root
            mod_instance = module.get("INSTANCE")  # name of the instance, unique within the scope
            mod_type = module.get("MODTYPE")  # type of the module (axi_dma, axisAcquisitionIP, etc.)
            mod_vlnv = module.get("VLNV")  # complete version
            # add the node to the graph
            self.system_graph.add_node(mod_fullname, instance=mod_instance, type=mod_type, vlnv=mod_vlnv)

            # build connectivity
            mod_fullname = module.get("FULLNAME")  # name path starting from root

            # find the bus interfaces
            bus_interfaces = module.find("BUSINTERFACES")
            if bus_interfaces is None:
                continue
            # iterate over the bus interfaces and add the edges to the graph
            for bus_if in bus_interfaces.findall("BUSINTERFACE"):
                bus_type = bus_if.get("TYPE")  # MASTER or SLAVE
                bus_vlnv = bus_if.get("VLNV")  # bus VLNV
                bus_name = bus_if.get("NAME")  # interface name (like drive_axis or m00_axis etc.)
                bus_id = bus_if.get("BUSNAME")  # bus identifier, unique for each connection
                if not bus_id or bus_id.upper() == "UNCONNECTED" or bus_id == "__NOC__":
                    continue
                if bus_vlnv and bus_vlnv in [STREAM_AXI_VLNV, AXI4_VLNV]:
                    if bus_type in MASTER_TYPE_LIST:
                        # add the edge to edge dict, knowing that bus id is unique
                        if bus_id in axi_out_edges.keys():
                            self.log.error("Bus id %s is not unique.", bus_id)
                            raise ValueError(f"Bus id {bus_id} is not unique.")
                        axi_out_edges[bus_id] = {
                            "master": mod_fullname,
                            "master_if": bus_name,
                            "bus_vlnv": bus_vlnv,
                        }
                    elif bus_type in SLAVE_TYPE_LIST:
                        # add the edge to edge dict, knowing that bus id is unique
                        if bus_id in axi_in_edges.keys():
                            self.log.error("Bus id %s is not unique.", bus_id)
                            raise ValueError(f"Bus id {bus_id} is not unique.")
                        axi_in_edges[bus_id] = {
                            "slave": mod_fullname,
                            "slave_if": bus_name,
                            "bus_vlnv": bus_vlnv,
                        }
            # create clocking edges, using the ports child
            ports = module.find("PORTS")
            if ports is None:
                continue
            for port in ports.findall("PORT"):
                port_type = port.get("DIR")
                port_vlnv = port.get("SIGIS")
                port_name = port.get("NAME")
                clk_freq = port.get("CLKFREQUENCY")
                port_id = port.get("SIGNAME")
                if not port_id or port_id.upper() == "UNCONNECTED":
                    continue
                if port_vlnv and port_vlnv.upper() == "CLK":
                    if port_type and port_type.upper() == "O":
                        if port_id in clock_out_edges:
                            self.log.error("Clock port id %s is not unique.", port_id)
                            raise ValueError(f"Clock port id {port_id} is not unique.")
                        clock_out_edges[port_id] = {
                            "master": mod_fullname,
                            "master_port": port_name,
                            "frequency": clk_freq,
                        }
                    elif port_type and port_type.upper() == "I":
                        if port_id not in clock_in_edges:
                            clock_in_edges[port_id] = []
                        clock_in_edges[port_id].append(
                            {
                                "slave": mod_fullname,
                                "slave_port": port_name,
                            }
                        )

        # build the axi connectivity using the two dictionaries
        for bus_id, out_edge in axi_out_edges.items():
            if bus_id in axi_in_edges:
                in_edge = axi_in_edges[bus_id]
                master_node = out_edge["master"]
                slave_node = in_edge["slave"]
                bus_vlnv = out_edge["bus_vlnv"]
                master_if = out_edge["master_if"]
                slave_if = in_edge["slave_if"]
                self.system_graph.add_edge(
                    master_node,
                    slave_node,
                    bus_id=bus_id,
                    bus_vlnv=bus_vlnv,
                    master_port=master_if,
                    slave_port=slave_if,
                )
            else:
                self.log.error("Could not find a slave for %s.", bus_id)
                raise ValueError(f"Could not find a slave for {bus_id}.")

        # build the clock connectivity using the two dictionaries
        for clock_id, out_edge in clock_out_edges.items():
            if clock_id in clock_in_edges:
                for in_edge in clock_in_edges[clock_id]:
                    master_node = out_edge["master"]
                    slave_node = in_edge["slave"]
                    self.system_graph.add_edge(
                        master_node,
                        slave_node,
                        bus_id=clock_id,
                        bus_vlnv="CLK",
                        master_port=out_edge["master_port"],
                        slave_port=in_edge["slave_port"],
                        frequency=out_edge["frequency"],
                    )

        # remove unconnected nodes
        isolated_nodes = [node for node in self.system_graph.nodes if self.system_graph.degree(node) == 0]
        if len(isolated_nodes) > 0:
            self.log.warning("Removing %s isolated nodes from the graph.", len(isolated_nodes))
        self.system_graph.remove_nodes_from(isolated_nodes)

    def _create_bus_connectivity_graph(self, bus_vlnv: str) -> nx.MultiDiGraph:
        """Create the graph of the connections between nodes for the specified bus vlnv.

        Nodes that act as passthrough, like width converters or register slices are collapsed.

        :param bus_vlnv: The vlnv of the bus to filter the graph
        :type bus_vlnv: str
        :return: The graph of the connections between nodes for the specified bus vlnv
        :rtype: nx.MultiDiGraph
        """
        # collapse nodes that act as passthrough, like width converters or register slices
        # create a temporary copy of the graph with only the edges whose vlnv are the one specified
        filtered_graph = nx.MultiDiGraph()
        filtered_graph.add_nodes_from(self.system_graph.nodes(data=True))
        for u, v, data in self.system_graph.edges(data=True):
            if data.get("bus_vlnv") == bus_vlnv:
                filtered_graph.add_edge(u, v, **data)
        # remove orphan nodes from filtered graph
        isolated_nodes = [node for node in filtered_graph.nodes if filtered_graph.degree(node) == 0]
        filtered_graph.remove_nodes_from(isolated_nodes)

        def is_pass_through(vlnv: str, bus_vlnv: str) -> bool:
            """Check if the node is a passthrough node.

            :param vlnv: The vlnv of the node
            :type vlnv: str
            :param bus_vlnv: The vlnv of the bus
            :type bus_vlnv: str
            :return: True if the node is a passthrough node
            :rtype: bool
            """
            if not vlnv:
                return False
            if bus_vlnv == STREAM_AXI_VLNV:
                PASS_THROUGH_MODULES = AXIS_PASS_THROUGH_MODULES
            elif bus_vlnv == AXI4_VLNV:
                PASS_THROUGH_MODULES = AXI4_PASS_THROUGH_MODULES
            elif bus_vlnv == "CLK":
                PASS_THROUGH_MODULES = []
            else:
                raise ValueError(f"Unknown bus vlnv {bus_vlnv}")
            return any(pt in vlnv for pt in PASS_THROUGH_MODULES)

        nodes_to_collapse = []
        for node_id, node_data in filtered_graph.nodes.items():
            vlnv = node_data["vlnv"]
            if vlnv and is_pass_through(vlnv, bus_vlnv):
                # actually check that the pass throgh only has one input and one output
                if filtered_graph.in_degree(node_id) == 1 and filtered_graph.out_degree(node_id) == 1:
                    nodes_to_collapse.append(node_id)
                else:
                    raise NotImplementedError(
                        f"Cannot collapse node {node_id} because it has more than one input or output."
                    )

        # collapse nodes that are considered to be passthrough
        for pt_node in nodes_to_collapse:
            # take the predecessor and successor of the node
            new_master = list(filtered_graph.predecessors(pt_node))[0]
            new_slave = list(filtered_graph.successors(pt_node))[0]

            # get the edge data from the predecessor and successor
            edge_in_data = filtered_graph.get_edge_data(new_master, pt_node)[0]
            edge_out_data = filtered_graph.get_edge_data(pt_node, new_slave)[0]

            # combine the two edge data, taking the edge in data and modifing the slave port name
            data = {**edge_in_data, "slave_port": edge_out_data["slave_port"]}

            # add the new edge, bypassing pt_node
            filtered_graph.add_edge(new_master, new_slave, **data)

            # delete the pt_node
            filtered_graph.remove_node(pt_node)

        return filtered_graph

    def get_address_mapping(self) -> dict[str, list[dict[str, str]]] | None:
        """Get the AXI Memory mapping of AXI master interfaces that are available in the design.

        :return: Dict describing the mapping for ips
        :rtype: dict | None
        """
        address_mapping_dict = {}
        # find modules that have a memory map children
        for module in self._root.findall("./MODULES//MEMORYMAP/.."):
            master_name = module.attrib["INSTANCE"]
            address_mapping_dict[master_name] = []
            # get all memory ranges within the memory map
            for memory_range in module.findall("./MEMORYMAP/MEMRANGE"):
                # add to the dictionary the description of the memory range to the dictionary
                address_mapping_dict[master_name].append(memory_range.attrib.copy())
        # return the dictionary with the memory mapping
        return address_mapping_dict

    def get_module_parameters(self, module_name: str) -> dict[str, str]:
        """Get the parameters of a module.

        :param module_name: The full name of a module
        :type module_name: str
        """
        # find module by full name
        module = self._root.find(f"./MODULES/MODULE[@FULLNAME='{module_name}']")
        if module is None:
            raise ValueError(f"Module {module_name} not found in the hwh file.")
        # get the parameters of the module
        params = {}
        parameters = module.find("PARAMETERS")
        if parameters is not None:
            for param in parameters.findall("PARAMETER"):
                param_name = param.get("NAME")
                param_value = param.get("VALUE")
                if param_name is not None:
                    params[param_name] = param_value
        return params

    def get_interface_map(self, module_fullname: str, graph: nx.MultiDiGraph) -> dict[str, str]:
        """Get the mapping between the interface names and the bus ids.

        :param module_fullname: The full name of a module
        :type module_fullname: str
        :return: A dictionary mapping interface names to bus ids
        :rtype: dict[str, str]
        """
        # find the node in the provided graph, then, map master and interface ports to bus ids
        interface_map = {}
        if module_fullname not in graph.nodes:
            raise ValueError(f"Module {module_fullname} not found in the graph.")
        for u, v, data in graph.out_edges(module_fullname, data=True):
            master_port = data.get("master_port")
            bus_id = data.get("bus_id")
            if master_port is not None and bus_id is not None:
                interface_map[master_port] = bus_id
        for u, v, data in graph.in_edges(module_fullname, data=True):
            slave_port = data.get("slave_port")
            bus_id = data.get("bus_id")
            if slave_port is not None and bus_id is not None:
                interface_map[slave_port] = bus_id
        return interface_map
