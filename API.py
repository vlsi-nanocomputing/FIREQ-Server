from pynq import Overlay
from pynq import PL
import xrfclk
import xrfdc

import os

from FIREQ_LL_API.OverlayDriver import FIREQ_SoC

# Path to the base directory where the overlay files are stored
BASE_PATH = "/home/xilinx/jupyter_notebooks/"


# Defining main function
def main():
    print("### FIREQ API interface ###\n")

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

    # get overlay
    print("# Loading the overlay")
    ol = FIREQ_SoC(ol_filepath)


if __name__=="__main__":
    main()