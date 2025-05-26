""" Error codes """
IP_MISSING = -1         # IP parameter missing
OPTION_MISSING = -2     # option parameter missing
VALUE_ERROR = -3        # parameter type or range wrong


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


def set_manual_trigger(ip=None, *options):
    """
    Set the manual trigger for one channel, use 0 to disable

    :param ip: IP selected
    :type ip: DefaultIP
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING

    ip.SetManualTrigger()
    return 0


def set_source(ip=None, *options):
    """
    Set the source of the wave LFSR or FIFO

    :param ip: IP selected
    :type ip: DefaultIP
    :param source: source of the wave, must be "LFSR" or "FIFO"
    :type source: int
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING
    # check correctness of option
    if len(options) < 1:
        print("# Error: source parameter missing")
        return OPTION_MISSING

    source = options[0]
    if not source in ['LFSR', 'FIFO']:
        print('# Error: source provided is not valid, must be "LFSR" or "FIFO"')
        return VALUE_ERROR

    if source == 'LFSR':
        ip.SetSource(1)
    elif source == 'FIFO':
        ip.SetSource(0)

    return 0


def set_lfsr_seed(ip=None, *options):
    """
    Set the seed for LFSR

    :param ip: IP selected
    :type ip: DefaultIP
    :param seed: seed of LFSR
    :type seed: int
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING
    # check correctness of option
    if len(options) < 1:
        print("# Error: Seed parameter missing")
        return OPTION_MISSING

    try:
        seed = int(options[0])

    except ValueError as e:
        print(f"# Error: {e}")
        return VALUE_ERROR

    ip.SetLFSRSeed(seed)
    return 0


def set_redaout_inc_off(ip=None, *options):
    """
    Set readout increment and offset value

    :param ip: IP selected
    :type ip: DefaultIP
    :param increment: Increment value for readout, must be a 48 bit number
    :type increment: int
    :param offset: Offset value for readout, must be a 48 bit number
    :type offset: int
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING
    # check correctness of option
    if len(options) < 2:
        print("# Error: Increment or Phase parameter missing")
        return OPTION_MISSING

    try:
        increment = int(options[0])
        offset = int(options[0])
        if increment < 0 or increment > 0xFFFFFFFFFFFF:
            raise ValueError(f"{increment} is not a 48 bit number")
        if offset < 0 or offset > 0xFFFFFFFFFFFF:
            raise ValueError(f"{offset} is not a 48 bit number")

    except ValueError as e:
        print(f"# Error: {e}")
        return VALUE_ERROR

    ip.SetReaoutIncOff(increment, offset)
    return 0


def set_drive_inc(ip=None, *options):
    """
    Set drive increment value

    :param ip: IP selected
    :type ip: DefaultIP
    :param increment: Increment value for readout, must be a 48 bit number
    :type increment: int
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING
    # check correctness of option
    if len(options) < 1:
        print("# Error: Increment parameter missing")
        return OPTION_MISSING

    try:
        increment = int(options[0])
        if increment < 0 or increment > 0xFFFFFFFFFFFF:
            raise ValueError(f"{increment} is not a 48 bit number")

    except ValueError as e:
        print(f"# Error: {e}")
        return VALUE_ERROR

    ip.SetDriveInc(increment)
    return 0


def set_trigger(ip=None, *options):
    """
    Set the source of the wave LFSR or FIFO

    :param ip: IP selected
    :type ip: DefaultIP
    :param trigger: Trigger selection, must be "readout" or "drive"
    :type trigger: str
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING
    # check correctness of option
    if len(options) < 1:
        print("# Error: source parameter missing")
        return OPTION_MISSING

    source = options[0]
    if not source in ['readout', 'drive']:
        print('# Error: source provided is not valid, must be "readout" or "drive"')
        return VALUE_ERROR

    if source == 'readout':
        ip.SetTrigger(1)
    elif source == 'drive':
        ip.SetTrigger(0)

    return 0



def print_help(*options):
    """
    Print the menu
    """
    print("Usage: <command> [<IP class>] [options ...]")
    print("Options available:")
    print("\t- help")
    print("\t- exit")
    print("\t- description <IP>: Print the description of the IP")
    print("\t- set_manual_trigger <IP>: Set the manual trigger")
    print("\t- set_source <IP> <source>: Set the source of the wave LFSR or FIFO")
    print("\t- set_seed <IP> <seed>: Set the seed for LFSR")
    print("\t- set_readout_inc_off <IP> <inc> <off>: Set readout increment and offset value")
    print("\t- set_drive_inc <IP> <inc>: Set drive increment value")



def exit_function(*options):
    """
    Function to terminate the program
    """
    print("# Exit...")
    exit(0)