""" Error codes """
IP_MISSING = -1         # IP parameter missing
OPTION_MISSING = -2     # option parameter missing
VALUE_ERROR = -3        # parameter type or range wrong
WRONG_IP = -4           # function not preset fo r the IP


def write_description(ip=None, *options):
    """
    Print the description of the IP

    :param ip: IP selected
    :type ip: DefaultIP
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING

    ip.WriteDescription()
    return 0


def print_help(*options):
    """
    Print the menu
    """
    print("Usage: <command> [<IP class>] [options ...]")
    print("Useful commands available:")
    print("\t- help: Print this message")
    print("\t- exit: Terminate the function")
    print("\t- description <IP>: Print the description of the IP")
    print("\t- help_generator: Print the commands available for Generator IP")


def exit_function(*options):
    """
    Function to terminate the program
    """
    print("# Exit...")
    exit(0)