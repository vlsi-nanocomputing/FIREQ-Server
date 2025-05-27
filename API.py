from pynq import Overlay
from pynq import PL
import xrfclk
import xrfdc
import customdrivers

import os

from commands import *

BASE_PATH = "/home/xilinx/jupyter_notebooks/"
IP_BLOCKS = ["Generator", ]


def init_rf_clks(lmk_freq=245.76, lmx_freq=491.52):
    """Initialise the LMK and LMX clocks for the radio hierarchy.

    The radio clocks are required to talk to the RF-DCs and only need
    to be initialised once per session.

    """
    xrfclk.set_ref_clks(lmk_freq=lmk_freq, lmx_freq=lmx_freq)


# define command dict
cmd_dict = {
    "help": print_help,
    "help_generator": print_help_generate,
    "exit": exit_function,
    "description": write_description,
}
# merge generator commands
cmd_dict |= cmd_gen

# Defining main function
def main():
    print("### FIREQ API interface ###\n")
    print_help()

    # get overlay path
    ol_folder = input("\n# Insert Overlay folder\n")
    ol_filename = input('# Insert Overlay filename (press Enter for "overlay.bit")\n')
    if not ol_filename:
        ol_filename = "overlay.bit"

    ol_filepath = BASE_PATH + ol_folder + '/' + ol_filename

    # check existence of overlay
    if not os.path.exists(ol_filepath):
        print(f"# Error: Overlay invalid filepath '{ol_filepath}'")
        exit(-1)

    # reset
    print("# Reset the board")
    PL.reset()

    # get overlay
    print("# Loading the overlay")
    ol = Overlay(ol_filepath)

    # init clock
    print("# Init clock")
    init_rf_clks()

    # create dict with all the IPs
    ip_dict = {}

    all_ips = ol.ip_dict.keys()
    for ip_name in IP_BLOCKS:
        n_instances = 0
        for ip in all_ips:
            if ip_name in ip:
                n_instances += 1
                # ip_dict[f"{ip_name.lower()}{n_instances}"] = ol.ip_dict[ip]["phys_addr"]
                ip_dict[f"{ip_name.lower()}{n_instances}"] = getattr(ol,ip)

    print("\n# IPs available:")
    for ip in ip_dict.keys():
        print(f"\t- {ip}")

    while True:
        cmd = input("\nInsert a command: ")
        cmd_list = cmd.split(" ")

        if not cmd_list[0] in cmd_dict:
            print(f"# Error: invalid command '{cmd_list[0]}'")
            continue

        # only command
        if len(cmd_list) == 1:
            cmd_dict[cmd_list[0]]()

        # command + ip
        if len(cmd_list) == 2:
            # check existence of IP
            if not cmd_list[1] in ip_dict:
                print(f"# Error: invalid IP name '{cmd_list[1]}'")
                continue

            cmd_dict[cmd_list[0]](ip_dict[cmd_list[1]])

        # command + ip + options
        if len(cmd_list) > 2:
            # check existence of IP
            if not cmd_list[1] in ip_dict:
                print(f"# Error: invalid IP name '{cmd_list[1]}'")
                continue

            cmd_dict[cmd_list[0]](ip_dict[cmd_list[1]], *cmd_list[2:])


# __name__
if __name__=="__main__":
    main()