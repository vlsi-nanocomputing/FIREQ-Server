from commands.Commands import IP_MISSING, OPTION_MISSING, VALUE_ERROR, WRONG_IP


def set_manual_trigger(ip=None, *options):
    """
    set_manual_trigger <IP>: Set the manual trigger

    :param ip: IP selected
    :type ip: DefaultIP
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING

    try:
        ip.SetManualTrigger()

    except AttributeError as e:
        print(f" Error: {e}")
        return WRONG_IP
    return 0


def set_source(ip=None, *options):
    """
    set_source <IP> <source>: Set the source of the wave (LFSR or FIFO)

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

    try:
        if source == 'LFSR':
            ip.SetSource(1)
        elif source == 'FIFO':
            ip.SetSource(0)

    except AttributeError as e:
        print(f" Error: {e}")
        return WRONG_IP

    return 0


def get_source(ip=None, *options):
    """
    get_source <IP>: Get the source of the wave (LFSR or FIFO)

    :param ip: IP selected
    :type ip: DefaultIP
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING

    try:
        ip.GetSource()

    except AttributeError as e:
        print(f" Error: {e}")
        return WRONG_IP

    return 0


def set_lfsr_seed(ip=None, *options):
    """
    set_seed <IP> <seed>: Set the seed for LFSR

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

    try:
        ip.SetLFSRSeed(seed)

    except AttributeError as e:
        print(f" Error: {e}")
        return WRONG_IP

    return 0


def get_lfsr_seed(ip=None, *options):
    """
    get_seed <IP>: Set the seed for LFSR

    :param ip: IP selected
    :type ip: DefaultIP
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING

    try:
        ip.GetLFSRSeed()

    except AttributeError as e:
        print(f" Error: {e}")
        return WRONG_IP

    return 0


def set_redaout_inc_off(ip=None, *options):
    """
    set_readout_inc_off <IP> <inc> <off>: Set readout increment and offset value

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

    try:
        ip.SetReaoutIncOff(increment, offset)

    except AttributeError as e:
        print(f" Error: {e}")
        return WRONG_IP

    return 0


def set_drive_inc(ip=None, *options):
    """
    set_drive_inc <IP> <inc>: Set drive increment value

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

    try:
        ip.SetDriveInc(increment)

    except AttributeError as e:
        print(f" Error: {e}")
        return WRONG_IP

    return 0


def set_trigger(ip=None, *options):
    """
    set_trigger <IP> <ttype>: Set the type of trigger (readout or drive)

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

    try:
        if source == 'readout':
            ip.SetTrigger(1)
        elif source == 'drive':
            ip.SetTrigger(0)

    except AttributeError as e:
        print(f" Error: {e}")
        return WRONG_IP

    return 0


def set_trigger_channel(ip=None, *options):
    """
    set_trigger_channel <IP> <channel> <ttype>: Set the channel to trigger referred to the type (readout or drive)

    :param ip: IP selected
    :type ip: DefaultIP
    :param channel: Channel to select
    :type channel: int
    :param ttype: Trigger type, must be "readout" or "drive"
    :type ttype: str
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING
    # check correctness of option
    if len(options) < 2:
        print("# Error: channel or ttype parameter missing")
        return OPTION_MISSING

    channel = 0
    try:
        channel = int(options[0])

    except ValueError as e:
        print(f"Error: {e}")

    ttype = options[1]
    if not ttype in ['readout', 'drive']:
        print('# Error: ttype provided is not valid, must be "readout" or "drive"')
        return VALUE_ERROR

    try:
        if ttype == 'readout':
            ip.SetTriggerChannel(channel, 1)
        elif ttype == 'drive':
            ip.SetTriggerChannel(channel, 0)

    except AttributeError as e:
        print(f" Error: {e}")
        return WRONG_IP

    return 0


def get_trigger_channel(ip=None, *options):
    """
    get_trigger_channel <IP> <ttype>: Get thetrigger mask referred to the type (readout or drive)

    :param ip: IP selected
    :type ip: DefaultIP
    :param ttype: Trigger type, must be "readout" or "drive"
    :type ttype: str
    :return: Error code
    :rtype: int
    """
    # check IP instance
    if ip is None:
        print("# Error: IP parameter missing")
        return IP_MISSING
    # check correctness of option
    if len(options) < 1:
        print("# Error: ttype parameter missing")
        return OPTION_MISSING

    ttype = options[0]
    if not ttype in ['readout', 'drive']:
        print('# Error: ttype provided is not valid, must be "readout" or "drive"')
        return VALUE_ERROR

    try:
        if ttype == 'readout':
            ip.GetTriggerChannel(1)
        elif ttype == 'drive':
            ip.GetTriggerChannel(0)

    except AttributeError as e:
        print(f" Error: {e}")
        return WRONG_IP

    return 0

# add all function to dict
cmd_gen = {
    "set_manual_trigger": set_manual_trigger,
    "set_source": set_source,
    "get_source": get_source,
    "set_seed": set_lfsr_seed,
    "get_seed": get_lfsr_seed,
    "set_readout_inc_off": set_redaout_inc_off,
    "set_drive_inc": set_drive_inc,
    "set_trigger": set_trigger,
    "set_trigger_channel": set_trigger_channel,
    "get_trigger_channel": get_trigger_channel,
}


def print_help_generate(*options):
    """
    Print the menu
    """
    print("Usage: <command> [<IP class>] [options ...]")
    print("Commands available for generator:")
    # get first row of docstring
    for cmd in cmd_gen:
        doc = cmd_gen[cmd].__doc__.split('\n')[1].lstrip(" \n\t")
        print(f"\t- {doc}")

print_help_generate()