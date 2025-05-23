def write_description(ip=None, *options):
    """
    Print the description of the IP

    Parameters
    ----------
    ip : class DefaultIP
    """
    # check IP instance
    if ip is None:
        print("# Error: IP not defined")
        return

    print(ip.WriteDescription())

def print_help(*options):
    """
    Print the menu
    """
    print("Usage: <command> [<IP class>] [options ...]")
    print("Options availables:")
    print("\t- help")
    print("\t- exit")
    print("\t- description <IP>: give a description of the IP")


def exit_function(*options):
    """
    Function to terminate the program
    """
    print("# Exit...")
    exit(0)

print_help()